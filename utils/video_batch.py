"""Video Batch：把影片一批一批送過任意 IMAGE 節點，同一個 prompt、同一批 node ID 重複執行。

Start 每批以無狀態的 ffmpeg 解一段絕對時間窗口；End 在 check_lazy_status 裡驅動批次，逐批落盤，
最後組成成片。工作狀態放在 dynprompt 上，因為 --cache-none 下節點 instance 每次都是新的。
"""
import itertools
import json
import math
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from fractions import Fraction
from typing import NamedTuple

from . import segment_spill
from .encoder import assemble_settings
from .ffmpeg import resolve_ffmpeg_cmd
from .output_path import resolve_output_path
from .segment_spill import APPEND_LABEL, ASSEMBLE_LABEL, RAM_HEADROOM
from .timeline_media import MAX_AUDIO_CHANNELS, MAX_AUDIO_RATE, decode_audio, decode_video_window, scale_filter
from .timeline_plan import H3_GRID, length_ok, sample_index
from .timeline_source import PROBE_TIMEOUT_SEC, ffmpeg_input_args, opaque_fingerprint, probe_source, resolve_timeline_source
from .video_io import chunk_frames

START_CLASS = "MF_StartVideoBatch"
END_CLASS = "MF_EndVideoBatch"
START_LABEL = "Start Video Batch"
END_LABEL = "End Video Batch"

DEFAULT_FRAMES_PER_BATCH = 64
# Start can run before End, so both read these from the prompt alone. A linked value would not be known yet.
END_WIDGETS = ("frames_per_batch", "overlap_frames", "fps", "max_frames", "keep_source_audio", "filename_prefix", "codec", "crf", "preset")
H3_PREFIX = "MiniMaxH3"
# Containers whose keyframe index makes the window seek land correctly. TS and MPEG have none.
VERIFIED_FORMATS = {"mov", "matroska", "avi"}
RESIZE_MODES = ("none", "fit", "fill", "stretch")
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
WIDE_GAMUT_PRIMARIES = {"bt2020", "smpte431", "smpte432"}
# The WAV writer stores sizes in 32-bit RIFF fields, headers included.
WAV_PAYLOAD_LIMIT = 2 ** 32 - 64
# Decoded bytes, the array built from them, and the copy made while writing the WAV.
AUDIO_RAM_COPIES = 3
# How big the film may get next to its FFV1 parts, for the disk estimate: ProRes and near-lossless CRF are about the same, ordinary CRF far smaller.
OUTPUT_RATIO_NEAR_LOSSLESS = 1.0
OUTPUT_RATIO_LOSSY = 0.25
NEAR_LOSSLESS_CRF = 10
AUDIO_FIX_HINT = "請先把音軌轉成 48 kHz 雙聲道（例如 ffmpeg -i 影片 -c:v copy -ac 2 -ar 48000 -c:a aac 輸出.mp4），或關閉 keep_source_audio。"


def _is_link(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int)


class JobGraph(NamedTuple):
    end: str
    starts: tuple
    body: frozenset
    outputs: tuple
    frames_link: list
    audio_link: list
    audio_mode: str  # "none" | "external" | "batch"
    has_h3_body: bool
    start_audio: frozenset  # Starts whose audio output is linked: only those decode audio


class JobParams(NamedTuple):
    B: int
    K: int
    fps: float
    max_frames: int
    keep_source_audio: bool
    filename_prefix: str
    codec: str
    crf: int
    preset: str


def _walk(edges, roots, stop=()):
    seen, stack = set(), list(roots)
    while stack:
        cur = stack.pop()
        if cur in seen or cur in stop:
            continue
        seen.add(cur)
        stack.extend(edges.get(cur, ()))
    return seen


def plan_graph(prompt, class_mappings):
    """驗證原始圖並算出工作範圍（SPEC §4）。"""
    class_of = {node_id: node["class_type"] for node_id, node in prompt.items()}
    parents = {node_id: [v[0] for v in node.get("inputs", {}).values() if _is_link(v)] for node_id, node in prompt.items()}
    children = {}
    for node_id, sources in parents.items():
        for source in sources:
            children.setdefault(source, []).append(node_id)

    ends = sorted(n for n, c in class_of.items() if c == END_CLASS)
    all_starts = sorted(n for n, c in class_of.items() if c == START_CLASS)
    if not ends:
        raise ValueError(f"[{START_LABEL}] 需要接到一個 {END_LABEL}：批次處理的成片由它輸出。請把處理後的畫面接到 {END_LABEL} 的 frames。")
    if len(ends) > 1:
        raise ValueError(f"[{END_LABEL}] 一個工作流只能有一個 {END_LABEL}，目前有 {len(ends)} 個。請刪掉多餘的。")
    end_id = ends[0]
    end_inputs = prompt[end_id]["inputs"]

    upstream = _walk(parents, parents[end_id])
    orphans = [s for s in all_starts if s not in upstream]
    if orphans:
        raise ValueError(f"[{END_LABEL}] 有 {START_LABEL} 沒有接到 {END_LABEL}（節點 {', '.join(orphans)}）。請接上或刪掉。")
    frames_link = end_inputs.get("frames")
    frames_upstream = _walk(parents, [frames_link[0]]) if _is_link(frames_link) else set()
    if not any(s in frames_upstream for s in all_starts):
        raise ValueError(f"[{END_LABEL}] frames 必須來自 {START_LABEL}（可以經過處理節點）。請檢查接線。")

    after = _walk(children, children.get(end_id, ()))
    body = _walk(children, [c for s in all_starts for c in children.get(s, ())], stop={end_id}) - after
    leaks = sorted(n for n in after if set(parents[n]) & (body | set(all_starts)))
    if leaks:
        raise ValueError(f"[{END_LABEL}] {END_LABEL} 下游的節點不能直接讀批次內的畫面（節點 {', '.join(leaks)}），"
                         "它只會拿到其中一批。請改接 final_video_path。")
    for node_id in sorted(body):
        class_def = class_mappings[class_of[node_id]]
        if hasattr(class_def, "GET_SCHEMA") and class_def.GET_SCHEMA().loop_boundary:
            raise ValueError(f"[{END_LABEL}] 批次內不能放 Start / End Loop（節點 {node_id}）。請移到批次外。")
    controlled = body | set(all_starts)
    for start_id in all_starts:
        for name, value in prompt[start_id]["inputs"].items():
            if _is_link(value) and _walk(parents, [value[0]]) & controlled:
                raise ValueError(f"[{START_LABEL}] {name} 不能依賴批次內的節點（節點 {start_id}）。請改接批次外的來源。")

    audio_link = end_inputs.get("audio") if _is_link(end_inputs.get("audio")) else None
    audio_mode = "none" if audio_link is None else "batch" if audio_link[0] in controlled else "external"
    outputs = tuple(sorted(n for n in body if getattr(class_mappings[class_of[n]], "OUTPUT_NODE", False)))
    start_audio = frozenset(v[0] for node in prompt.values() for v in node.get("inputs", {}).values()
                            if _is_link(v) and v[1] == 1 and v[0] in all_starts)
    return JobGraph(end=end_id, starts=tuple(all_starts), body=frozenset(body), outputs=outputs, frames_link=frames_link,
                    audio_link=audio_link, audio_mode=audio_mode,
                    has_h3_body=any(class_of[n].startswith(H3_PREFIX) for n in body), start_audio=start_audio)


