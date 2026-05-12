"""MF_ComposeWatermark — Watermark preset 節點，內部 lower 到 overlay op。

v2.1 ROADMAP Phase 4：Codex review 指出 watermark 不是「overlay 加幾個參數」、有專屬 UX。
暴露 placement preset (TL/TR/BL/BR/center)、opacity、relative scale、temporal window、repeat mode。
"""
import os

from ..utils.compose_ir import ComposeIR
from ..utils.ffmpeg import probe


PLACEMENT_PRESETS = {
    "top_left": ("{margin_l}", "{margin_t}"),
    "top_right": ("W-w-{margin_r}", "{margin_t}"),
    "bottom_left": ("{margin_l}", "H-h-{margin_b}"),
    "bottom_right": ("W-w-{margin_r}", "H-h-{margin_b}"),
    "center": ("(W-w)/2", "(H-h)/2"),
    "tile": ("0", "0"),  # 配合 tile filter，watermark 已平鋪成滿幅，overlay 從 (0,0) 起始即可
}


class MF_ComposeWatermark:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "compose": ("MF_COMPOSE",),
                "image_path": ("STRING", {"default": "input/watermark.png"}),
                "placement": (list(PLACEMENT_PRESETS.keys()), {"default": "bottom_right"}),
                # 相對 frame width 的縮放比例，0.05~0.5；對應 watermark 寬度 = scale * target_width
                "relative_scale": ("FLOAT", {"default": 0.15, "min": 0.05, "max": 0.5, "step": 0.01}),
                # 透明度 0–1（1 = 不透明）。實作走 colorchannelmixer alpha — 不破壞原 PNG alpha
                "opacity": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.05}),
                "margin_top": ("INT", {"default": 20, "min": 0, "max": 1000, "step": 1}),
                "margin_right": ("INT", {"default": 20, "min": 0, "max": 1000, "step": 1}),
                "margin_bottom": ("INT", {"default": 20, "min": 0, "max": 1000, "step": 1}),
                "margin_left": ("INT", {"default": 20, "min": 0, "max": 1000, "step": 1}),
                # 時間區間：start=end=0 → 全長顯示
                "visible_start_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
                "visible_end_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE",)
    RETURN_NAMES = ("compose",)
    FUNCTION = "watermark"
    CATEGORY = "MediaForge/Compose"

    def watermark(self, compose, image_path, placement, relative_scale, opacity,
                  margin_top, margin_right, margin_bottom, margin_left,
                  visible_start_sec, visible_end_sec):
        if not isinstance(compose, ComposeIR):
            raise ValueError(
                f"[Compose Watermark] 輸入不是 MF_COMPOSE IR，拿到 {type(compose).__name__}"
            )
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"[Compose Watermark] 找不到 watermark 圖片：{image_path}")
        if placement not in PLACEMENT_PRESETS:
            raise ValueError(f"[Compose Watermark] 未知 placement={placement!r}")

        ir = compose.clone()
        wm_label = ir.add_image_input(image_path)

        # 解析 placement 表達式中的 margin
        x_tpl, y_tpl = PLACEMENT_PRESETS[placement]
        x_expr = x_tpl.format(margin_l=margin_left, margin_r=margin_right,
                              margin_t=margin_top, margin_b=margin_bottom)
        y_expr = y_tpl.format(margin_l=margin_left, margin_r=margin_right,
                              margin_t=margin_top, margin_b=margin_bottom)

        scale_w = max(8, int(ir.target_width * relative_scale))

        params = {"x": x_expr, "y": y_expr, "scale_w": scale_w}
        # opacity<1 → IR overlay op 走 colorchannelmixer aa= 把 PNG alpha 整體乘 k
        if opacity < 0.999:
            params["alpha"] = float(opacity)
        if visible_end_sec > visible_start_sec:
            params["start_sec"] = float(visible_start_sec)
            params["end_sec"] = float(visible_end_sec)
        if placement == "tile":
            # 計算需要幾 x 幾 tile 才能覆蓋整個 frame。
            # 從 ffprobe 取 watermark 真實 aspect ratio，再算縮放後高度 — 否則寬 logo (200x50)
            # 會把 rows 高估、實際只覆蓋 30% frame (Codex R3 P3 finding)。
            import math
            wm_w, wm_h = _probe_image_dims(image_path)
            if wm_w > 0 and wm_h > 0:
                scaled_h = scale_w * (wm_h / wm_w)
            else:
                # ffprobe 解析失敗 fallback 到正方形假設、印警告
                print(f"[Compose Watermark] 注意：無法讀取 {image_path} 尺寸、tile 行數按正方形估計")
                scaled_h = float(scale_w)
            cols = max(1, math.ceil(ir.target_width / scale_w))
            rows = max(1, math.ceil(ir.target_height / max(1.0, scaled_h)))
            params["tile"] = f"{cols}x{rows}"

        ir.append_op("overlay", params, extra_input=wm_label)
        return (ir,)


def _probe_image_dims(path):
    """ffprobe → (width, height)；解析失敗回 (0, 0)。"""
    info = probe(path)
    if not info:
        return (0, 0)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return (0, 0)
    return (int(v.get("width", 0)), int(v.get("height", 0)))


NODE_CLASS_MAPPINGS = {"MF_ComposeWatermark": MF_ComposeWatermark}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeWatermark": "💧 Compose Watermark"}
