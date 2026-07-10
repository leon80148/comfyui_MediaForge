"""MF_DetectSilence — FFmpeg silencedetect 包裝，回傳 list of silence ranges。

v2.1 ROADMAP Phase 3 — ComfyUI 生態獨有節點。
用途：lecture timelapse / podcast pre-edit / streaming highlight。
搭配 MF_TrimByRanges 可達自動剪片 pipeline。
"""
import os
import re
import subprocess
import tempfile

from ..utils.cache_key import path_fingerprint
from ..utils.ffmpeg import ensure_ffmpeg, probe_audio_duration, probe_duration, resolve_ffmpeg_cmd


# silencedetect 輸出例：
#   [silencedetect @ 0x...] silence_start: 12.345
#   [silencedetect @ 0x...] silence_end: 15.678 | silence_duration: 3.333
SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


class MF_DetectSilence:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                # 任一：STRING path (常用) 或 AUDIO dict (純 audio workflow)
                "audio_source": ("STRING", {"default": "input/sample.mp4"}),
                # 噪音門檻 dB，越負越嚴格（-30dB 比 -50dB 更容易判定為靜音）
                "noise_db": ("FLOAT", {"default": -30.0, "min": -90.0, "max": 0.0, "step": 0.5}),
                # 最短靜音長度（短於此值不算）
                "min_duration_sec": ("FLOAT", {"default": 0.5, "min": 0.05, "max": 60.0, "step": 0.05}),
            },
            "optional": {
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("SILENCE_RANGES", "STRING", "INT")
    RETURN_NAMES = ("ranges", "ranges_json", "count")
    FUNCTION = "detect"
    CATEGORY = "MediaForge/Analysis"
    # 連線型別 SILENCE_RANGES 為 MediaForge 自定，下游 MF_TrimByRanges 吃這個

    def detect(self, audio_source, noise_db, min_duration_sec, audio=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Detect Silence] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        cleanup_tmp = None
        try:
            if audio is not None:
                # 把 AUDIO dict 寫成 tmp wav 後跑 silencedetect — 比 stdin pipe 簡單可靠
                source_path = _audio_dict_to_tmp_wav(audio)
                cleanup_tmp = source_path
            else:
                if not os.path.exists(audio_source):
                    raise FileNotFoundError(f"[Detect Silence] 找不到輸入檔：{audio_source}")
                source_path = audio_source

            cmd = resolve_ffmpeg_cmd([
                "ffmpeg", "-v", "info",  # silencedetect 訊息走 stderr at info level
                "-i", source_path,
                "-af", f"silencedetect=noise={noise_db}dB:d={min_duration_sec}",
                "-vn",
                "-f", "null", "-",
            ])
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                tail = "\n".join((proc.stderr or "").strip().splitlines()[-30:])
                raise RuntimeError(
                    f"[Detect Silence] FFmpeg 失敗 (exit {proc.returncode}):\n{tail}"
                )

            # W1-5：影片以靜音收尾時最後一段沒有 silence_end，probe 時長讓
            # _parse_silence_log 能把它收尾成 [last_start, duration] 而非直接丟棄。
            # R6-2 fix：優先用 audio stream 自己的 duration，不是 container 整體 ——
            # container 是所有 stream 的 max，audio 比 video 短時（audio 提前結束）
            # 用 container 補尾段，會把「container 有、但 audio 已經沒有」的畫面也
            # 判成靜音，下游 mode=remove 連這段畫面都會被剪掉。audio stream duration
            # 缺失時才退階用 container（best-effort、可能長於實際音軌，但總比整段
            # 捨棄好）。
            duration = probe_audio_duration(source_path)
            if duration is None:
                duration = probe_duration(source_path)
            ranges = _parse_silence_log(proc.stderr or "", duration=duration)
        finally:
            if cleanup_tmp:
                try:
                    os.unlink(cleanup_tmp)
                except OSError:
                    pass

        import json
        ranges_json = json.dumps(ranges)
        print(f"[Detect Silence] 偵測到 {len(ranges)} 段靜音 (noise<={noise_db}dB, dur>={min_duration_sec}s)")
        return (ranges, ranges_json, len(ranges))

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # audio 接了 -> audio_source 不會被讀，tensor 本身已參與 ComfyUI 原生 input
        # hash；否則同路徑換內容(mtime 變)要 invalidate cache(W1-13)。
        if kwargs.get("audio") is not None:
            return ""
        return path_fingerprint(kwargs.get("audio_source", ""))


def _parse_silence_log(log, duration=None):
    """Parse silencedetect stderr → list of [start, end]。

    影片以靜音收尾時 FFmpeg 不會印對應的 silence_end（EOF 前沒有「非靜音」事件觸發
    log）。若 caller 傳入 duration（source 檔案總長度）且大於最後一個沒配對的
    silence_start，把它收尾成 [start, duration]；沒傳 duration（呼叫端 probe 失敗）
    或 duration 不合理（<= trailing start）就退回原行為（捨棄該段 + 印警告）。
    """
    starts = [float(m.group(1)) for m in SILENCE_START_RE.finditer(log)]
    ends = [float(m.group(1)) for m in SILENCE_END_RE.finditer(log)]
    n = min(len(starts), len(ends))
    ranges = [[starts[i], ends[i]] for i in range(n)]

    if len(starts) > len(ends):
        trailing_start = starts[-1]
        if duration is not None and duration > trailing_start:
            ranges.append([trailing_start, duration])
            print(
                f"[Detect Silence] 影片以靜音收尾，已用檔案時長補上最後一段："
                f"[{trailing_start:.2f}, {duration:.2f}]"
            )
        else:
            # 影片以靜音結尾 → silencedetect 看不到 end (EOF 未明確收尾)；
            # 提示給 caller 知道結尾還有一段被丟掉的靜音
            print(
                f"[Detect Silence] 注意：偵測到 {len(starts) - len(ends)} 個 silence_start 沒對到 silence_end，"
                "通常代表影片結尾即為靜音，該段已被忽略；若需保留請改用 MF_ProbeMedia 取 duration 補。"
            )
    return ranges


def _audio_dict_to_tmp_wav(audio_dict):
    import wave
    import numpy as np

    waveform = audio_dict.get("waveform")
    sr = int(audio_dict.get("sample_rate") or 0)
    if waveform is None or sr <= 0:
        raise ValueError("[Detect Silence] AUDIO dict 必須含 waveform 與 sample_rate")

    wav = waveform[0].detach().cpu().clamp(-1.0, 1.0).numpy()  # [C, T]
    pcm16 = (wav.T * 32767.0).round().astype(np.int16)

    fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="mf_silence_")
    os.close(fd)
    with wave.open(tmp_path, "wb") as w:
        w.setnchannels(pcm16.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())
    return tmp_path


NODE_CLASS_MAPPINGS = {"MF_DetectSilence": MF_DetectSilence}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_DetectSilence": "🤫 Detect Silence"}