def job_params(end_inputs):
    for name in END_WIDGETS:
        if _is_link(end_inputs.get(name)):
            raise ValueError(f"[{END_LABEL}] {name} 不能接線，請直接在節點上設定。")
    return JobParams(B=end_inputs.get("frames_per_batch", DEFAULT_FRAMES_PER_BATCH), K=end_inputs.get("overlap_frames", 0),
                     fps=end_inputs.get("fps", 0.0), max_frames=end_inputs.get("max_frames", 0),
                     keep_source_audio=end_inputs.get("keep_source_audio", True), filename_prefix=end_inputs.get("filename_prefix"),
                     codec=end_inputs.get("codec"), crf=end_inputs.get("crf"), preset=end_inputs.get("preset"))


def _h3_neighbours(b):
    base, step = 5, 17
    if b <= base:
        return (base,)
    below = base + (b - base) // step * step
    return (below, below + step)


def check_params(b, k, has_h3_body):
    if b < 1:
        raise ValueError(f"[{END_LABEL}] frames_per_batch 必須至少是 1，收到 {b}。")
    if k < 0 or k > b // 2:
        raise ValueError(f"[{END_LABEL}] overlap_frames 必須介於 0 和 frames_per_batch 的一半（{b // 2}）之間，收到 {k}。")
    if has_h3_body and not length_ok(H3_GRID, b):
        options = "、".join(str(x) for x in _h3_neighbours(b))
        raise ValueError(f"[{END_LABEL}] 批次內有 MiniMax H3 節點，frames_per_batch 必須符合 17n+5，{b} 不符合。請改成 {options}。")


def fps_fraction(value):
    """widget 的小數 fps → (num, den)。"""
    fraction = Fraction(value).limit_denominator(1001)
    return fraction.numerator, fraction.denominator


def window_start(index, b, k):
    return index * (b - k)


def estimate_batches(n, b, k):
    """只用來顯示進度；實際的最後一批由解碼結果決定（SPEC §5）。"""
    return max(1, math.ceil((n - k) / (b - k)))


def _seam(frames):
    return {"kind": "crossfade", "frames": frames} if frames else {"kind": "cut"}


def make_segment(index, b, k, length, is_last, fps, job_id, prev_assembly_id):
    """第 index 批的 MF_SEGMENT 欄位，直接給 segment_spill.check_sequence / append_segment 用。"""
    first = window_start(index, b, k)
    if index > 0 and length <= k:
        raise RuntimeError(f"[{END_LABEL}] 第 {index + 1} 批只有 {length} 幀，沒有超過重疊的 {k} 幀（前一批確認過後面還有畫面）。"
                           "來源可能在處理中改變了，請重新執行。")
    overlap_in = k if index > 0 else 0
    return {"index": index, "length": length, "overlap_in": overlap_in,
            "emit_interval": [first + overlap_in, first + length],
            "seam_in": _seam(k if index > 0 else 0), "seam_out": _seam(k if not is_last else 0),
            "fps": list(fps), "is_last": is_last, "assembly_id": f"{job_id}:{index}", "prev_assembly_id": prev_assembly_id,
            "audio_locks": [], "latent_locks": []}


def display_size(width, height, sar, rotation):
    """coded 軸先做 SAR 正規化，再套 rotation：濾鏡輸出、預配置張量與 frame_bytes 都用這個尺寸。"""
    num, den = sar
    if num != den:
        width = round(width * num / den)
        width += width % 2
    return (height, width) if rotation in (90, 270) else (width, height)


def _probe_format(path, stream):
    cmd = (["ffprobe", "-v", "error"] + ffmpeg_input_args(path)
           + ["-select_streams", f"v:{stream}", "-show_entries", "format=format_name:stream=color_transfer,color_primaries", "-of", "json"])
    proc = subprocess.run(resolve_ffmpeg_cmd(cmd), capture_output=True, timeout=PROBE_TIMEOUT_SEC, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"[{START_LABEL}] 無法讀取 {os.path.basename(path)}：\n" + proc.stderr.decode("utf-8", errors="replace")[-500:])
    info = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
    stream = (info.get("streams") or [{}])[0]
    return {"format_name": info.get("format", {}).get("format_name", ""), "color_transfer": stream.get("color_transfer"),
            "color_primaries": stream.get("color_primaries")}


def _first_frame_us(path, stream):
    """容器沒寫 start_time 時的時間原點（很短的 MKV 就沒有）：直接讀第一幀的 PTS。"""
    cmd = (["ffprobe", "-v", "error"] + ffmpeg_input_args(path)
           + ["-select_streams", f"v:{stream}", "-read_intervals", "%+#1", "-show_entries", "frame=pts_time", "-of", "csv=p=0"])
    proc = subprocess.run(resolve_ffmpeg_cmd(cmd), capture_output=True, timeout=PROBE_TIMEOUT_SEC, check=False, text=True, errors="replace")
    try:
        return round(float(proc.stdout.split()[0].strip(",")) * 1_000_000)
    except (IndexError, ValueError):
        return None


