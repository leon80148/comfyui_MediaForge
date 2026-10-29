"""Timeline 分段落盤引擎：不可變 MF_SEGMENT_STATE、flush / crossfade、FFV1 spill writer、最終組裝。

每段解碼後只把「已確定」的輸出區間串流寫成 FFV1 part（音訊寫 float32 WAV part），記憶體只留
下一段 crossfade 要用的 pending 與 carry tail。part 的檔案生命週期綁在 SpillPart 物件上：後續
state 以 tuple 共享同一批 SpillPart，最後一個強引用消失才刪檔——ComfyUI 快取逐出某個 state
不會刪到其他 state 還在用的檔案。
"""
import json
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import uuid
import weakref
from fractions import Fraction

import numpy as np
import torch

from .ffmpeg import resolve_ffmpeg_cmd
from .output_path import resolve_output_path
from .plugin_tmp import PLUGIN_TMP_DIR
from .timeline_media import decode_audio
from .timeline_messages import TimelineError, format_message
from .timeline_plan import sample_index
from .timeline_source import opaque_fingerprint, resolve_timeline_source

APPEND_LABEL = "Append Segment"
ASSEMBLE_LABEL = "Assemble Segments"

SPILL_ROOT = PLUGIN_TMP_DIR
CHUNK_BYTES = 16 * 1024 * 1024
STALL_TIMEOUT = 120.0
STDERR_TAIL_BYTES = 64 * 1024
RAM_HEADROOM = 0.8
# 實測 FFV1 level 3 ≈ 0.51 MB/幀 @1344×768；磁碟預檢再乘 2 倍
FFV1_BYTES_PER_PIXEL = 0.5
THUMB_SIZE = 16
COLOR_TAGS = {"color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709", "color_range": "tv"}


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _remove_session_dir(path):
    _unlink(os.path.join(path, "owner.json"))
    try:
        os.rmdir(path)
    except OSError:
        pass


class _Session:
    """session 目錄的擁有者；每個 SpillPart 都強引用它，最後一個 part 消失後才移除目錄。"""

    def __init__(self, path):
        self.path = path
        weakref.finalize(self, _remove_session_dir, path)


_live_sessions = weakref.WeakValueDictionary()


