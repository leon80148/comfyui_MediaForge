"""MF_ComposeOverlayImage — Compose IR 上 append overlay op (一般圖片用)。"""
import os

from ..utils.compose_ir import ComposeIR


class MF_ComposeOverlayImage:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "compose": ("MF_COMPOSE",),
                "image_path": ("STRING", {"default": "input/overlay.png"}),
                "x_expr": ("STRING", {"default": "10"}),
                "y_expr": ("STRING", {"default": "10"}),
                # 縮放：0 = 原圖大小；>0 = 寬度像素 (高度等比例)
                "scale_w": ("INT", {"default": 0, "min": 0, "max": 7680, "step": 2}),
                "start_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
                "end_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE",)
    RETURN_NAMES = ("compose",)
    FUNCTION = "overlay_image"
    CATEGORY = "MediaForge/Compose"

    def overlay_image(self, compose, image_path, x_expr, y_expr, scale_w, start_sec, end_sec):
        if not isinstance(compose, ComposeIR):
            raise ValueError(
                f"[Compose Overlay Image] 輸入不是 MF_COMPOSE IR，拿到 {type(compose).__name__}"
            )
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"[Compose Overlay Image] 找不到圖片：{image_path}")

        ir = compose.clone()
        wm_label = ir.add_image_input(image_path)
        params = {"x": x_expr, "y": y_expr}
        if scale_w > 0:
            params["scale_w"] = int(scale_w)
        if end_sec > start_sec:
            params["start_sec"] = float(start_sec)
            params["end_sec"] = float(end_sec)
        ir.append_op("overlay", params, extra_input=wm_label)
        return (ir,)


NODE_CLASS_MAPPINGS = {"MF_ComposeOverlayImage": MF_ComposeOverlayImage}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeOverlayImage": "🖼️ Compose Overlay Image"}
