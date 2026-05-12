import os

from ..utils.color import hex_to_ass_color
from ..utils.ffmpeg import ensure_ffmpeg, escape_filter_path, run_ffmpeg
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

# font_file dropdown sentinel meaning "fall back to the font_name STRING widget"
_FONT_SENTINEL = "(use font_name)"


def _list_fonts():
    """List .ttf / .otf / .ttc files in plugin's font/ subdir for the dropdown.

    Returns sorted filenames (no path). Empty list if font/ doesn't exist.
    """
    if not os.path.isdir(_FONT_DIR):
        return []
    return sorted(
        f for f in os.listdir(_FONT_DIR)
        if f.lower().endswith((".ttf", ".otf", ".ttc"))
    )


def _read_ttf_family_name(ttf_path):
    """Lazy-import fontTools; extract Family Name from TTF for ASS Fontname.

    Why: ASS subtitles filter resolves font by Family Name (not filename) via
    libass + fontsdir. The TTF's filename ≠ its internal Family Name in general
    (e.g. NotoSansCJK-Regular.ttf → "Noto Sans CJK TC"). Reading the name table
    gives us the right string without making the user look it up manually.

    Returns family name string, or None if fontTools missing / parse fails —
    caller falls back to the user-typed font_name in that case.
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print(
            "[Burn Subtitle] 注意：未安裝 fontTools，無法自動讀取 TTF Family Name。"
            "請 `pip install fontTools` 或在 font_name 手動輸入字型的內部名稱。"
        )
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
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                "srt_path": ("STRING", {"default": "input/sample.srt"}),
                "output_path": ("STRING", {"default": "output/subtitled.mp4"}),

                # 字體與顏色
                "font_name": ("STRING", {"default": "Microsoft JhengHei"}),
                "font_size": ("INT", {"default": 24, "min": 8, "max": 150}),
                "font_color_hex": ("STRING", {"default": "#FFFFFF"}),
                "bold": ("BOOLEAN", {"default": True}),
                "italic": ("BOOLEAN", {"default": False}),

                # 邊框與陰影
                "outline_color_hex": ("STRING", {"default": "#000000"}),
                "outline_width": ("INT", {"default": 2, "min": 0, "max": 10}),
                "shadow_depth": ("INT", {"default": 1, "min": 0, "max": 10}),
                "border_style": ("INT", {"default": 1, "min": 1, "max": 3, "step": 2}),
                "back_color_hex": ("STRING", {"default": "#000000"}),

                # 排版：字幕「寬度」= 播放區寬 - margin_l - margin_r；想推到某一側就拉大那邊的 margin
                "alignment": (list(_ALIGNMENT_MAP.keys()), {"default": "bottom_center (2)"}),
                "margin_v": ("INT", {"default": 20, "min": 0, "max": 500}),
                "margin_l": ("INT", {"default": 50, "min": 0, "max": 1000}),
                "margin_r": ("INT", {"default": 50, "min": 0, "max": 1000}),
                "letter_spacing": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 20.0, "step": 0.1}),

                # 影片輸出設定
                "target_fps": ("INT", {"default": 0, "min": 0, "max": 120, "step": 1}),
            },
            "optional": {
                # In-memory chain：frames 接了 → 走 tensor → temp .mp4 → burn 流程
                "frames": ("IMAGE",),
                "fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),
                # 從 plugin/font/ 挑 TTF；選了會 set fontsdir + 嘗試 lazy 讀 Family Name
                "font_file": ([_FONT_SENTINEL] + fonts, {"default": _FONT_SENTINEL}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "burn"
    CATEGORY = "MediaForge/Subtitle"

    def burn(self, video_path, srt_path, output_path, font_name, font_size,
             font_color_hex, bold, italic, outline_color_hex, outline_width, shadow_depth,
             border_style, back_color_hex, alignment, margin_v, margin_l, margin_r,
             letter_spacing, target_fps,
             frames=None, fps=30.0, audio=None, font_file=_FONT_SENTINEL):

        if not ensure_ffmpeg():
            raise RuntimeError("[Burn Subtitle] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(srt_path):
            raise FileNotFoundError(f"[Burn Subtitle] 找不到字幕：{srt_path}")

        # ASS Style 欄位
        primary = hex_to_ass_color(font_color_hex)
        outline = hex_to_ass_color(outline_color_hex)
        # border_style=3 為「box」底色樣式，底色半透明 (alpha=80) 避免完全遮畫面；其他樣式則完全透明
        back = hex_to_ass_color(back_color_hex, alpha="80" if border_style == 3 else "00")
        bold_val = -1 if bold else 0
        italic_val = -1 if italic else 0
        alignment_int = _ALIGNMENT_MAP[alignment]

        # font_file 處理：選了具體 TTF → 嘗試 lazy 讀 Family Name 覆蓋 font_name
        effective_font_name = font_name
        fontsdir = None
        if font_file and font_file != _FONT_SENTINEL:
            ttf_path = os.path.join(_FONT_DIR, font_file)
            if not os.path.exists(ttf_path):
                raise FileNotFoundError(
                    f"[Burn Subtitle] font_file 不存在：{ttf_path}"
                    "（重新整理瀏覽器讓 dropdown 重新掃描 font/ 目錄）"
                )
            detected = _read_ttf_family_name(ttf_path)
            if detected:
                effective_font_name = detected
                print(f"[Burn Subtitle] 自動偵測 TTF Family Name：{detected}")
            else:
                # fontTools 沒裝或 parse 失敗 → 仍 set fontsdir 讓 libass 嘗試用 font_name 找
                print(f"[Burn Subtitle] 退用 font_name='{font_name}' 在 fontsdir 內尋找")
            fontsdir = _FONT_DIR

        style = (
            f"Fontname={effective_font_name},Fontsize={font_size},"
            f"PrimaryColour={primary},OutlineColour={outline},"
            f"BackColour={back},Bold={bold_val},Italic={italic_val},Spacing={letter_spacing},"
            f"BorderStyle={border_style},Outline={outline_width},Shadow={shadow_depth},"
            f"Alignment={alignment_int},MarginV={margin_v},MarginL={margin_l},MarginR={margin_r}"
        )

        cleanup_tmp = None
        try:
            # Dual-input dispatch：frames 接了 → 寫 temp mp4；沒接 → 用 video_path
            if frames is not None:
                source_path = encode_tensor_to_tempfile(frames, fps=fps, audio=audio)
                cleanup_tmp = source_path
            else:
                if not os.path.exists(video_path):
                    raise FileNotFoundError(f"[Burn Subtitle] 找不到影片：{video_path}")
                source_path = video_path

            # 組 -vf subtitles=...:[fontsdir=...:]force_style='...' (FFmpeg 用 : 串選項)
            vf_parts = [f"subtitles={escape_filter_path(srt_path)}"]
            if fontsdir:
                vf_parts.append(f"fontsdir={escape_filter_path(fontsdir)}")
            vf_parts.append(f"force_style='{style}'")
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
