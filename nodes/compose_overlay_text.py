"""MF_ComposeOverlayText — Compose IR 上 append drawtext op。"""
from ..utils.compose_ir import ComposeIR


class MF_ComposeOverlayText:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "compose": ("MF_COMPOSE",),
                "text": ("STRING", {"default": "Hello MediaForge", "multiline": True}),
                # 位置：FFmpeg drawtext 接受表達式（w/h/text_w/text_h/t），預設置中下
                "x_expr": ("STRING", {"default": "(w-text_w)/2"}),
                "y_expr": ("STRING", {"default": "h-text_h-40"}),
                "fontsize": ("INT", {"default": 36, "min": 8, "max": 300}),
                "fontcolor": ("STRING", {"default": "white"}),
                "borderw": ("INT", {"default": 2, "min": 0, "max": 20}),
                "bordercolor": ("STRING", {"default": "black"}),
                "fontfile": ("STRING", {"default": ""}),  # 空 → 用 ffmpeg 預設
                # 時間區間：start=end=0 表全長顯示
                "start_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
                "end_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE",)
    RETURN_NAMES = ("compose",)
    FUNCTION = "overlay_text"
    CATEGORY = "MediaForge/Compose"

    def overlay_text(self, compose, text, x_expr, y_expr, fontsize, fontcolor,
                     borderw, bordercolor, fontfile, start_sec, end_sec):
        if not isinstance(compose, ComposeIR):
            raise ValueError(f"[Compose Overlay Text] 輸入不是 MF_COMPOSE IR，拿到 {type(compose).__name__}")

        ir = compose.clone()
        params = {
            "text": text,
            "x": x_expr,
            "y": y_expr,
            "fontsize": int(fontsize),
            "fontcolor": fontcolor,
            "borderw": int(borderw),
            "bordercolor": bordercolor,
        }
        if fontfile.strip():
            params["fontfile"] = fontfile.strip()
        if end_sec > start_sec:
            params["start_sec"] = float(start_sec)
            params["end_sec"] = float(end_sec)
        ir.append_op("drawtext", params)
        return (ir,)


NODE_CLASS_MAPPINGS = {"MF_ComposeOverlayText": MF_ComposeOverlayText}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeOverlayText": "✏️ Compose Overlay Text"}
