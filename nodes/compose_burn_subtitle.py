"""MF_ComposeBurnSubtitle — append subtitle op spec into MF_COMPOSE_OPS chain。

跟獨立 `MF_BurnSubtitle` 的差別:這個版本**只 append op spec、不跑 ffmpeg**。
ComposeVideo compile 時把 subtitle filter 合進整條 -filter_complex,做到「字幕 +
overlay + 浮水印 + 音訊」單次 encode。

設定 widget 跟 MF_BurnSubtitle 完全一致(font dropdown / ASS style 全套)、共用
utils/ass_style helpers,字幕渲染輸出視覺上保證 pixel-identical。
"""
import os

from ..utils.ass_style import (
    ALIGNMENT_MAP,
    FONT_DIR,
    NO_FONTS_PLACEHOLDER,
    build_ass_style_string,
    list_fonts,
    resolve_font,
)
from ..utils.cache_key import path_fingerprint


class MF_ComposeBurnSubtitle:
    @classmethod
    def INPUT_TYPES(s):
        fonts = list_fonts()
        font_choices = fonts or [NO_FONTS_PLACEHOLDER]
        default_font = "msjh.ttc" if "msjh.ttc" in fonts else font_choices[0]
        return {
            "required": {
                "srt_path": ("STRING", {"default": "input/sample.srt"}),

                # === 字型 ===
                "font": (font_choices, {"default": default_font}),
                "font_size": ("INT", {"default": 24, "min": 8, "max": 150}),
                "font_color_hex": ("STRING", {"default": "#FFFFFF"}),
                "bold": ("BOOLEAN", {"default": True}),
                "italic": ("BOOLEAN", {"default": False}),
                "letter_spacing": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 20.0, "step": 0.1}),

                # === 描邊 / 陰影 / 底色 ===
                "outline_color_hex": ("STRING", {"default": "#000000"}),
                "outline_width": ("INT", {"default": 2, "min": 0, "max": 10}),
                "shadow_depth": ("INT", {"default": 1, "min": 0, "max": 10}),
                "border_style": ("INT", {"default": 1, "min": 1, "max": 3, "step": 2}),
                "back_color_hex": ("STRING", {"default": "#000000"}),

                # === 位置 ===
                "alignment": (list(ALIGNMENT_MAP.keys()), {"default": "bottom_center (2)"}),
                "margin_v": ("INT", {"default": 20, "min": 0, "max": 500}),
                "margin_l": ("INT", {"default": 50, "min": 0, "max": 1000}),
                "margin_r": ("INT", {"default": 50, "min": 0, "max": 1000}),
            },
            "optional": {
                # 上游 overlay chain (一般接其他 overlay 節點)。沒接 = 從空 chain 起點
                "overlays": ("MF_COMPOSE_OPS",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE_OPS",)
    RETURN_NAMES = ("overlays",)
    FUNCTION = "add"
    CATEGORY = "MediaForge/Compose"

    def add(self, srt_path,
            font, font_size,
            font_color_hex, bold, italic, letter_spacing,
            outline_color_hex, outline_width, shadow_depth, border_style, back_color_hex,
            alignment, margin_v, margin_l, margin_r,
            overlays=None):
        # SRT 檢查(早 raise、避免到 ComposeVideo 才爆)
        if not os.path.exists(srt_path):
            raise FileNotFoundError(f"[Compose Burn Subtitle] 找不到字幕:{srt_path}")

        # 字型解析 — 同 MF_BurnSubtitle、共用 helper
        _ttf_path, family_name = resolve_font(font, tag="Compose Burn Subtitle")

        # ASS style string 組裝 — 跟 BurnSubtitle 結果完全一致(共用 helper)
        style = build_ass_style_string(
            family_name, font_size,
            font_color_hex, outline_color_hex, back_color_hex,
            bold, italic, letter_spacing,
            outline_width, shadow_depth, border_style,
            alignment, margin_v, margin_l, margin_r,
        )

        # op spec 用絕對 srt path + 預先組好的 style + 明確 fontsdir;
        # ComposeVideo compile 時 build_subtitles_filter 會做 escape_filter_path
        params = {
            "srt_path": srt_path,
            "style_string": style,
            "fontsdir": FONT_DIR,
        }

        ops = list(overlays) if overlays else []
        ops.append({"type": "subtitle", "params": params})
        return (ops,)

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # C1：ComfyUI IS_CHANGED 收到的 linked 輸入（上游 overlays chain）一律是
        # None，檔案變更偵測必須由持有 path widget 的節點自己負責——srt_path 是
        # 這個節點自己的 widget，IS_CHANGED 看得到。
        return path_fingerprint(kwargs.get("srt_path", ""))


NODE_CLASS_MAPPINGS = {"MF_ComposeBurnSubtitle": MF_ComposeBurnSubtitle}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeBurnSubtitle": "🔥 Compose Burn Subtitle"}
