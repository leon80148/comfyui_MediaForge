"""MF_LoadSegmentFrames — 依 MF_SEGMENT 從來源影片解出本段的參考畫面與音訊。

時間軸幀 f ↔ 來源時間 (f − origin_frame)·den/num 秒（相對來源第一個 video 幀）。FFmpeg 先
accurate seek 到段首、再以 fps=num/den 重取樣：同 fps 的來源逐幀精確，不同 fps 則是決定性
的最近幀取樣。既有 MF_LoadVideoFrames 沒有區間 seek，所以另立這個節點。
"""
import subprocess
from fractions import Fraction

import numpy as np
import torch

from ..utils.ffmpeg import resolve_ffmpeg_cmd
from ..utils.timeline_messages import TimelineError
from ..utils.timeline_plan import sample_index
from ..utils.timeline_source import ffmpeg_input_args, opaque_fingerprint, probe_source, resolve_timeline_source
from ..utils.video_io import MAX_DECODE_BYTES

NODE_LABEL = "Load Segment Frames"
RESIZE_MODES = ("none", "fit", "fill", "stretch")
MAX_AUDIO_RATE = 384000
MAX_AUDIO_CHANNELS = 32
AUDIO_PREROLL = Fraction(1)  # 秒；遠大於常見音訊封包（AAC ≈ 21 ms、PCM-in-MKV ≈ 85 ms）
SHORTFALL_MODES = ("error", "hold_last", "black")


def _scale_filter(resize, width, height):
    if resize == "fit":
        return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if resize == "fill":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    if resize == "stretch":
        return f"scale={width}:{height},setsar=1"
    return None


def _run(cmd, what):
    proc = subprocess.run(resolve_ffmpeg_cmd(cmd), capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-30:])
        raise RuntimeError(f"[{NODE_LABEL}] FFmpeg {what}失敗 (exit {proc.returncode})：\n{tail}")
    return proc.stdout


def _decode_video(path, seek, count, fps, size, scale):
    num, den = fps
    vf = f"fps={num}/{den}" + (f",{scale}" if scale else "")
    # -map 與 probe 一樣取第一條 video；不指定時 FFmpeg 可能選到 default 的其他串流
    out = _run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{float(seek):.9f}"] + ffmpeg_input_args(path)
               + ["-map", "0:v:0", "-vf", vf, "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
               "解碼畫面")
    w, h = size
    n = len(out) // (w * h * 3)
    arr = np.frombuffer(out[: n * w * h * 3], dtype=np.uint8).reshape(n, h, w, 3)
    return torch.from_numpy(arr.copy()).float() / 255.0


def _decode_audio(path, seek, count, sample_rate, channels):
    # 輸入端 -ss 依 video 關鍵幀跳：音訊可能晚一個封包才開始，第一個關鍵幀之前的音訊甚至整段讀不到；
    # raw PCM 又沒有時間戳。所以往前多解一段（接近開頭就乾脆從檔頭讀），aresample 依 PTS 補齊
    # 空白（音軌晚於起點時也靠它），再以 sample 數精確裁掉前段
    pre = min(seek, AUDIO_PREROLL)
    af = (f"aresample=async=1:first_pts=0,atrim=start_sample={round(pre * sample_rate)},asetpts=PTS-STARTPTS,"
          f"apad=whole_len={count},atrim=end_sample={count}")
    head = ["-ss", f"{float(seek - pre):.9f}"] if seek > pre else []
    out = _run(["ffmpeg", "-v", "error", "-nostdin"] + head + ffmpeg_input_args(path)
               + ["-map", "0:a:0", "-af", af, "-ac", str(channels), "-ar", str(sample_rate), "-f", "f32le", "pipe:1"],
               "解碼音訊")
    pcm = np.frombuffer(out, dtype=np.float32).reshape(-1, channels).T
    return torch.from_numpy(pcm.copy()).unsqueeze(0)


