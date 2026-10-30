"""MF_LoadSegmentFrames — 依 MF_SEGMENT 從來源影片解出本段的參考畫面與音訊。

時間軸幀 f ↔ 來源時間 (f − origin_frame)·den/num 秒（相對來源第一個 video 幀）；解碼規則見
utils/timeline_media.py。既有 MF_LoadVideoFrames 沒有區間 seek，所以另立這個節點。
"""
from fractions import Fraction

import torch

from ..utils.timeline_media import decode_audio, decode_video, scale_filter
from ..utils.timeline_messages import TimelineError
from ..utils.timeline_plan import require_segment, sample_index
from ..utils.timeline_source import opaque_fingerprint, probe_source, resolve_timeline_source
from ..utils.video_io import MAX_DECODE_BYTES

NODE_LABEL = "Load Segment Frames"
RESIZE_MODES = ("none", "fit", "fill", "stretch")
MAX_AUDIO_RATE = 384000
MAX_AUDIO_CHANNELS = 32
SHORTFALL_MODES = ("error", "hold_last", "black")


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
        window = require_segment(segment, (), NODE_LABEL)["source_window"]
        if window is None:
            raise ValueError(f"[{NODE_LABEL}] 本段沒有來源影片；請在 Edit Timeline 選擇 video。")
        path = resolve_timeline_source(window["source_ref"])
        if opaque_fingerprint(path) != window["source_fingerprint"]:
            raise TimelineError(NODE_LABEL, "E_SOURCE_CHANGED")
        info = probe_source(path)
        fps = tuple(segment["fps"])
        length = segment["length"]
        scale = scale_filter(resize, width, height)
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
        frames = decode_video(path, seek, length - head, fps, size, scale, NODE_LABEL, info["video_ordinal"]) if length > head else torch.zeros(0, size[1], size[0], 3)
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
            waveform = (decode_audio(path, seek, offset, need, sr, channels, channels, NODE_LABEL) if need > 0
                        else torch.zeros(1, channels, 0))
            # seek 超過來源尾端時 FFmpeg 可能一個樣本都不輸出，apad 無從補起
            waveform = torch.cat([torch.zeros(1, channels, silent_head), waveform,
                                  torch.zeros(1, channels, need - waveform.shape[-1])], dim=-1)
            audio = {"waveform": waveform, "sample_rate": sr}
        return (frames, audio, frames.shape[0])


NODE_CLASS_MAPPINGS = {"MF_LoadSegmentFrames": MF_LoadSegmentFrames}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_LoadSegmentFrames": "📽️ Load Segment Frames"}
