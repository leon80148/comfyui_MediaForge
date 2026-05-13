"""MF_ComposeOverlayText — append drawtext op spec into MF_COMPOSE_OPS chain。

Chain pattern (替代舊的 MF_COMPOSE IR mutation):輸入 optional overlays 列表、
append 自己的 op spec、回傳更新後的列表給下游 (通常是 MF_ComposeVideo)。
"""


class MF_ComposeOverlayText:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {"default": "Hello MediaForge", "multiline": True}),
                # 位置:FFmpeg drawtext 接受表達式(w/h/text_w/text_h/t)、預設置中下
                "x_expr": ("STRING", {"default": "(w-text_w)/2"}),
                "y_expr": ("STRING", {"default": "h-text_h-40"}),
                "fontsize": ("INT", {"default": 36, "min": 8, "max": 300}),
                "fontcolor": ("STRING", {"default": "white"}),
                "borderw": ("INT", {"default": 2, "min": 0, "max": 20}),
                "bordercolor": ("STRING", {"default": "black"}),
                "fontfile": ("STRING", {"default": ""}),  # 空 → ComposeVideo compile 時 fallback
                # 時間區間:start=end=0 表全長顯示
                "start_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
                "end_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
            },
            "optional": {
                # 上游 chain (其他 overlay 節點)。沒接 = 新 chain 起點、append 後輸出。
                "overlays": ("MF_COMPOSE_OPS",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE_OPS",)
    RETURN_NAMES = ("overlays",)
    FUNCTION = "add"
    CATEGORY = "MediaForge/Compose"

    def add(self, text, x_expr, y_expr, fontsize, fontcolor,
            borderw, bordercolor, fontfile, start_sec, end_sec,
            overlays=None):
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

        # 順序 = z-order:先在列表的在底層、後 append 的疊在上面
        ops = list(overlays) if overlays else []
        ops.append({"type": "drawtext", "params": params})
        return (ops,)


NODE_CLASS_MAPPINGS = {"MF_ComposeOverlayText": MF_ComposeOverlayText}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeOverlayText": "✏️ Compose Overlay Text"}
