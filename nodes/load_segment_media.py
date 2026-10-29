"""MF_LoadSegmentMedia — 從 MF_SEGMENT 取出本段的第 N 個素材（圖 / 影片切片 / 音訊），給 loop 路徑自己接線。

H3 請用 🎬 Encode H3 Segment（它會照 core 的規則一次編好所有素材）；這個節點給其他模型取素材，
例如 Wan / LTX 的參考圖或錨定畫面。kind=lock 在這裡只是「素材音訊」，不會做 latent 鎖音。
"""
import torch.nn.functional as F

from ..utils.timeline_media import load_audio_slice, load_image, load_video_slice, scale_filter
from ..utils.timeline_messages import TimelineError
from ..utils.timeline_plan import require_segment

NODE_LABEL = "Load Segment Media"
KINDS = {"picture": "pictures", "video": "videos", "audio": "audios",
         "keyframe": "keyframes", "guide": "guides", "lock": "locks"}
RESIZE_MODES = ("none", "fit", "fill", "stretch")


class MF_LoadSegmentMedia:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "segment": ("MF_SEGMENT",),
                "kind": (list(KINDS), {"default": "picture"}),
                "ordinal": ("INT", {"default": 1, "min": 1, "max": 512, "step": 1,
                                    "tooltip": "本段該類素材的第幾個（1 起算，順序同 Edit Timeline 的標籤表）。"}),
                "resize": (list(RESIZE_MODES), {"default": "none",
                                                "tooltip": "none 原尺寸；fit 等比留邊；fill 等比裁切；stretch 拉伸。"}),
                "width": ("INT", {"default": 1344, "min": 2, "max": 7680, "step": 2}),
                "height": ("INT", {"default": 768, "min": 2, "max": 4320, "step": 2}),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "INT", "INT")
    RETURN_NAMES = ("image", "audio", "frame_idx", "count")
    FUNCTION = "load"
    CATEGORY = "MediaForge/Video"

    def load(self, segment, kind, ordinal, resize, width, height):
        require_segment(segment, ("encode",), NODE_LABEL)
        if kind not in KINDS:
            raise ValueError(f"[{NODE_LABEL}] kind 必須是 {list(KINDS)}，收到 {kind!r}。")
        items = segment["materials"][KINDS[kind]]
        if not 1 <= ordinal <= len(items):
            raise TimelineError(NODE_LABEL, "E_MEDIA_MISSING", segment=segment["index"] + 1, ordinal=ordinal,
                                kind=kind, count=len(items))
        item = items[ordinal - 1]
        fps = tuple(segment["fps"])
        number = segment["index"] + 1
        image = audio = None
        if kind in ("picture", "keyframe"):
            image = load_image(item, NODE_LABEL, max_short_edge=None)
            if resize != "none":
                # 單張圖也走 FFmpeg 會多一次編解碼；這裡直接用 torch 的雙三次縮放
                image = _resize(image, resize, width, height)
        elif kind in ("video", "guide"):
            frames = item["effective_frames"] if kind == "video" else item["frames"]
            scale = scale_filter(resize, width, height)
            image = load_video_slice(item, item["source_start"], frames, fps, NODE_LABEL, number,
                                     scale, (width, height) if scale else None)
            if item["soundtrack"]:
                audio = load_audio_slice(item, item["source_start"], frames, fps, NODE_LABEL,
                                         origin_key="source_start_us")
        else:
            audio = load_audio_slice(item, item["source_start"], item["frames"], fps, NODE_LABEL)
        return (image, audio, item.get("frame_idx", 0), len(items))


def _resize(image, mode, width, height):
    x = image.movedim(-1, 1)
    h, w = x.shape[-2:]
    if mode == "stretch":
        return F.interpolate(x, size=(height, width), mode="bicubic", align_corners=False).clamp(0, 1).movedim(1, -1)
    scale = min(width / w, height / h) if mode == "fit" else max(width / w, height / h)
    nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
    x = F.interpolate(x, size=(nh, nw), mode="bicubic", align_corners=False).clamp(0, 1)
    if mode == "fill":
        top, left = (nh - height) // 2, (nw - width) // 2
        return x[..., top:top + height, left:left + width].movedim(1, -1)
    out = x.new_zeros(x.shape[0], x.shape[1], height, width)
    top, left = (height - nh) // 2, (width - nw) // 2
    out[..., top:top + nh, left:left + nw] = x
    return out.movedim(1, -1)


NODE_CLASS_MAPPINGS = {"MF_LoadSegmentMedia": MF_LoadSegmentMedia}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_LoadSegmentMedia": "🖼️ Load Segment Media"}
