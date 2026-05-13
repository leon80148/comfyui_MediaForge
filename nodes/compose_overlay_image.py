"""MF_ComposeOverlayImage — append overlay op spec into MF_COMPOSE_OPS chain。

跟 ComposeWatermark 的差別:overlay image 是「絕對位置 + 絕對縮放」的通用 overlay、
watermark 是「相對位置 preset + relative_scale + tile/opacity」的便利版。
"""
import os


class MF_ComposeOverlayImage:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image_path": ("STRING", {"default": "input/overlay.png"}),
                "x_expr": ("STRING", {"default": "10"}),
                "y_expr": ("STRING", {"default": "10"}),
                # 縮放:0 = 原圖大小;>0 = 寬度像素 (高度等比例)
                "scale_w": ("INT", {"default": 0, "min": 0, "max": 7680, "step": 2}),
                "start_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
                "end_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
            },
            "optional": {
                "overlays": ("MF_COMPOSE_OPS",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE_OPS",)
    RETURN_NAMES = ("overlays",)
    FUNCTION = "add"
    CATEGORY = "MediaForge/Compose"

    def add(self, image_path, x_expr, y_expr, scale_w, start_sec, end_sec,
            overlays=None):
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"[Compose Overlay Image] 找不到圖片:{image_path}")

        params = {"x": x_expr, "y": y_expr}
        if scale_w > 0:
            params["scale_w"] = int(scale_w)
        if end_sec > start_sec:
            params["start_sec"] = float(start_sec)
            params["end_sec"] = float(end_sec)

        ops = list(overlays) if overlays else []
        ops.append({
            "type": "overlay",
            "image_path": image_path,
            "params": params,
        })
        return (ops,)


NODE_CLASS_MAPPINGS = {"MF_ComposeOverlayImage": MF_ComposeOverlayImage}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeOverlayImage": "🖼️ Compose Overlay Image"}