def _check_ram(nbytes):
    import psutil
    available = psutil.virtual_memory().available
    if nbytes > available * RAM_HEADROOM:
        raise ValueError(f"[{START_LABEL}] 這批畫面約需 {nbytes / 2 ** 30:.1f} GiB RAM，目前可用 {available / 2 ** 30:.1f} GiB。"
                         "請調低 End Video Batch 的 frames_per_batch，或用 resize 縮小畫面。")


def batch_text(number, estimate):
    return f"第 {number} 批" if estimate is None else f"第 {number} / 約 {max(number, estimate)} 批"


def check_encoder(width, height, pix_fmt, encoder_args, label):
    """幾幀的試編碼（SPEC §8）：奇數尺寸配 yuv420p、NVENC 開不起來，都在第 0 批就知道，不是整片跑完才失敗。"""
    # testsrc is RGB like the real input. The color source is YUV, and FFmpeg 6.1 silently shrinks it to an even size.
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"testsrc=s={width}x{height}:r=30", "-frames:v", "2",
           "-vf", f"scale=out_color_matrix=bt709:out_range=tv,format={pix_fmt}", *encoder_args, "-pix_fmt", pix_fmt, "-f", "null", "-"]
    proc = subprocess.run(resolve_ffmpeg_cmd(cmd), capture_output=True, timeout=PROBE_TIMEOUT_SEC, check=False)
    if proc.returncode == 0:
        return
    if width % 2 or height % 2:
        raise ValueError(f"[{label}] 處理後的畫面是 {width}x{height}，這個 codec 需要偶數寬高。"
                         f"請調整處理節點的縮放倍率，或在 {END_LABEL} 前加一個縮放節點調成偶數（例如 {width + width % 2}x{height + height % 2}）。")
    detail = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
    raise RuntimeError(f"[{label}] 這個 codec 試編碼失敗（{width}x{height}、{pix_fmt}）。請改用其他 codec，例如 h264 (libx264)。\n{detail}")


def _same_disk(a, b):
    return os.stat(a).st_dev == os.stat(b).st_dev


def check_disk_space(temp_dir, output_dir, temp_bytes, output_bytes):
    """這是估計，不是保證（SPEC §8）。同一顆磁碟要一起放得下：分段等成片寫完才刪。"""
    needs = ([(temp_dir, temp_bytes + output_bytes)] if _same_disk(temp_dir, output_dir)
             else [(temp_dir, temp_bytes), (output_dir, output_bytes)])
    for path, need in needs:
        free = shutil.disk_usage(path).free
        if need > free:
            raise ValueError(f"[{END_LABEL}] 磁碟空間可能不夠：{path} 估計還需要 {need / 2 ** 30:.1f} GiB，目前剩 {free / 2 ** 30:.1f} GiB。"
                             "請清出空間，或用 max_frames、resize 減少要處理的量。")


def check_output_ram(batch_bytes, held_bytes):
    """第 0 批之後依實際輸出尺寸再估一次（SPEC §8）。手上這批在下一批開始前會還回來，要算進可用量。"""
    import psutil
    available = psutil.virtual_memory().available + held_bytes
    if batch_bytes > available * RAM_HEADROOM:
        raise ValueError(f"[{END_LABEL}] 處理後每批約需 {batch_bytes / 2 ** 30:.1f} GiB RAM，目前可用約 {available / 2 ** 30:.1f} GiB。"
                         "請調低 frames_per_batch，或用 resize 縮小畫面。")


def _relabelled(step, *args, **kwargs):
    """共用引擎的訊息標的是 Append / Assemble Segments，這個工作流裡沒有那些節點。"""
    try:
        return step(*args, **kwargs)
    except (ValueError, RuntimeError) as e:
        text = str(e)
        for label in (APPEND_LABEL, ASSEMBLE_LABEL):
            if text.startswith(f"[{label}]"):
                raise RuntimeError(f"[{END_LABEL}]" + text[len(label) + 2:]) from e
        raise


def _check_wav_payload(samples, channels):
    nbytes = samples * channels * 4
    if nbytes > WAV_PAYLOAD_LIMIT:
        raise ValueError(f"[{END_LABEL}] 音軌太大：{samples} samples × {channels} 聲道約 {nbytes / 2 ** 30:.1f} GiB，暫存 WAV 的上限約 4 GiB。{AUDIO_FIX_HINT}")


def check_audio_budget(samples, channels, rate):
    """來源音軌解得下嗎（SPEC §7）。解碼前就擋，不要跑完全片才發現。"""
    if not (0 < (rate or 0) <= MAX_AUDIO_RATE and 0 < (channels or 0) <= MAX_AUDIO_CHANNELS):
        raise ValueError(f"[{END_LABEL}] 來源音軌規格異常（{rate} Hz × {channels} 聲道），拒絕解碼。{AUDIO_FIX_HINT}")
    _check_wav_payload(samples, channels)
    import psutil
    need = samples * channels * 4 * AUDIO_RAM_COPIES
    available = psutil.virtual_memory().available
    if need > available * RAM_HEADROOM:
        raise ValueError(f"[{END_LABEL}] 解碼音軌約需 {need / 2 ** 30:.1f} GiB RAM，目前可用 {available / 2 ** 30:.1f} GiB。{AUDIO_FIX_HINT}")


def fit_override(wave, rate, fps, frames):
    """整條取代的音軌先裁到影片長度再檢查 WAV 上限：比影片長的部分本來就不會寫出去。"""
    wave = wave[:, :sample_index(frames, rate, fps)]
    _check_wav_payload(wave.shape[1], wave.shape[0])
    return wave, rate


