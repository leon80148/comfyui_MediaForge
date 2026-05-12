import os

from ..utils.color import hex_to_ass_color
from ..utils.ffmpeg import ensure_ffmpeg, escape_filter_path, run_ffmpeg
from ..utils.output_path import resolve_output_path
from ..utils.video_io import encode_tensor_to_tempfile


# Plugin root = nodes/.. — used to locate the font/ subdirectory
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FONT_DIR = os.path.join(_PLUGIN_DIR, "font")

# ASS Alignment numpad keys: 1=BL, 2=BC, 3=BR, 4=ML, 5=MC, 6=MR, 7=TL, 8=TC, 9=TR
_ALIGNMENT_MAP = {
    "bottom_left (1)": 1,
    "bottom_center (2)": 2,
    "bottom_right (3)": 3,
    "middle_left (4)": 4,
    "middle_center (5)": 5,
    "middle_right (6)": 6,
    "top_left (7)": 7,
    "top_center (8)": 8,
    "top_right (9)": 9,
}

# 沒任何字型可選時 dropdown 不能空，放個 placeholder + burn() 時 raise 帶友善訊息
_NO_FONTS_PLACEHOLDER = "(把 .ttf / .otf / .ttc 丟進 plugin/font/ 後重新整理)"


def _list_fonts():
    """List .ttf / .otf / .ttc files in plugin's font/ subdir for the dropdown."""
    if not os.path.isdir(_FONT_DIR):
        return []
    return sorted(
        f for f in os.listdir(_FONT_DIR)
        if f.lower().endswith((".ttf", ".otf", ".ttc"))
    )


