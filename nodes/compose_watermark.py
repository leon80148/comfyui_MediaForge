"""MF_ComposeWatermark — append watermark op spec into MF_COMPOSE_OPS chain。

Watermark 預設 UX:placement preset + relative_scale + opacity + tile mode +
時間區間。`relative_scale` 跟 tile row/col 計算延後到 ComposeVideo compile 時
做 (因為要用 target_width / target_height、那兩個值要等 ComposeVideo probe
完才知道,尤其當 target_*=0 沿用 source 時)。

跟 OverlayImage 的差別:OverlayImage 是「絕對 x/y + 絕對 scale_w」通用 overlay、
Watermark 是「placement preset + 相對 scale + 透明度 + 時間窗」便利包。
"""
import os

from ..utils.cache_key import path_fingerprint
from ..utils.compose_ops import PLACEMENT_PRESETS


class MF_ComposeWatermark:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image_path": ("STRING", {"default": "input/watermark.png"}),
                "placement": (list(PLACEMENT_PRESETS.keys()), {"default": "bottom_right"}),
                # 相對 frame width 的縮放比例,0.05~0.5;對應 watermark 寬度 = scale * target_width
                "relative_scale": ("FLOAT", {"default": 0.15, "min": 0.05, "max": 0.5, "step": 0.01}),
                # 透明度 0–1 (1 = 不透明)。實作走 colorchannelmixer alpha — 不破壞原 PNG alpha
                "opacity": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.05}),
                "margin_top": ("INT", {"default": 20, "min": 0, "max": 1000, "step": 1}),
                "margin_right": ("INT", {"default": 20, "min": 0, "max": 1000, "step": 1}),
                "margin_bottom": ("INT", {"default": 20, "min": 0, "max": 1000, "step": 1}),
                "margin_left": ("INT", {"default": 20, "min": 0, "max": 1000, "step": 1}),
                # 時間區間:start=end=0 → 全長顯示
                "visible_start_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
                "visible_end_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
            },
            "optional": {
                "overlays": ("MF_COMPOSE_OPS",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE_OPS",)
    RETURN_NAMES = ("overlays",)
    FUNCTION = "add"
    CATEGORY = "MediaForge/Compose"

    def add(self, image_path, placement, relative_scale, opacity,
            margin_top, margin_right, margin_bottom, margin_left,
            visible_start_sec, visible_end_sec,
            overlays=None):
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"[Compose Watermark] 找不到 watermark 圖片:{image_path}")
        if placement not in PLACEMENT_PRESETS:
            raise ValueError(f"[Compose Watermark] 未知 placement={placement!r}")

        # 純粹 op spec、relative_scale / tile / placement 計算留到 ComposeVideo compile 時 resolve
        params = {
            "placement": placement,
            "relative_scale": float(relative_scale),
            "opacity": float(opacity),
            "margin_top": int(margin_top),
            "margin_right": int(margin_right),
            "margin_bottom": int(margin_bottom),
            "margin_left": int(margin_left),
            "visible_start_sec": float(visible_start_sec),
            "visible_end_sec": float(visible_end_sec),
        }

        ops = list(overlays) if overlays else []
        ops.append({
            "type": "watermark",
            "image_path": image_path,
            "params": params,
        })
        return (ops,)

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # C1：ComfyUI IS_CHANGED 收到的 linked 輸入（例如上游 overlays chain）一律
        # 是 None（execution_list=None，見 execution.py IsChangedCache.get），檔案
        # 變更偵測必須由持有 path widget 的節點自己負責——下游 MF_ComposeVideo
        # 收不到這個節點吐出的 op spec 內容，沒辦法幫忙 fingerprint。image_path 是
        # 這個節點自己的 widget，IS_CHANGED 看得到，換掉同名 PNG（mtime 變）要讓
        # 整條 chain 判定「變了」。
        return path_fingerprint(kwargs.get("image_path", ""))


NODE_CLASS_MAPPINGS = {"MF_ComposeWatermark": MF_ComposeWatermark}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeWatermark": "💧 Compose Watermark"}