@dataclass
class BatchSource:
    path: str
    fingerprint: str
    fps: tuple
    size: tuple
    filters: list
    offset_sec: Fraction
    stream: int
    has_audio: bool
    audio_sample_rate: int
    audio_channels: int
    duration_us: int

    def read(self, first, b, limit, check_interrupt=None):
        """第 first 幀起最多 b 幀，補到 b 幀；回傳 (frames, 有效幀數 L, 後面是否還有畫面)。

        多要一幀來判斷片尾，不信 metadata。limit 是從 first 起還能用幾幀（max_frames），None 表示不限。
        """
        want = b + 1 if limit is None else min(b + 1, limit)
        width, height = self.size
        _check_ram((b + 1) * width * height * 12)
        import torch
        # Padding in the same buffer: otherwise a short last batch holds two copies just as the job is nearly done.
        buffer = torch.empty(max(want, b), height, width, 3, dtype=torch.float32)
        frames = decode_video_window(self.path, first, want, self.fps, self.offset_sec, self.size, self.filters, self.stream,
                                     START_LABEL, check_interrupt, into=buffer)
        got = frames.shape[0]
        if got == 0:
            raise ValueError(f"[{START_LABEL}] {os.path.basename(self.path)} 從第 {first} 幀起沒有畫面。請確認影片沒有損毀、不是空檔案。")
        if got >= b:
            return frames[:b], b, got > b
        buffer[got:b] = buffer[got - 1]
        return buffer[:b], got, False

    def check_unchanged(self):
        if opaque_fingerprint(self.path) != self.fingerprint:
            raise RuntimeError(f"[{START_LABEL}] {os.path.basename(self.path)} 在處理途中改變了，已停止，避免成片混到兩支影片。請重新執行。")

    def read_audio(self, samples, check_interrupt=None):
        """整條來源音訊 [1,C,n]，零點是第一個 video 幀（SPEC §7）。samples=None 解到 EOF，超過預算就報錯，不當成 EOF。"""
        rate, channels = self.audio_sample_rate, self.audio_channels
        if samples is None:
            import psutil
            budget = int(min(WAV_PAYLOAD_LIMIT, psutil.virtual_memory().available * RAM_HEADROOM / AUDIO_RAM_COPIES)) // (channels * 4)
        else:
            check_audio_budget(samples, channels, rate)
            budget = samples
        self.check_unchanged()
        count = budget + 1 if samples is None else samples
        wave = decode_audio(self.path, self.offset_sec, self.offset_sec, count, rate, channels, channels, START_LABEL, check_interrupt)
        self.check_unchanged()
        if wave.shape[-1] > budget:
            raise ValueError(f"[{END_LABEL}] {os.path.basename(self.path)} 的音軌超過可用的上限（{budget} samples）。{AUDIO_FIX_HINT}")
        return wave