def _read_ttf_family_name(ttf_path):
    """Lazy-import fontTools; extract Family Name from TTF for ASS Fontname.

    Why: ASS subtitles filter resolves font by Family Name (not filename) via
    libass + fontsdir. The TTF filename ≠ internal Family Name in general
    (e.g. NotoSansCJK-Regular.ttf → "Noto Sans CJK TC"). Reading the name table
    gives us the right string without making the user look it up manually.

    Returns family name string, or None if fontTools missing / parse fails —
    caller falls back to the font's filename stem then.
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    try:
        # fontNumber=0 — TTC (font collection) 取第一個 face
        ttf = TTFont(ttf_path, fontNumber=0)
        name_table = ttf["name"]
        # 先試 nameID 16 (Preferred Family，使用者在 OS 字型選單看到的名字)、
        # 再 fallback 到 1 (Family，較舊、會把粗體/斜體拆成不同 family)。
        for name_id in (16, 1):
            for rec in name_table.names:
                if rec.nameID == name_id:
                    try:
                        return rec.toUnicode()
                    except UnicodeDecodeError:
                        continue
    except Exception as e:
        print(f"[Burn Subtitle] 注意：解析 {os.path.basename(ttf_path)} 失敗：{e}")
    return None


class MF_BurnSubtitle:
    @classmethod
    def INPUT_TYPES(s):
        fonts = _list_fonts()
        font_choices = fonts or [_NO_FONTS_PLACEHOLDER]
        # msjh.ttc = Microsoft JhengHei、常用中文字幕字型；有就帶為預設
        default_font = "msjh.ttc" if "msjh.ttc" in fonts else font_choices[0]
        return {
            "required": {
                # === 影片來源（最頂、最先設定）===
                # ComfyUI 渲染順序：required 全部 → optional 全部。video_path 放 required position 1
                # 才會在 node body 最頂端。連 frames pin 時 web/dual_input_lock.js 自動把它 hide。
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                # === 字幕來源 ===
                "srt_path": ("STRING", {"default": "input/sample.srt"}),
                # === 輸出設定 ===
                # filename_prefix 是 ComfyUI SaveImage 慣例：每次跑 workflow 自動接 _00001/_00002...，
                # 不會 silently 覆蓋舊輸出。子目錄 OK，如 "MediaForge/subtitled" → output/MediaForge/subtitled_00001.mp4
                "filename_prefix": ("STRING", {"default": "MediaForge/subtitled"}),

                # === 字型（user-prioritized，移到 styling block 開頭） ===
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

                # === 字幕位置（寬度 = 播放區寬 − margin_l − margin_r） ===
                "alignment": (list(_ALIGNMENT_MAP.keys()), {"default": "bottom_center (2)"}),
                "margin_v": ("INT", {"default": 20, "min": 0, "max": 500}),
                "margin_l": ("INT", {"default": 50, "min": 0, "max": 1000}),
                "margin_r": ("INT", {"default": 50, "min": 0, "max": 1000}),
            },
            "optional": {
                # Tensor pipeline (in-memory chain)：連線 frames 時 web/dual_input_lock.js 會 hide 上面的 video_path
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),

                # 進階：輸出畫格率覆寫；0 = 沿用 source fps
                "target_fps": ("INT", {"default": 0, "min": 0, "max": 120, "step": 1}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "burn"
    CATEGORY = "MediaForge/Subtitle"

    def burn(self, video_path, srt_path, filename_prefix, font, font_size,
             font_color_hex, bold, italic, letter_spacing,
             outline_color_hex, outline_width, shadow_depth, border_style, back_color_hex,
             alignment, margin_v, margin_l, margin_r,
             frames=None, tensor_fps=30.0, audio=None,
             target_fps=0):
        if not ensure_ffmpeg():
            raise RuntimeError("[Burn Subtitle] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(srt_path):
            raise FileNotFoundError(f"[Burn Subtitle] 找不到字幕：{srt_path}")

        # font dropdown：空目錄 placeholder 命中即 raise 帶友善訊息
        if font == _NO_FONTS_PLACEHOLDER:
            raise FileNotFoundError(
                "[Burn Subtitle] font/ 目錄沒有 .ttf / .otf / .ttc。"
                "請把字型丟到 plugin/font/ 後重新整理瀏覽器讓 dropdown 重新掃描。"
            )
        ttf_path = os.path.join(_FONT_DIR, font)
        if not os.path.exists(ttf_path):
            raise FileNotFoundError(
                f"[Burn Subtitle] font 檔不存在：{ttf_path}"
                "（如剛丟新檔請重新整理瀏覽器讓 dropdown 重新掃描）"
            )

        # 偵測 Family Name；fontTools 沒裝 / parse 失敗就退用檔名 stem
        family_name = _read_ttf_family_name(ttf_path)
        if family_name:
            print(f"[Burn Subtitle] 字型 Family Name (auto): {family_name}")
        else:
            family_name = os.path.splitext(font)[0]
            print(
                f"[Burn Subtitle] 字型 Family Name 退用檔名 stem: {family_name}"
                "（建議 `pip install fontTools` 讓自動偵測 TTF 內部名稱）"
            )

        # 解析輸出路徑 — auto-counter 不會覆蓋舊檔
        output_path = resolve_output_path(filename_prefix, ".mp4")

        # ASS Style 組裝
        primary = hex_to_ass_color(font_color_hex)
        outline = hex_to_ass_color(outline_color_hex)
        # border_style=3 為「box」底色樣式，底色半透明 (alpha=80) 避免完全遮畫面；其他樣式則完全透明
        back = hex_to_ass_color(back_color_hex, alpha="80" if border_style == 3 else "00")
        bold_val = -1 if bold else 0
        italic_val = -1 if italic else 0
        alignment_int = _ALIGNMENT_MAP[alignment]

        style = (
            f"Fontname={family_name},Fontsize={font_size},"
            f"PrimaryColour={primary},OutlineColour={outline},"
            f"BackColour={back},Bold={bold_val},Italic={italic_val},Spacing={letter_spacing},"
            f"BorderStyle={border_style},Outline={outline_width},Shadow={shadow_depth},"
            f"Alignment={alignment_int},MarginV={margin_v},MarginL={margin_l},MarginR={margin_r}"
        )

        cleanup_tmp = None
        try:
            # Dual-input dispatch：frames 接了 → 寫 temp mp4；沒接 → 用 video_path
            if frames is not None:
                source_path = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
                cleanup_tmp = source_path
            else:
                if not os.path.exists(video_path):
                    raise FileNotFoundError(
                        f"[Burn Subtitle] 找不到影片：{video_path}"
                        "（連 frames 或在 video_path 填好路徑）"
                    )
                source_path = video_path

            # 組 -vf subtitles=...:fontsdir=...:force_style='...' (FFmpeg 用 : 串選項)
            vf_parts = [
                f"subtitles={escape_filter_path(srt_path)}",
                f"fontsdir={escape_filter_path(_FONT_DIR)}",
                f"force_style='{style}'",
            ]
            vf_arg = ":".join(vf_parts)

            command = ["ffmpeg", "-y", "-i", source_path]
            if target_fps > 0:
                command.extend(["-r", str(target_fps)])
            # R10 P2 fix：`-c:a copy` 跨容器 (e.g., MKV/WebM → MP4) mux fail，統一轉 AAC
            command.extend([
                "-vf", vf_arg,
                "-c:a", "aac", "-b:a", "192k",
                output_path,
            ])

            if not run_ffmpeg(command, tag="Burn Subtitle"):
                raise RuntimeError("[Burn Subtitle] FFmpeg 燒字幕失敗，請查看上方 stderr 輸出。")
        finally:
            if cleanup_tmp:
                try:
                    os.unlink(cleanup_tmp)
                except OSError:
                    pass

        print(f"[Burn Subtitle] 輸出成功: {output_path}")
        return (output_path,)


NODE_CLASS_MAPPINGS = {"MF_BurnSubtitle": MF_BurnSubtitle}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_BurnSubtitle": "🔥 Burn Subtitle"}