def _open_session(path):
    session = _live_sessions.get(path)
    if session is None:
        import psutil
        os.makedirs(path, exist_ok=True)
        # PID + 程序啟動時間：sweep 只清「owner 已不在」的目錄，PID 被重用也不會誤判
        with open(os.path.join(path, "owner.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "create_time": psutil.Process().create_time()}, f)
        session = _live_sessions[path] = _Session(path)
    return session


class SpillPart:
    """一個已驗證、已 rename 的 part 檔。finalizer 只捕捉路徑字串——捕捉 self 會讓它永遠不被回收。"""

    def __init__(self, session, path, kind, start, count, layout):
        self.session = session
        self.path = path
        self.kind = kind        # "video"：FFV1 幀；"audio"：float32 WAV samples
        self.start = start      # 輸出座標的起始幀 / 起始 sample
        self.count = count
        self.layout = layout    # video：(width, height, (num, den))；audio：(sample_rate, channels)
        self.size = os.path.getsize(path)
        self._finalizer = weakref.finalize(self, _unlink, path)

    def intact(self):
        try:
            return os.path.getsize(self.path) == self.size
        except OSError:
            return False

    def remove(self):
        self._finalizer()


class PendingPart(SpillPart):
    """crossfade 保留區（raw uint8 RGB 或 float32 PCM）。與 SpillPart 同一套 owner：被多個 state
    共享、最後一個引用消失才刪檔；讀取不消耗，重跑 / 分支可以重讀。"""

    def __init__(self, session, path, kind, start, count, layout, shape, dtype):
        super().__init__(session, path, kind, start, count, layout)
        self.shape = tuple(shape)
        self.dtype = dtype

    def load(self, label):
        if not self.intact():
            raise TimelineError(label, "E_SPILL_MISSING")
        return torch.from_numpy(np.fromfile(self.path, dtype=self.dtype).reshape(self.shape))


def _write_raw(path, tensor):
    """tensor → raw bytes（.partial → 驗證大小 → rename）。"""
    data = tensor.detach().to("cpu").contiguous().numpy()
    partial = path + ".partial"
    try:
        data.tofile(partial)
        if os.path.getsize(partial) != data.nbytes:
            raise RuntimeError(f"[{APPEND_LABEL}] pending 暫存寫入不完整。請確認磁碟空間後重試。")
        os.replace(partial, path)
    except BaseException:
        _unlink(partial)
        raise
    return data.dtype.str


# ---------- 純運算：量化、crossfade、音訊 flush ----------

def crossfade_frames(seam):
    return seam["frames"] if seam is not None and seam["kind"] == "crossfade" else 0


def quantize(frames):
    """float [N,H,W,3]（任意 device / dtype）→ CPU uint8，與 video_io 相同的 round(clamp(x)·255)。"""
    x = frames.to(device="cpu", dtype=torch.float32, copy=True)
    return x.clamp_(0.0, 1.0).mul_(255.0).round_().to(torch.uint8)


def mix_frame(a_u8, b, w):
    """crossfade 一幀：在 0–255 浮點域算 (1−w)·A + w·B，最後才量化。"""
    b255 = b.to(device="cpu", dtype=torch.float32).clamp(0.0, 1.0) * 255.0
    return ((1.0 - w) * a_u8.to(torch.float32) + w * b255).round_().clamp_(0.0, 255.0).to(torch.uint8)


def chunk_frames(width, height):
    """每批轉換 / 寫入的幀數：float32 工作副本 + uint8 結果合計約 CHUNK_BYTES，至少一幀。"""
    return max(1, CHUNK_BYTES // (width * height * 3 * 5))


def audio_input(audio, label):
    """AUDIO dict → (waveform [C,N] float32 CPU, sample_rate)。"""
    wave = audio["waveform"]
    if wave.ndim != 3 or wave.shape[0] != 1:
        raise ValueError(f"[{label}] AUDIO waveform 必須是 batch=1 的 [1,C,N]，收到 {tuple(wave.shape)}。請一次只接一條音訊。")
    return wave[0].detach().to(device="cpu", dtype=torch.float32), int(audio["sample_rate"])


def _fade_gains(w, curve):
    if curve == "linear":
        return 1.0 - w, w
    return torch.cos(math.pi / 2 * w), torch.sin(math.pi / 2 * w)


def flush_audio(committed_end, pending, wave, wave_start, mix, commit_end, curve, last):
    """§4.6 音訊 flush；回傳 (從 committed_end 起要 commit 的 [C,k] samples, 補靜音數)。

    pending = (起點, [C,m]) 是前段扣留的 crossfade 音訊；wave 是本段音訊，絕對起點 wave_start；
    mix = (m0, m1) 是混合區。缺資料的位置不當成 0 參與淡化：只有一邊有資料就直接用那一邊。
    非末段的尾端缺口留給下一段——下一段自己的音訊從重疊區開始，可能就涵蓋它（H3 每段解碼
    音訊比畫面短約 8 ms）；只有被後面資料夾住的缺口、或末段到 commit_end 的缺口才補靜音。
    """
    span = commit_end - committed_end
    out = torch.zeros(wave.shape[0], span, dtype=torch.float32)
    have = torch.zeros(span, dtype=torch.bool)
    lo, hi = max(committed_end, wave_start), min(commit_end, wave_start + wave.shape[1])
    if hi > lo:
        out[:, lo - committed_end:hi - committed_end] = wave[:, lo - wave_start:hi - wave_start]
        have[lo - committed_end:hi - committed_end] = True
    if mix is not None:
        m0, m1 = mix
        p0, a = pending
        lo, hi = max(m0, p0, committed_end), min(m1, p0 + a.shape[1], commit_end)
        if hi > lo:
            w = torch.arange(lo - m0 + 1, hi - m0 + 1, dtype=torch.float64) / (m1 - m0 + 1)
            ga, gb = _fade_gains(w, curve)
            idx = slice(lo - committed_end, hi - committed_end)
            av = a[:, lo - p0:hi - p0].double()
            out[:, idx] = torch.where(have[idx], ga * av + gb * out[:, idx].double(), av).float()
            have[idx] = True
    filled = torch.nonzero(have).flatten()
    k = span if last else (int(filled[-1]) + 1 if len(filled) else 0)
    return out[:, :k], k - int(have[:k].sum())


# ---------- 序列 / 預檢 ----------

def check_state(state, label):
    if not isinstance(state, dict) or state.get("type") != "MF_SEGMENT_STATE":
        raise ValueError(f"[{label}] state 必須來自 {APPEND_LABEL}，請連接前一段的 state。")
    if state.get("version") != 2:
        raise ValueError(f"[{label}] state 是舊版格式，請重新執行整個時間軸流程。")


def check_sequence(state, segment):
    if state is None:
        if segment["index"] != 0:
            raise TimelineError(APPEND_LABEL, "E_SEQUENCE", expected=1, actual=segment["index"] + 1)
        return
    check_state(state, APPEND_LABEL)
    if state["complete"]:
        raise TimelineError(APPEND_LABEL, "E_SEQUENCE", expected="（已完成，不能再附加）", actual=segment["index"] + 1)
    if state["next_index"] != segment["index"]:
        raise TimelineError(APPEND_LABEL, "E_SEQUENCE", expected=state["next_index"] + 1, actual=segment["index"] + 1)
    # 組裝前綴身分：前面各段的取樣內容、seam、emit、鎖音都相同才接受（不同計畫的等價前綴可以共用）
    if state["assembly_id"] != segment["prev_assembly_id"]:
        raise TimelineError(APPEND_LABEL, "E_PLAN")


def check_ram(nbytes):
    import psutil
    if nbytes > psutil.virtual_memory().available * RAM_HEADROOM:
        raise TimelineError(APPEND_LABEL, "E_RAM", gib=nbytes / 2 ** 30)


# ---------- spill writer（writer thread + 呼叫端監督） ----------

class _StderrTail(threading.Thread):
    """持續讀 stderr（避免 pipe 塞滿反過來卡住 FFmpeg），只保留最後 64 KiB。"""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self._stream = stream
        self._buf = bytearray()
        self.start()

    def run(self):
        while chunk := self._stream.read1(4096):
            self._buf += chunk
            del self._buf[:-STDERR_TAIL_BYTES]

    def print_tail(self, label):
        lines = bytes(self._buf).decode("utf-8", errors="replace").strip().splitlines()[-30:]
        if lines:
            print(f"[{label}] FFmpeg stderr：\n" + "\n".join(lines))


def _feed(stdin, chunks, progress, errors):
    try:
        for chunk in chunks:
            stdin.write(memoryview(chunk.contiguous().numpy()).cast("B"))
            progress[0] += int(chunk.shape[0])
        stdin.close()
    except Exception as e:
        errors.append(e)


def _joined(thread):
    thread.join(0.2)
    return not thread.is_alive()


def _exited(proc):
    try:
        proc.wait(0.2)
        return True
    except subprocess.TimeoutExpired:
        return False


def _supervise(finished, check_interrupt, progress=None):
    """等 finished() 成立；期間檢查取消，progress() 超過 STALL_TIMEOUT 沒變就判定卡住。"""
    last, since = (progress() if progress else None), time.monotonic()
    while not finished():
        check_interrupt()
        if progress is None:
            continue
        now = progress()
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since > STALL_TIMEOUT:
            raise RuntimeError(f"[{APPEND_LABEL}] FFmpeg 超過 {STALL_TIMEOUT:g} 秒沒有進度，已中止。請確認磁碟空間與系統負載後重試。")


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _probe_streams(path, fmt, entries):
    cmd = resolve_ffmpeg_cmd(["ffprobe", "-v", "error", "-f", fmt, "-count_packets",
                              "-show_entries", f"stream={entries}", "-of", "json", path])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=STALL_TIMEOUT)
    if r.returncode != 0:
        return []
    return json.loads(r.stdout).get("streams", [])


def _ffv1_command(partial, width, height, fps):
    return ["ffmpeg", "-nostdin", "-v", "error", "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", f"{width}x{height}", "-framerate", f"{fps[0]}/{fps[1]}", "-i", "pipe:0", "-an",
            "-c:v", "ffv1", "-level", "3", "-slices", "16", "-pix_fmt", "bgr0", "-f", "matroska", partial]


def _write_video_part(path, chunks, width, height, fps, count, check_interrupt):
    """chunks（uint8 [n,H,W,3]）→ FFV1 .partial；幀數經 ffprobe 驗證後才 rename 成 path。"""
    partial = path + ".partial"
    proc = subprocess.Popen(resolve_ffmpeg_cmd(_ffv1_command(partial, width, height, fps)),
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    stderr = _StderrTail(proc.stderr)
    progress, errors = [0], []
    writer = threading.Thread(target=_feed, args=(proc.stdin, chunks, progress, errors), daemon=True)
    writer.start()
    try:
        try:
            _supervise(lambda: _joined(writer), check_interrupt, lambda: progress[0])
            if not errors:
                _supervise(lambda: _exited(proc), check_interrupt, lambda: progress[0])
        finally:
            # 取消 / 逾時時直接結束 FFmpeg，阻塞在 pipe write 的 writer thread 才會醒來
            _stop(proc)
            writer.join()
            stderr.join()
            try:
                proc.stdin.close()
            except OSError:
                pass
        if errors and not isinstance(errors[0], OSError):
            raise errors[0]
        if errors or proc.returncode != 0:
            stderr.print_tail(APPEND_LABEL)
            raise RuntimeError(f"[{APPEND_LABEL}] FFV1 spill 寫入失敗（exit {proc.returncode}），請查看上方 stderr 輸出。")
        streams = _probe_streams(partial, "matroska", "codec_type,nb_read_packets")
        written = streams[0].get("nb_read_packets") if len(streams) == 1 else None
        if progress[0] != count or written != str(count):
            raise RuntimeError(f"[{APPEND_LABEL}] spill part 幀數不符：預期 {count}，送出 {progress[0]}，檔案 {written}。請重試。")
        os.replace(partial, path)
    except BaseException:
        _unlink(partial)
        raise


def _write_wav(path, wave, sample_rate):
    """[C,N] → float32 PCM WAV（WAVE_FORMAT_IEEE_FLOAT）；先寫 .partial 再 rename。"""
    channels, n = wave.shape
    data = wave.t().contiguous().numpy().astype("<f4", copy=False)
    fmt = struct.pack("<HHIIHH", 3, channels, sample_rate, sample_rate * channels * 4, channels * 4, 32)
    partial = path + ".partial"
    try:
        with open(partial, "wb") as f:
            f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 12 + 8 + data.nbytes) + b"WAVE")
            f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
            f.write(b"fact" + struct.pack("<II", 4, n))
            f.write(b"data" + struct.pack("<I", data.nbytes))
            f.write(memoryview(data).cast("B"))
        os.replace(partial, path)
    except BaseException:
        _unlink(partial)
        raise


def _committed_chunks(frames, held, x_in, own_lo, own_hi, step):
    # crossfade 幀逐幀算，其餘幀分批量化：整段只在 writer 端以 ~16 MiB 工作集轉成 uint8
    for j in range(x_in):
        yield mix_frame(held[j], frames[own_lo - x_in + j], (j + 1) / (x_in + 1)).unsqueeze(0)
    for i in range(own_lo, own_hi, step):
        yield quantize(frames[i:min(i + step, own_hi)])


# ---------- Append ----------

def _thumb(frame):
    x = frame.to(device="cpu", dtype=torch.float32).clamp(0.0, 1.0).permute(2, 0, 1).unsqueeze(0) * 255.0
    return torch.nn.functional.adaptive_avg_pool2d(x, THUMB_SIZE)[0]


def _luma(thumb):
    return float(0.2126 * thumb[0].mean() + 0.7152 * thumb[1].mean() + 0.0722 * thumb[2].mean())


def _seam_report(segment, state, frames, out, padded, curve, x_in, window):
    """邊界診斷（只回報）：前段最後一幀 vs 本段第一個輸出幀的 16×16 縮圖 MAE / 亮度跳變（0–255）；
    音訊 jump = 本段第一個 commit sample 與前一個已 commit sample 的最大差，RMS / 峰值取本段
    開頭 window 個 samples（crossfade 時涵蓋混合區）。"""
    report = {"index": segment["index"], "frame": segment["emit_interval"][0], "kind": segment["seam_in"]["kind"],
              "frames": x_in, "audio_curve": curve if (x_in and out is not None) else None,
              "thumb_mae": None, "luma_jump": None, "audio_jump": None, "audio_rms": None, "audio_peak": None,
              "padded_samples": padded}
    edge = state["edge"]
    if edge is not None:
        right = _thumb(frames[segment["overlap_in"]])
        report["thumb_mae"] = float((right - edge["thumb"]).abs().mean())
        report["luma_jump"] = _luma(right) - _luma(edge["thumb"])
    if out is not None and out.shape[1]:
        if edge is not None and edge["audio"] is not None:
            report["audio_jump"] = float((out[:, 0] - edge["audio"]).abs().max())
        head = out[:, :window]
        report["audio_rms"] = float(head.pow(2).mean().sqrt())
        report["audio_peak"] = float(head.abs().max())
    return report


def _final_ok(state):
    clock = state["audio_clock"]
    total = state["total_frames"]
    return (state["complete"] and state["pending"] is None and state["committed_frames"] == total
            and (clock is None
                 or state["committed_end_sample"] == sample_index(total, clock["sample_rate"], state["video"]["fps"])))


def _check_invariants(new, old):
    pending = new["pending"]
    held = pending["frame_range"][1] - pending["frame_range"][0] if pending else 0
    ok = (new["committed_frames"] + held == new["processed_end"]
          and new["committed_end_sample"] >= old["committed_end_sample"]
          and new["available_end_sample"] >= new["committed_end_sample"])
    if not ok or (new["complete"] and not _final_ok(new)):
        raise RuntimeError(f"[{APPEND_LABEL}] state 不變量檢查失敗（內部錯誤）。請重新執行整個迴圈。")


def _merge_locks(held, added):
    """母帶鎖音區間依 anchor id 合併：同一個 lock 跨重疊區會被前後兩段各收錄一次。"""
    by_anchor = {lock["anchor"]: lock for lock in held}
    for lock in added:
        by_anchor.setdefault(lock["anchor"], lock)
    return tuple(sorted(by_anchor.values(), key=lambda lock: lock["start"]))


def append_segment(state, segment, frames, audio, *, curve, carry, check_interrupt, lock_drift=None):
    """把一段畫面 / 音訊 flush 進 spill，回傳新的 state（不修改輸入 state）。

    frames：[L,H,W,3] float，幀數已驗證；audio：audio_input() 的結果或 None；carry：下一段的
    tail 或 None。任何失敗只清掉本次產物後 raise——輸入 state 與它的 part 不受影響。
    """
    length = segment["length"]
    a, e = segment["emit_interval"]
    # 本段第 0 幀在輸出座標的位置；cut 模式的 start 是來源座標，不能拿來對齊
    origin = a - segment["overlap_in"]
    x_in, x_out = crossfade_frames(segment["seam_in"]), crossfade_frames(segment["seam_out"])
    height, width = int(frames.shape[1]), int(frames.shape[2])
    fps = tuple(segment["fps"])
    layout = None if audio is None else (audio[1], int(audio[0].shape[0]))
    if state is None:
        session_id = uuid.uuid4().hex
        state = {"type": "MF_SEGMENT_STATE", "version": 2, "session": session_id, "assembly_id": None,
                 "spill_dir": os.path.join(SPILL_ROOT, f"seg_{session_id}"),
                 "next_index": 0, "video": {"width": width, "height": height, "fps": list(fps)},
                 "audio_clock": None if layout is None else {"sample_rate": layout[0], "channels": layout[1]},
                 "parts": (), "processed_end": 0, "committed_frames": 0, "committed_end_sample": 0,
                 "available_end_sample": 0, "pending": None, "carry": None, "seams": (), "edge": None,
                 "locks": (), "complete": False, "total_frames": None}
    video = state["video"]
    if (video["width"], video["height"]) != (width, height):
        raise ValueError(f"[{APPEND_LABEL}] 第 {segment['index'] + 1} 段畫面 {width}x{height} 與前段 "
                         f"{video['width']}x{video['height']} 不同；請讓各段使用相同解析度。")
    clock = state["audio_clock"]
    if (None if clock is None else (clock["sample_rate"], clock["channels"])) != layout:
        raise TimelineError(APPEND_LABEL, "E_AUDIO_LAYOUT")
    pending = state["pending"]
    if ((pending["frame_range"] if pending else None) != ([a - x_in, a] if x_in else None)
            or state["processed_end"] != a or state["committed_frames"] != a - x_in):
        raise RuntimeError(f"[{APPEND_LABEL}] state 的游標 / pending 區間與本段接縫不符（內部錯誤）。請重新執行整個迴圈。")
    held_frames = pending["frames"].load(APPEND_LABEL) if x_in else None
    held_audio = pending["audio"].load(APPEND_LABEL) if (x_in and pending["audio"] is not None) else None

    session = _open_session(state["spill_dir"])
    need = 2 * (e - a) * width * height * FFV1_BYTES_PER_PIXEL
    if shutil.disk_usage(session.path).free < need:
        raise TimelineError(APPEND_LABEL, "E_DISK", gib=need / 2 ** 30)

    out, padded, pending_audio, window = None, 0, None, 0
    end_sample = state["committed_end_sample"]
    if clock is not None:
        sr = clock["sample_rate"]
        wave = audio[0]
        wave_start = sample_index(origin, sr, fps)
        commit_end = sample_index(e - x_out, sr, fps)
        mix = (sample_index(a - x_in, sr, fps), sample_index(a, sr, fps)) if x_in else None
        held = (pending["sample_range"][0], held_audio) if held_audio is not None else None
        out, padded = flush_audio(end_sample, held, wave, wave_start, mix if held else None, commit_end, curve,
                                  segment["is_last"])
        end_sample += out.shape[1]
        window = max(mix[1] - mix[0] if mix else 0, sr // 100)
        if x_out:
            # pending 永遠從 commit_end 起；本段音訊沒撐到 commit_end 時 pending 為空
            hi = min(sample_index(e, sr, fps), wave_start + wave.shape[1]) if end_sample == commit_end else commit_end
            pending_audio = wave[:, commit_end - wave_start:max(hi, commit_end) - wave_start].clone()

    start_frame = state["committed_frames"]
    count = x_in + (e - a - x_out)
    new_parts, new_pending = [], None
    try:
        if count:
            path = os.path.join(session.path, f"seg_{segment['index']}_{uuid.uuid4().hex}.mkv")
            chunks = _committed_chunks(frames, held_frames, x_in, segment["overlap_in"], length - x_out,
                                       chunk_frames(width, height))
            _write_video_part(path, chunks, width, height, fps, count, check_interrupt)
            new_parts.append(SpillPart(session, path, "video", start_frame, count, (width, height, fps)))
        if out is not None and out.shape[1]:
            path = os.path.join(session.path, f"seg_{segment['index']}_{uuid.uuid4().hex}.wav")
            _write_wav(path, out, clock["sample_rate"])
            new_parts.append(SpillPart(session, path, "audio", state["committed_end_sample"], out.shape[1],
                                       (clock["sample_rate"], clock["channels"])))
        if x_out:
            # crossfade 保留區落盤：快取住的每個 state 不再各自握著一份 RGB（1344×768×22 幀 ≈ 65 MiB）
            tail = quantize(frames[length - x_out:])
            path = os.path.join(session.path, f"pending_{segment['index']}_{uuid.uuid4().hex}.rgb")
            dtype = _write_raw(path, tail)
            new_pending = {"frame_range": [e - x_out, e], "sample_range": None, "audio": None,
                           "frames": PendingPart(session, path, "pending_video", e - x_out, x_out,
                                                 (width, height, fps), tail.shape, dtype)}
            new_parts.append(new_pending["frames"])
            if pending_audio is not None and pending_audio.shape[1]:
                path = os.path.join(session.path, f"pending_{segment['index']}_{uuid.uuid4().hex}.f32")
                dtype = _write_raw(path, pending_audio)
                new_pending["sample_range"] = [commit_end, commit_end + pending_audio.shape[1]]
                new_pending["audio"] = PendingPart(session, path, "pending_audio", commit_end, pending_audio.shape[1],
                                                   (clock["sample_rate"], clock["channels"]), pending_audio.shape, dtype)
                new_parts.append(new_pending["audio"])
        available_end = new_pending["sample_range"][1] if (new_pending and new_pending["sample_range"]) else end_sample
        seam = _seam_report(segment, state, frames, out, padded, curve, x_in, window)
        # planned：計畫要鎖；verified：Append 的漂移檢查通過（外部 sampler 拿掉 metadata 時不會走到這裡）
        seam["latent_lock"] = {"planned": bool(segment["latent_locks"]), "verified": lock_drift is not None,
                               "drift": lock_drift}
        last_audio = out[:, -1].clone() if (out is not None and out.shape[1]) else \
            (state["edge"]["audio"] if state["edge"] else None)
        spill = tuple(p for p in new_parts if not isinstance(p, PendingPart))
        new_state = {**state, "assembly_id": segment["assembly_id"], "next_index": segment["index"] + 1,
                     "parts": state["parts"] + spill, "processed_end": e,
                     "committed_frames": start_frame + count, "committed_end_sample": end_sample,
                     "available_end_sample": available_end, "pending": new_pending, "carry": carry,
                     "seams": state["seams"] + (seam,),
                     "edge": {"thumb": _thumb(frames[length - 1]), "audio": last_audio},
                     "locks": _merge_locks(state["locks"], segment["audio_locks"]),
                     "complete": segment["is_last"], "total_frames": e if segment["is_last"] else None}
        _check_invariants(new_state, state)
    except BaseException:
        for part in new_parts:
            part.remove()
        raise
    if padded:
        print(format_message(APPEND_LABEL, "W_AUDIO_PAD", samples=padded))
    return new_state


# ---------- Assemble ----------

def _contiguous(parts, end):
    cursor = 0
    for part in parts:
        if part.start != cursor:
            return False
        cursor += part.count
    return cursor == end


def _concat_list(directory, paths):
    path = os.path.join(directory, f"list_{uuid.uuid4().hex}.ffconcat")
    with open(path, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for p in paths:
            f.write("file '" + os.path.abspath(p).replace("'", "'\\''") + "'\n")
    return path


def _reserve_output(filename_prefix, ext):
    # resolve_output_path 只看既有檔名；並行執行可能算出同一個 counter，用 O_EXCL 佔位檔搶名
    for _ in range(100):
        path = resolve_output_path(filename_prefix, ext)
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"[{ASSEMBLE_LABEL}] 無法保留輸出檔名（{filename_prefix}）。請改用其他 filename_prefix。")


def _run(cmd, check_interrupt):
    proc = subprocess.Popen(resolve_ffmpeg_cmd(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    stderr = _StderrTail(proc.stderr)
    try:
        _supervise(lambda: _exited(proc), check_interrupt)
    finally:
        _stop(proc)
        stderr.join()
    return proc.returncode, stderr


MASTER_RATE = 48000


def _lock_pcm(lock, fps, directory, label):
    """鎖音區間的 canonical PCM（48 kHz 雙聲道 float32 WAV）：整個區間一次重取樣，樣本數 = Q₄₈ₖ 端點差。"""
    path = resolve_timeline_source(lock["ref"])
    if opaque_fingerprint(path) != lock["fingerprint"]:
        raise TimelineError(label, "E_SOURCE_CHANGED")
    num, den = fps
    count = sample_index(lock["end"], MASTER_RATE, fps) - sample_index(lock["start"], MASTER_RATE, fps)
    # -ss 以容器起點為 0、素材時間以第一個 audio PTS 為 0
    origin = Fraction((lock["audio_start_us"] or 0) - (lock["format_start_us"] or 0), 1_000_000)
    wave = decode_audio(path, origin + Fraction(lock["in"] * den, num), origin, count, MASTER_RATE, 2, label)[0]
    out = os.path.join(directory, f"lock_{lock['anchor']}_{uuid.uuid4().hex}.wav")
    _write_wav(out, wave, MASTER_RATE)
    return out


def master_filter(locks, total_samples, has_base):
    """生成音軌（一次重取樣到 48 kHz）以 sample 精確的 atrim 切開，鎖音區間換成原聲 PCM，再依序 concat。

    輸入編號：有生成音軌時它是 0、鎖音 PCM 從 1 起；沒有時鎖音從 0 起、底層改用靜音。
    """
    lead = (f"[0:a]aresample={MASTER_RATE},aformat=sample_fmts=flt:channel_layouts=stereo,asetpts=N/SR/TB,"
            if has_base else f"anullsrc=r={MASTER_RATE}:cl=stereo,aformat=sample_fmts=flt,")
    chains = [lead + f"apad=whole_len={total_samples},atrim=end_sample={total_samples}[base]"]
    gaps, cursor = [], 0
    for lo, hi in locks:
        if lo > cursor:
            gaps.append((cursor, lo))
        cursor = hi
    if cursor < total_samples:
        gaps.append((cursor, total_samples))
    chains.append(f"[base]asplit={len(gaps)}" + "".join(f"[b{k}]" for k in range(len(gaps))) if gaps
                  else "[base]anullsink")
    first_lock = 1 if has_base else 0
    pieces = []
    for _, kind, k in sorted([(lo, "gap", k) for k, (lo, _) in enumerate(gaps)]
                             + [(lo, "lock", k) for k, (lo, _) in enumerate(locks)]):
        if kind == "gap":
            chains.append(f"[b{k}]atrim=start_sample={gaps[k][0]}:end_sample={gaps[k][1]},asetpts=N/SR/TB[g{k}]")
            pieces.append(f"[g{k}]")
        else:
            chains.append(f"[{first_lock + k}:a]aformat=sample_fmts=flt:channel_layouts=stereo,asetpts=N/SR/TB[l{k}]")
            pieces.append(f"[l{k}]")
    chains.append("".join(pieces) + f"concat=n={len(pieces)}:v=0:a=1[master]")
    return ";".join(chains)


def build_master(state, directory, check_interrupt):
    """lock_audio=original：生成音軌在鎖音區間換回原聲 → 48 kHz float32 WAV 母帶（AAC 之前可逐 sample 驗收）。"""
    fps = tuple(state["video"]["fps"])
    total = sample_index(state["total_frames"], MASTER_RATE, fps)
    locks = [(sample_index(lock["start"], MASTER_RATE, fps), sample_index(lock["end"], MASTER_RATE, fps))
             for lock in state["locks"]]
    scratch, clock = [], state["audio_clock"]
    try:
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
        if clock is not None:
            scratch.append(_concat_list(directory, [p.path for p in state["parts"] if p.kind == "audio"]))
            cmd += ["-f", "concat", "-safe", "0", "-i", scratch[-1]]
        for lock in state["locks"]:
            scratch.append(_lock_pcm(lock, fps, directory, ASSEMBLE_LABEL))
            cmd += ["-i", scratch[-1]]
        master = os.path.join(directory, f"master_{uuid.uuid4().hex}.wav")
        cmd += ["-filter_complex", master_filter(locks, total, clock is not None), "-map", "[master]",
                "-c:a", "pcm_f32le", master]
        returncode, stderr = _run(cmd, check_interrupt)
        if returncode != 0:
            stderr.print_tail(ASSEMBLE_LABEL)
            _unlink(master)
            raise RuntimeError(f"[{ASSEMBLE_LABEL}] 鎖音母帶組裝失敗（exit {returncode}），請查看上方 stderr 輸出。")
        return master, total
    finally:
        for path in scratch:
            _unlink(path)


def assemble(state, filename_prefix, ext, pix_fmt, encoder_args, override, check_interrupt, lock_audio="generated"):
    """parts → 最終影片；回傳 (輸出路徑, report dict)。不刪 spill part。只驗證 state 自洽（不接 plan）。

    override：audio_input() 的結果或 None，整條取代內部音軌，以它自己的 sample rate 補齊 / 截斷到 Q(F)。
    lock_audio="original" 且 state 有鎖音時，母帶在鎖音區間換回原聲（override 優先）。
    """
    check_state(state, ASSEMBLE_LABEL)
    if not state["complete"]:
        raise TimelineError(ASSEMBLE_LABEL, "E_SEQUENCE", expected="最後一段", actual=state["next_index"])
    if not _final_ok(state):
        raise RuntimeError(f"[{ASSEMBLE_LABEL}] state 不變量檢查失敗（內部錯誤）。請重新執行整個迴圈。")
    parts = state["parts"]  # 編碼期間持有全部 SpillPart，避免中途被回收
    video = [p for p in parts if p.kind == "video"]
    audio = [p for p in parts if p.kind == "audio"]
    clock = state["audio_clock"]
    v = state["video"]
    fps = tuple(v["fps"])
    total = state["total_frames"]
    if not (_contiguous(video, total) and _contiguous(audio, state["committed_end_sample"] if clock else 0)
            and all(p.intact() for p in parts)
            and all(p.layout == (v["width"], v["height"], fps) for p in video)
            and all(p.layout == (clock["sample_rate"], clock["channels"]) for p in audio)):
        raise TimelineError(ASSEMBLE_LABEL, "E_SPILL_MISSING")

    num, den = fps
    scratch = []
    try:
        scratch.append(_concat_list(state["spill_dir"], [p.path for p in video]))
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", scratch[-1]]
        source, sample_rate = "none", None
        if override is not None:
            wave, sample_rate = override
            scratch.append(os.path.join(state["spill_dir"], f"override_{uuid.uuid4().hex}.wav"))
            _write_wav(scratch[-1], wave, sample_rate)
            cmd += ["-i", scratch[-1]]
            source = "override"
        elif lock_audio == "original" and state["locks"]:
            master, _ = build_master(state, state["spill_dir"], check_interrupt)
            scratch.append(master)
            cmd += ["-i", master]
            source, sample_rate = "locked_original", MASTER_RATE
        elif clock is not None:
            scratch.append(_concat_list(state["spill_dir"], [p.path for p in audio]))
            cmd += ["-f", "concat", "-safe", "0", "-i", scratch[-1]]
            source, sample_rate = "internal", clock["sample_rate"]
        samples = None if sample_rate is None else sample_index(total, sample_rate, fps)
        # PTS 依累積幀號重建（MKV 的 1/1000 time base 會讓 part 時長有捨入誤差）；FFmpeg 8 不接受
        # `-r` 配 passthrough，所以幀率由 fps filter 定（時間戳已精確，不會補幀 / 丟幀），色彩 tag
        # 也必須由 setparams 寫在 frame 上——encoder 的 -color_* 會被 frame 屬性覆蓋
        cmd += ["-map", "0:v:0", "-vf",
                f"settb=1/{num},setpts=N*{den},fps={num}/{den},scale=out_color_matrix=bt709:out_range=tv,"
                f"format={pix_fmt},setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"]
        if samples is not None:
            cmd += ["-map", "1:a:0", "-af", f"asetpts=N/SR/TB,apad=whole_len={samples},atrim=end_sample={samples}"]
        cmd += [*encoder_args, "-pix_fmt", pix_fmt, "-enc_time_base:v", f"1/{num}", "-fps_mode:v", "passthrough",
                "-frames:v", str(total)]
        if samples is not None:
            cmd += ["-c:a", "aac", "-b:a", "192k"]

        output = _reserve_output(filename_prefix, ext)
        tmp = output + ".partial"
        try:
            returncode, stderr = _run(cmd + ["-f", "mov" if ext == ".mov" else "mp4", tmp], check_interrupt)
            if returncode != 0:
                stderr.print_tail(ASSEMBLE_LABEL)
                raise RuntimeError(f"[{ASSEMBLE_LABEL}] 最終編碼失敗（exit {returncode}），請查看上方 stderr 輸出。")
            streams = _probe_streams(tmp, "mov", "codec_type,nb_read_packets," + ",".join(COLOR_TAGS))
            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            audio_count = sum(s.get("codec_type") == "audio" for s in streams)
            if not (len(video_streams) == 1 and video_streams[0].get("nb_read_packets") == str(total)
                    and audio_count == (0 if samples is None else 1)
                    and all(video_streams[0].get(k) == tag for k, tag in COLOR_TAGS.items())):
                raise RuntimeError(f"[{ASSEMBLE_LABEL}] 輸出驗證失敗（幀數 / 音軌 / 色彩標記不符）：{streams}。請改用其他 codec 後重試。")
            os.replace(tmp, output)
        except BaseException:
            _unlink(tmp)
            _unlink(output)
            raise
    finally:
        for path in scratch:
            _unlink(path)

    lock_reason = ("audio_override" if override is not None and state["locks"] else
                   None if source == "locked_original" else "no_locks" if not state["locks"] else "disabled")
    report = {"version": 2, "assembly_id": state["assembly_id"],
              "session": state["session"], "frames": total, "fps": [num, den],
              "width": v["width"], "height": v["height"], "pix_fmt": pix_fmt,
              "color": {"assumed": "BT.709", "conversion": "full-range RGB → limited-range YUV",
                        "tags": "bt709/bt709/bt709/tv"},
              "audio": {"source": source, "sample_rate": sample_rate, "samples": samples},
              "padded_samples": sum(s["padded_samples"] for s in state["seams"]),
              "seams": list(state["seams"]),
              "parts": {"video": len(video), "audio": len(audio)},
              "effective": [{"feature": "lock_audio", "requested": lock_audio == "original",
                             "applied": source == "locked_original", "reason": lock_reason}]}
    return output, report