def open_source(path, resize, width, height, target_fps, label):
    """讀 metadata 並擋掉尚未驗證的來源（SPEC §6）。target_fps 是 (num, den) 或 None（沿用來源）。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"[{label}] 找不到影片：{path}")
    info = probe_source(path)
    fmt = _probe_format(path, info["video_ordinal"])
    name = os.path.basename(path)
    if not VERIFIED_FORMATS & set(fmt["format_name"].split(",")):
        raise ValueError(f"[{label}] {name} 的格式（{fmt['format_name']}）尚未通過批次解碼驗證：TS、MPG 沒有關鍵幀索引，無法精準分批。"
                         f"請先轉成 MP4 或 MKV，例如：ffmpeg -i \"{name}\" -c copy output.mkv")
    if fmt["color_transfer"] in HDR_TRANSFERS:
        raise ValueError(f"[{label}] {name} 是 HDR 影片（{fmt['color_transfer']}），批次節點尚未支援。請先轉成 SDR。")
    if fmt["color_primaries"] in WIDE_GAMUT_PRIMARIES:
        raise ValueError(f"[{label}] {name} 是廣色域影片（{fmt['color_primaries']}），批次節點尚未支援。請先轉成 BT.709。")
    video_start = info["source_start_us"] if info["source_start_us"] is not None else _first_frame_us(path, info["video_ordinal"])
    # Without a container start time, -copyts -start_at_zero shifts nothing, so the timeline starts at 0.
    container_start = info["format_start_us"] if info["format_start_us"] is not None else 0
    if video_start is None or video_start < container_start:
        raise ValueError(f"[{label}] 無法判斷 {name} 的影片起點。請先轉檔，例如：ffmpeg -i \"{name}\" -c copy output.mkv")
    fps = tuple(target_fps) if target_fps else tuple(info["fps"] or ())
    if not fps:
        raise ValueError(f"[{label}] 無法判斷 {name} 的 fps。請在 {END_LABEL} 指定 fps。")
    sar = tuple(info["sar"])
    size = display_size(info["width"], info["height"], sar, info["rotation_deg"])
    filters = [f"scale={size[0]}:{size[1]},setsar=1"] if sar[0] != sar[1] else []
    scale = scale_filter(resize, width, height)
    if scale:
        filters.append(scale)
        size = (width, height)
    return BatchSource(path=path, fingerprint=opaque_fingerprint(path), fps=fps, size=size, filters=filters,
                       offset_sec=Fraction(video_start - container_start, 1_000_000), stream=info["video_ordinal"],
                       has_audio=info["has_audio"], audio_sample_rate=info["audio_sample_rate"], audio_channels=info["audio_channels"],
                       duration_us=info["video_duration_us"] if info["video_duration_us"] is not None else info["duration_us"])


def check_interrupt():
    import comfy.model_management
    comfy.model_management.throw_exception_if_processing_interrupted()


def _check_core(execution_list, dynprompt):
    """這個協定直接用到 core 內部；缺任何一項就在開工時明確報錯，不要跑到一半才默默算錯。"""
    from comfy_execution.caching import BasicCache, NullCache

    required = ("output_cache", "execution_cache", "pendingNodes", "add_strong_link", "get_cache", "is_cached", "is_staged_node_blocked")
    missing = [] if execution_list is not None else ["EXECUTION_LIST"]
    missing += [name for name in required if execution_list is not None and not hasattr(execution_list, name)]
    if not hasattr(dynprompt, "ephemeral_parents"):
        missing.append("ephemeral_parents")
    if execution_list is not None and not isinstance(execution_list.output_cache, (BasicCache, NullCache)):
        missing.append(type(execution_list.output_cache).__name__)
    if missing:
        raise RuntimeError(f"[{END_LABEL}] 此 ComfyUI 版本未通過相容檢查（缺 {', '.join(missing)}）。驗證過的版本是 core 0.42.5。")


# State of the job with parts on disk that has not finished. Small: parts are paths, and no frames are held here.
_unfinished = None


def reclaim_unfinished():
    """失敗或 Stop 留下的分段，在下一個工作開始時明確刪掉（SPEC §8）。finalizer 只是兜底：快取還握著舊 prompt。"""
    global _unfinished
    state, _unfinished = _unfinished, None
    if state is not None:
        segment_spill.discard(state)


class VideoBatchJob:
    """一個 prompt 的批次工作狀態。放在 dynprompt 上；不引用 dynprompt，避免循環引用拖延分段的回收。"""

    def __init__(self, prompt, class_mappings):
        self.graph = plan_graph(prompt, class_mappings)
        self.params = job_params(prompt[self.graph.end]["inputs"])
        check_params(self.params.B, self.params.K, self.graph.has_h3_body)
        self.target_fps = fps_fraction(self.params.fps) if self.params.fps > 0 else None
        self.id = uuid.uuid4().hex
        self.prepared = False
        self.sources = {}
        self.index = 0
        self.requested = False
        self.records = {}
        self.last_committed = -1
        self.finished = False
        self.state = None
        self.children = set()
        self.seen_ephemeral = 0
        self.frames_written = 0
        self.external_audio = None
        self.auto_audio = None
        self.source_audio = {}
        self.estimated_frames = None
        self.estimated_batches = None
        self.output_dir = None
        self.output_ratio = OUTPUT_RATIO_NEAR_LOSSLESS
        self.bar = None
        self.result = None

    @property
    def fps(self):
        return next(iter(self.sources.values())).fps

    # ---------- 來源預檢 ----------

    def _fetch(self, link, waiter, execution_list):
        # Receivers are per waiter. Without a main cache, asking for what another waiter already received would run the node again.
        for received in execution_list.execution_cache.values():
            entry = received.get(link[0])
            if entry is not None:
                return entry
        execution_list.add_strong_link(link[0], link[1], waiter)
        return execution_list.get_cache(link[0], waiter)

    def _resolve_inputs(self, start_id, waiter, execution_list, dynprompt):
        values, ready = {}, True
        for name, raw in dynprompt.get_node(start_id)["inputs"].items():
            if not _is_link(raw):
                values[name] = raw
                continue
            entry = self._fetch(raw, waiter, execution_list)
            if entry is None or entry.outputs is None:
                ready = False
                continue
            output = entry.outputs[raw[1]]
            if len(output) != 1 or type(output[0]).__name__ == "ExecutionBlocker":
                raise ValueError(f"[{START_LABEL}] {name} 必須是單一個值。請檢查接到它的節點。")
            values[name] = output[0]
        return values if ready else None

    @staticmethod
    def _single_audio(value):
        """AUDIO 輸出（core 的 list）→ audio_input() 的結果。"""
        if len(value) != 1:
            raise ValueError(f"[{END_LABEL}] audio 必須是單一條音訊，收到 {len(value)} 條（list）。請只接一條。")
        if type(value[0]).__name__ == "ExecutionBlocker":
            raise RuntimeError(f"[{END_LABEL}] audio 被上游節點阻擋，沒有輸出音訊。請調整該節點的條件，或拔掉 audio。")
        if value[0] is None:
            raise ValueError(f"[{END_LABEL}] audio 是空的，上游節點沒有輸出音訊。請換一個來源，或拔掉 audio。")
        return segment_spill.audio_input(value[0], END_LABEL)

    def _fetch_external_audio(self, waiter, execution_list):
        link = self.graph.audio_link
        entry = self._fetch(link, waiter, execution_list)
        if entry is None or entry.outputs is None:
            return None
        return self._single_audio(entry.outputs[link[1]])

    def _estimate_length(self):
        """metadata 估的總幀數與批數，只用來提早報錯和顯示進度。未知時是 None。"""
        num, den = self.fps
        estimates = [round(Fraction(s.duration_us * num, den * 1_000_000)) for s in self.sources.values() if s.duration_us is not None]
        if self.params.max_frames:
            estimates.append(self.params.max_frames)
        if estimates:
            self.estimated_frames = min(estimates)
            self.estimated_batches = estimate_batches(self.estimated_frames, self.params.B, self.params.K)

    def _plan_audio(self):
        """決定成片音軌從哪來（SPEC §7 的表），並在模型開跑前把做不到的情況報出來。"""
        for start_id in self.graph.start_audio:
            if not self.sources[start_id].has_audio:
                raise ValueError(f"[{START_LABEL}] {os.path.basename(self.sources[start_id].path)} 沒有音軌，不能使用 audio 輸出。請拔掉這條線，或換一支有音軌的影片。")
        if self.graph.audio_mode == "none" and self.params.keep_source_audio:
            with_audio = [start_id for start_id, source in self.sources.items() if source.has_audio]
            if len(with_audio) > 1:
                listing = "、".join(os.path.basename(self.sources[s].path) for s in with_audio)
                raise ValueError(f"[{END_LABEL}] {listing} 都有音軌，無法判斷成片要用哪一條。請把要用的音訊接到 audio，或關閉 keep_source_audio。")
            self.auto_audio = with_audio[0] if with_audio else None
        estimated = self.estimated_frames
        for start_id in {*self.graph.start_audio, *([self.auto_audio] if self.auto_audio else [])}:
            source = self.sources[start_id]
            samples = 0 if estimated is None else sample_index(estimated, source.audio_sample_rate or 0, self.fps)
            check_audio_budget(samples, source.audio_channels, source.audio_sample_rate)
        if self.external_audio is not None and estimated is not None:
            fit_override(*self.external_audio, self.fps, estimated)

    @staticmethod
    def _source_path(values):
        if values.get("video_path"):
            return str(values["video_path"])
        if not values.get("video"):
            raise FileNotFoundError(f"[{START_LABEL}] 還沒有選影片。請上傳或選一支影片，或接 video_path。")
        try:
            # Nodes 2.0 also lists other folders' files, as "name [output]". Same path rules, in that folder.
            import folder_paths
            name, folder = folder_paths.annotated_filepath(values["video"])
            return resolve_timeline_source(name, folder)
        except (FileNotFoundError, ValueError) as e:
            raise type(e)(f"[{START_LABEL}] {e}") from e

    def prepare(self, waiter, execution_list, dynprompt):
        """任何 Start 公開畫面前，先解析所有來源的路徑並做 metadata 檢查。回傳 False 表示 waiter 要等上游的值。

        另一個 Start 的 video_path 可能要等其他節點算出來；讓目前的節點等它，而不是先把畫面放出去讓模型開跑。
        """
        if self.prepared:
            return True
        _check_core(execution_list, dynprompt)
        resolved = {s: self._resolve_inputs(s, waiter, execution_list, dynprompt) for s in self.graph.starts}
        external = self.graph.audio_mode == "external"
        # Fixed on the job from here on, and read before any Start lets the model run.
        if external and self.external_audio is None:
            self.external_audio = self._fetch_external_audio(waiter, execution_list)
        if any(values is None for values in resolved.values()) or (external and self.external_audio is None):
            if not execution_list.is_staged_node_blocked():
                raise RuntimeError(f"[{START_LABEL}] 來源的輸入還沒就緒，卻沒有可等待的節點（內部錯誤）。請重新執行。")
            return False
        self.sources = {start_id: open_source(self._source_path(values), values.get("resize", "none"), values.get("width", 0),
                                              values.get("height", 0), self.target_fps, START_LABEL)
                        for start_id, values in resolved.items()}
        if len({source.fps for source in self.sources.values()}) > 1:
            listing = "、".join(f"{os.path.basename(s.path)}（{s.fps[0] / s.fps[1]:.3f}）" for s in self.sources.values())
            raise ValueError(f"[{END_LABEL}] 影片的 fps 不同：{listing}。請在 {END_LABEL} 的 fps 填一個共同的值。")
        self._estimate_length()
        self._plan_audio()
        self.prepared = True
        return True

    # ---------- Start ----------

    def read_batch(self, start_id):
        index = self.index
        source = self.sources[start_id]
        source.check_unchanged()
        first = window_start(index, self.params.B, self.params.K)
        limit = self.params.max_frames - first if self.params.max_frames else None
        frames, length, more = source.read(first, self.params.B, limit, check_interrupt)
        self.records[start_id] = (index, length, more)
        return frames, self.start_audio(start_id, index), source.fps[0] / source.fps[1], frames.shape[0]

    def start_audio(self, start_id, index):
        """第 index 批的來源音訊 [Q(F), Q(F+B))，不足補靜音；audio 輸出沒接線時不解碼。"""
        if start_id not in self.graph.start_audio:
            return None
        source = self.sources[start_id]
        rate = source.audio_sample_rate
        if start_id not in self.source_audio:
            # Some containers can only be read from the head, so decoding per batch would cost quadratic time.
            limit = sample_index(self.params.max_frames, rate, self.fps) if self.params.max_frames else None
            self.source_audio[start_id] = source.read_audio(limit, check_interrupt)
        whole = self.source_audio[start_id]
        first = window_start(index, self.params.B, self.params.K)
        a, b = sample_index(first, rate, self.fps), sample_index(first + self.params.B, rate, self.fps)
        part = whole[..., a:b]
        # A copy: a body node that edits its input must not change what the overlap of the next batch reads.
        wave = whole.new_zeros(1, whole.shape[1], b - a)
        wave[..., :part.shape[-1]] = part
        return {"waveform": wave, "sample_rate": rate}

    # ---------- End ----------

    def _controlled_children(self, dynprompt):
        """body 展開出的所有節點。所有權是永久的：固定 ID 的 child 每批都會重新展開。"""
        parents = dynprompt.ephemeral_parents
        if len(parents) != self.seen_ephemeral:
            roots = self.graph.body
            for node_id in itertools.islice(parents, self.seen_ephemeral, None):
                current = node_id
                while current in parents:
                    current = parents[current]
                    if current in roots:
                        self.children.add(node_id)
                        break
            self.seen_ephemeral = len(parents)
        return self.children

    def _check_child_audio(self, dynprompt):
        """Start 只為工作流上看得到的 audio 連線解碼。展開後才接上的 child 會拿到 None，不能讓它默默做出沒有音訊的結果。

        每批都要看：固定 ID 的 child 可能到後面的批次才接上。
        """
        for node_id in self.children:
            for value in dynprompt.get_node(node_id)["inputs"].values():
                if _is_link(value) and value[1] == 1 and value[0] in self.graph.starts and value[0] not in self.graph.start_audio:
                    raise RuntimeError(f"[{END_LABEL}] 節點 {node_id} 是展開後才要讀 {START_LABEL} 的 audio，工作流上沒有這條線，所以它拿不到音訊。"
                                       f"請把 {START_LABEL} 的 audio 接到工作流裡的節點。")

    def _release_literals(self, dynprompt):
        """寫進 child 的張量會一直留在動態圖上；child 每批換新 ID 時，等於每一批都留一份。這批跑完就用不到了。"""
        for node_id in self.children:
            info = dynprompt.ephemeral_prompt.get(node_id)
            inputs = info.get("inputs", {}) if info else {}
            kept = {name: value for name, value in inputs.items() if value is None or isinstance(value, (bool, int, float, str)) or _is_link(value)}
            if len(kept) != len(inputs):
                # A copy, because the expansion may hand back this dict next batch. That dict also keeps core's fingerprint, which _forget can no longer reach.
                info.pop("is_changed", None)
                dynprompt.ephemeral_prompt[node_id] = {**info, "inputs": kept}

    def _forget(self, execution_list, dynprompt):
        """清掉上一批的輸出，讓同一批 node ID 重新執行。"""
        from comfy_execution.caching import HierarchicalCache, NullCache
        from comfy_execution.progress import get_progress_state

        cache = execution_list.output_cache
        children = self._controlled_children(dynprompt)
        if not isinstance(cache, NullCache):
            for node_id in (*self.graph.starts, *self.graph.body, *children):
                owner = cache._get_cache_for(node_id) if isinstance(cache, HierarchicalCache) else cache
                if owner is None:
                    continue
                key = owner.cache_key_set.get_data_key(node_id)
                owner.cache.pop(key, None)
                for table in ("used_generation", "timestamps", "children"):
                    if hasattr(owner, table):
                        getattr(owner, table).pop(key, None)
                if node_id in children:
                    # Its key and fingerprint were computed from the last batch's literals. A re-expanded child needs new ones,
                    # and core keeps the fingerprint in two places, one of them on a node dict an expansion may reuse.
                    owner.cache_key_set.keys.pop(node_id, None)
                    owner.is_changed_cache.is_changed.pop(node_id, None)
                    dynprompt.ephemeral_prompt.get(node_id, {}).pop("is_changed", None)
                if execution_list.is_cached(node_id):
                    raise RuntimeError(f"[{END_LABEL}] 清除節點 {node_id} 的上一批輸出失敗（此 ComfyUI 版本未通過相容檢查）。")
        # A finished child would otherwise stay in every progress broadcast for the rest of the job.
        registry = get_progress_state()
        for child in children:
            registry.nodes.pop(child, None)

    def _say(self, text):
        from server import PromptServer
        server = PromptServer.instance
        server.send_progress_text(text, self.graph.end, server.client_id)

    def _show_progress(self, done):
        """以批為單位，永遠留一格給組裝：只有成品驗證通過才算滿（SPEC §10）。"""
        import comfy.utils
        if self.bar is None:
            self.bar = comfy.utils.ProgressBar(1, node_id=self.graph.end)
        self.bar.update_absolute(done, max(done, self.estimated_batches or done) + 1)

    def _request(self, end_id, execution_list, dynprompt):
        self._say(batch_text(self.index + 1, self.estimated_batches))
        if self.index > 0:
            self._forget(execution_list, dynprompt)
        # Every Start, even one a lazy branch skips this batch: its length and end-of-file are needed either way.
        for start_id in self.graph.starts:
            execution_list.add_strong_link(start_id, 0, end_id)
        for node_id in self.graph.outputs:
            execution_list.add_strong_link(node_id, None, end_id)
        link = self.graph.frames_link
        execution_list.add_strong_link(link[0], link[1], end_id)
        if self.graph.audio_mode == "batch":
            execution_list.add_strong_link(self.graph.audio_link[0], self.graph.audio_link[1], end_id)
        self.requested = True

    def _images(self, value):
        """body 的輸出（可能是 list）→ 一個 [B,H,W,3] 張量。"""
        number = self.index + 1
        if not value:
            raise RuntimeError(f"[{END_LABEL}] 第 {number} 批沒有拿到畫面。請檢查 frames 的來源節點。")
        for item in value:
            if type(item).__name__ == "ExecutionBlocker":
                raise RuntimeError(f"[{END_LABEL}] 第 {number} 批被上游節點阻擋，沒有輸出畫面。批次流程中不能跳過某一批，請調整該節點的條件。")
            if not hasattr(item, "ndim") or item.ndim != 4 or item.shape[-1] != 3:
                raise RuntimeError(f"[{END_LABEL}] 第 {number} 批的 frames 必須是 [幀數,高,寬,3] 的 RGB IMAGE，"
                                   f"收到 {tuple(getattr(item, 'shape', ()))}。若有 alpha 請先轉成 RGB。")
            if not item.is_floating_point():
                raise RuntimeError(f"[{END_LABEL}] 第 {number} 批的 frames 必須是 0–1 的浮點數 IMAGE，收到 {item.dtype}。")
        total = sum(int(item.shape[0]) for item in value)
        if total != self.params.B:
            raise RuntimeError(f"[{END_LABEL}] 第 {number} 批送進 {self.params.B} 幀，處理後變成 {total} 幀。批次節點要求處理節點不改變幀數，"
                               "否則音畫會不同步。改變幀數的節點（例如插幀）請放到 End Video Batch 之後。")
        import torch
        images = value[0] if len(value) == 1 else torch.cat(value)
        # In blocks: torch.isfinite allocates about 1.75 times its input, on the device holding it (measured at 4K: +10 GiB).
        step = chunk_frames(images.shape[2], images.shape[1])
        if not all(torch.isfinite(images[i:i + step]).all() for i in range(0, images.shape[0], step)):
            raise RuntimeError(f"[{END_LABEL}] 第 {number} 批的畫面含 NaN 或無限大，上游節點可能失敗了。")
        return images

    def _batch_audio(self, end_id, execution_list):
        """本批的音訊，只有 audio 接受控節點時才有（SPEC §7）。"""
        if self.graph.audio_mode != "batch":
            return None
        link = self.graph.audio_link
        entry = execution_list.get_cache(link[0], end_id)
        if entry is None or entry.outputs is None:
            raise RuntimeError(f"[{END_LABEL}] 第 {self.index + 1} 批沒有拿到 audio（此 ComfyUI 版本未通過相容檢查）。")
        audio = self._single_audio(entry.outputs[link[1]])
        clock = None if self.state is None else self.state["audio_clock"]
        if clock is not None and (clock["sample_rate"], clock["channels"]) != (audio[1], audio[0].shape[0]):
            raise ValueError(f"[{END_LABEL}] 第 {self.index + 1} 批音訊是 {audio[1]} Hz {audio[0].shape[0]} 聲道，前面是 {clock['sample_rate']} Hz {clock['channels']} 聲道。"
                             "每一批的音訊規格要一致。")
        return audio

    def _check_first_batch(self, images):
        """實際輸出尺寸出來後的早期檢查（SPEC §8）：不要整片跑完才發現編不出來。"""
        height, width = images.shape[1:3]
        ext, pix_fmt, encoder_args = assemble_settings(self.params.codec, self.params.crf, self.params.preset)
        self.output_dir = os.path.dirname(resolve_output_path(self.params.filename_prefix, ext))
        near_lossless = ext == ".mov" or self.params.crf < NEAR_LOSSLESS_CRF
        self.output_ratio = OUTPUT_RATIO_NEAR_LOSSLESS if near_lossless else OUTPUT_RATIO_LOSSY
        check_encoder(width, height, pix_fmt, encoder_args, END_LABEL)
        check_output_ram(self.params.B * width * height * 12, images.nbytes if images.device.type == "cpu" else 0)

    def _check_disk(self):
        """依已寫入的實際大小更新全片估計。每批都看：第一批可能是黑畫面，估得太低。"""
        done = self.state["committed_frames"]
        if self.estimated_frames is None or not done:
            return
        per_frame = sum(part.size for part in self.state["parts"]) / done
        check_disk_space(self.state["spill_dir"], self.output_dir, max(self.estimated_frames - done, 0) * per_frame,
                         max(self.estimated_frames, done) * per_frame * self.output_ratio)

    def _commit(self, end_id, execution_list):
        records = [self.records.get(start_id) for start_id in self.graph.starts]
        if any(record is None or record[0] != self.index for record in records):
            raise RuntimeError(f"[{END_LABEL}] 第 {self.index + 1} 批的來源沒有全部執行（此 ComfyUI 版本未通過相容檢查）。")
        length = min(record[1] for record in records)
        is_last = not all(record[2] for record in records)
        link = self.graph.frames_link
        entry = execution_list.get_cache(link[0], end_id)
        if entry is None or entry.outputs is None:
            raise RuntimeError(f"[{END_LABEL}] 第 {self.index + 1} 批沒有拿到 frames（此 ComfyUI 版本未通過相容檢查）。")
        images = self._images(entry.outputs[link[1]])
        segment = make_segment(self.index, self.params.B, self.params.K, length, is_last, self.fps, self.id,
                               None if self.state is None else self.state["assembly_id"])
        segment_spill.check_sequence(self.state, segment)
        if self.index == 0:
            self._check_first_batch(images)
        # Linear: crossfading two takes of the same source with equal power would add about 3 dB.
        self.state = _relabelled(segment_spill.append_segment, self.state, segment, images[:length], self._batch_audio(end_id, execution_list),
                                 curve="linear", carry=None, check_interrupt=check_interrupt, lock_drift=None)
        global _unfinished
        _unfinished = self.state
        self.frames_written = segment["emit_interval"][1]
        self._check_disk()
        self._show_progress(self.index + 1)

    def _advance(self, end_id, execution_list, dynprompt):
        controlled = {*self.graph.starts, *self.graph.body, *self._controlled_children(dynprompt)}
        received = execution_list.execution_cache.get(end_id, {})
        for node_id in [n for n in received if n in controlled]:
            del received[node_id]
        self._release_literals(dynprompt)
        self.requested = False
        self.last_committed = self.index
        if self.state["complete"]:
            self.finished = True
        else:
            self.index += 1

    def drive(self, end_id, execution_list, dynprompt):
        """End 的 check_lazy_status。回傳後若 End 還被擋著，core 會讓它 PENDING，等依賴跑完再進來一次。"""
        if not self.prepare(end_id, execution_list, dynprompt):
            return
        while not self.finished:
            check_interrupt()
            if not self.requested:
                self._request(end_id, execution_list, dynprompt)
            # Includes children expanded just now: an async side output must not cross into the next batch.
            controlled = {*self.graph.starts, *self.graph.body, *self._controlled_children(dynprompt)}
            for node_id in [n for n in execution_list.pendingNodes if n in controlled]:
                execution_list.add_strong_link(node_id, None, end_id)
            if execution_list.is_staged_node_blocked():
                return
            self._check_child_audio(dynprompt)
            self._commit(end_id, execution_list)
            self._advance(end_id, execution_list, dynprompt)

    def _final_override(self):
        """整條取代成片的音軌：外部音訊，或 N 確定後才解的自動原音。"""
        total = self.state["total_frames"]
        if self.external_audio is not None:
            return fit_override(*self.external_audio, self.fps, total)
        if self.auto_audio is None:
            return None
        source = self.sources[self.auto_audio]
        rate = source.audio_sample_rate
        wave = self.source_audio.get(self.auto_audio)
        if wave is None:
            wave = source.read_audio(sample_index(total, rate, self.fps), check_interrupt)
        return fit_override(wave[0], rate, self.fps, total)

    def _show_done(self):
        import comfy.model_management
        try:
            self.bar.update_absolute(self.bar.total, self.bar.total)
        except comfy.model_management.InterruptProcessingException:
            # The file is out and verified, so Stop can't take it back. Raising cleared the request, and it still applies to whatever runs next.
            comfy.model_management.interrupt_current_processing()
        self._say("完成")

    def finish(self):
        global _unfinished
        if self.result is not None:
            # Core runs End again when a downstream expansion links to it and no cache holds its output. The film is made and its parts are gone.
            return self.result
        if not self.finished:
            raise RuntimeError(f"[{END_LABEL}] 只完成到第 {self.last_committed + 1} 批就進入組裝（此 ComfyUI 版本未通過相容檢查）。")
        check_interrupt()
        self._say("正在組裝成片…")
        ext, pix_fmt, encoder_args = assemble_settings(self.params.codec, self.params.crf, self.params.preset)
        path, report = _relabelled(segment_spill.assemble, self.state, self.params.filename_prefix, ext, pix_fmt, encoder_args, self._final_override(),
                                   check_interrupt=check_interrupt, lock_audio="generated")
        segment_spill.discard(self.state)
        _unfinished = None
        # The executor's cache keeps this prompt, and so this job, alive until the next prompt starts.
        self.source_audio.clear()
        self.external_audio = None
        report.update({"node": END_LABEL, "frames_per_batch": self.params.B, "overlap_frames": self.params.K, "batches": self.last_committed + 1,
                       "sources": [os.path.basename(s.path) for s in self.sources.values()]})
        self.result = (path, json.dumps(report, ensure_ascii=False))
        self._show_done()
        return self.result


def get_job(dynprompt):
    job = getattr(dynprompt, "mf_video_batch_job", None)
    if job is None:
        import nodes
        reclaim_unfinished()
        job = VideoBatchJob(dynprompt.get_original_prompt(), nodes.NODE_CLASS_MAPPINGS)
        dynprompt.mf_video_batch_job = job
    return job