class MF_LoadSegmentFrames:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "segment": ("MF_SEGMENT",),
                "resize": (list(RESIZE_MODES), {"default": "none", "tooltip": "none 原尺寸；fit 等比留邊；fill 等比裁切；stretch 拉伸。"}),
                "width": ("INT", {"default": 1344, "min": 2, "max": 7680, "step": 2}),
                "height": ("INT", {"default": 768, "min": 2, "max": 4320, "step": 2}),
                "shortfall": (list(SHORTFALL_MODES), {
                    "default": "error",
                    "tooltip": "來源不足時：停止、延用最近一幀或補黑；完全沒有畫面時不能延用。",
                }),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "INT")
    RETURN_NAMES = ("frames", "audio", "frame_count")
    FUNCTION = "load"
    CATEGORY = "MediaForge/Video"

    def load(self, segment, resize, width, height, shortfall):
        window = segment.get("source_window") if isinstance(segment, dict) else None
        if window is None:
            raise ValueError(f"[{NODE_LABEL}] 本段沒有來源影片；請在 Edit Timeline 選擇 video。")
        path = resolve_timeline_source(window["source_ref"])
        if opaque_fingerprint(path) != window["source_fingerprint"]:
            raise TimelineError(NODE_LABEL, "E_SOURCE_CHANGED")
        info = probe_source(path)
        fps = tuple(segment["fps"])
        length = segment["length"]
        scale = _scale_filter(resize, width, height)
        size = (width, height) if scale else info["display_size"]
        if length * size[0] * size[1] * 3 > MAX_DECODE_BYTES:
            raise ValueError(f"[{NODE_LABEL}] 本段 {length} 幀 × {size[0]}x{size[1]} 超過解碼上限；請改用 resize 縮小。")

        start = window["start_frame"] - window["origin_frame"]  # 來源第 0 幀起算
        head = min(length, max(0, -start))  # 段頭落在來源開始之前的幀數
        first = start + head
        # -ss 以檔案起點為 0；時間軸錨在第一個 video 幀，兩者在 mpegts 等格式可能不同
        num, den = fps
        offset = Fraction((info["source_start_us"] or 0) - (info["format_start_us"] or 0), 1_000_000)
        seek = offset + Fraction(max(first, 0) * den, num)
        frames = _decode_video(path, seek, length - head, fps, size, scale) if length > head else torch.zeros(0, size[1], size[0], 3)
        missing = length - head - frames.shape[0]
        if head or missing:
            if shortfall == "error" or (shortfall == "hold_last" and frames.shape[0] == 0):
                raise TimelineError(NODE_LABEL, "E_SOURCE_SHORTFALL", segment=segment["index"] + 1)
            blank = torch.zeros(1, size[1], size[0], 3)
            head_fill = frames[:1] if shortfall == "hold_last" else blank
            tail_fill = frames[-1:] if shortfall == "hold_last" else blank
            frames = torch.cat([head_fill.expand(head, -1, -1, -1), frames, tail_fill.expand(missing, -1, -1, -1)])

        audio = None
        if info["has_audio"]:
            sr, channels = info["audio_sample_rate"], info["audio_channels"]
            total = sample_index(start + length, sr, fps) - sample_index(start, sr, fps)
            silent_head = sample_index(first, sr, fps) - sample_index(start, sr, fps)
            need = total - silent_head
            # 取樣率 / 聲道數來自檔案本身，惡意或損毀的 metadata 不能撐爆記憶體
            if not (0 < sr <= MAX_AUDIO_RATE and 0 < channels <= MAX_AUDIO_CHANNELS) or total * channels * 4 > MAX_DECODE_BYTES:
                raise ValueError(f"[{NODE_LABEL}] 來源音訊規格異常（{sr} Hz × {channels} ch），拒絕解碼。請改用其他來源或先重新轉檔。")
            waveform = _decode_audio(path, seek, need, sr, channels) if need > 0 else torch.zeros(1, channels, 0)
            # seek 超過來源尾端時 FFmpeg 可能一個樣本都不輸出，apad 無從補起
            waveform = torch.cat([torch.zeros(1, channels, silent_head), waveform,
                                  torch.zeros(1, channels, need - waveform.shape[-1])], dim=-1)
            audio = {"waveform": waveform, "sample_rate": sr}
        return (frames, audio, frames.shape[0])


NODE_CLASS_MAPPINGS = {"MF_LoadSegmentFrames": MF_LoadSegmentFrames}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_LoadSegmentFrames": "📽️ Load Segment Frames"}
