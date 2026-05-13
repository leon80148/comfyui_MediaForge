"""ASS subtitle styling helpers — shared between MF_BurnSubtitle (file-level node)
and MF_ComposeBurnSubtitle (Compose chain node).

Reason this exists: BurnSubtitle had grown three responsibilities — font dropdown
listing, TTF family-name detection, and the `force_style` string assembly. All
three are needed unchanged for the Compose chain version too. Putting them in a
shared utility prevents drift (e.g. one node supporting nameID 16 while the
other still only reads nameID 1).
"""
import os

from .color import hex_to_ass_color
from .ffmpeg import escape_filter_path


# Plugin root = utils/.. — used to locate the font/ subdirectory shipped with plugin
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(_PLUGIN_DIR, "font")

# ASS Alignment numpad keys: 1=BL, 2=BC, 3=BR, 4=ML, 5=MC, 6=MR, 7=TL, 8=TC, 9=TR
ALIGNMENT_MAP = {
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

# Dropdown 不能 empty,沒任何字型時放 placeholder + caller 命中即 raise 帶友善訊息
NO_FONTS_PLACEHOLDER = "(把 .ttf / .otf / .ttc 丟進 plugin/font/ 後重新整理)"


def list_fonts():
    """List .ttf / .otf / .ttc files in plugin's font/ subdir for INPUT_TYPES dropdown.

    Returns sorted list of basenames (no path). Empty list if font/ doesn't exist.
    """
    if not os.path.isdir(FONT_DIR):
        return []
    return sorted(
        f for f in os.listdir(FONT_DIR)
        if f.lower().endswith((".ttf", ".otf", ".ttc"))
    )


def read_ttf_family_name(ttf_path):
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
        # 先試 nameID 16 (Preferred Family,使用者在 OS 字型選單看到的名字)、
        # 再 fallback 到 1 (Family,較舊、會把粗體/斜體拆成不同 family)。
        for name_id in (16, 1):
            for rec in name_table.names:
                if rec.nameID == name_id:
                    try:
                        return rec.toUnicode()
                    except UnicodeDecodeError:
                        continue
    except Exception as e:
        print(f"[ass_style] 注意:解析 {os.path.basename(ttf_path)} 失敗:{e}")
    return None


def resolve_font(font_filename, tag="Subtitle"):
    """Validate font filename + auto-detect family name. Returns (ttf_path, family_name).

    `tag` 是錯誤訊息前綴 (e.g. "Burn Subtitle" / "Compose Burn Subtitle"),
    讓使用者看到 raise 時知道哪個節點報的。

    Raises FileNotFoundError with friendly Chinese messages for:
    - placeholder selected (no fonts in font/)
    - font filename doesn't exist on disk
    """
    if font_filename == NO_FONTS_PLACEHOLDER:
        raise FileNotFoundError(
            f"[{tag}] font/ 目錄沒有 .ttf / .otf / .ttc。"
            "請把字型丟到 plugin/font/ 後重新整理瀏覽器讓 dropdown 重新掃描。"
        )
    ttf_path = os.path.join(FONT_DIR, font_filename)
    if not os.path.exists(ttf_path):
        raise FileNotFoundError(
            f"[{tag}] font 檔不存在:{ttf_path}"
            "(如剛丟新檔請重新整理瀏覽器讓 dropdown 重新掃描)"
        )
    family_name = read_ttf_family_name(ttf_path)
    if family_name:
        print(f"[{tag}] 字型 Family Name (auto): {family_name}")
    else:
        family_name = os.path.splitext(font_filename)[0]
        print(
            f"[{tag}] 字型 Family Name 退用檔名 stem: {family_name}"
            "(建議 `pip install fontTools` 讓自動偵測 TTF 內部名稱)"
        )
    return ttf_path, family_name


def build_ass_style_string(
    family_name, font_size,
    primary_hex, outline_hex, back_hex,
    bold, italic, letter_spacing,
    outline_width, shadow_depth, border_style,
    alignment_str, margin_v, margin_l, margin_r,
):
    """Build the ASS `force_style` string for libass.

    顏色 #RRGGBB → ASS BGR+alpha 透過 hex_to_ass_color。border_style=3 (box) 走
    半透明底色避免完全遮畫面;其他樣式底色完全透明。

    alignment_str 是 ALIGNMENT_MAP 的 key (例 "bottom_center (2)"),raise KeyError
    if unknown — caller 應該從 ALIGNMENT_MAP 取 dropdown choices。
    """
    primary = hex_to_ass_color(primary_hex)
    outline = hex_to_ass_color(outline_hex)
    back = hex_to_ass_color(back_hex, alpha="80" if border_style == 3 else "00")
    bold_val = -1 if bold else 0
    italic_val = -1 if italic else 0
    alignment_int = ALIGNMENT_MAP[alignment_str]

    return (
        f"Fontname={family_name},Fontsize={font_size},"
        f"PrimaryColour={primary},OutlineColour={outline},"
        f"BackColour={back},Bold={bold_val},Italic={italic_val},Spacing={letter_spacing},"
        f"BorderStyle={border_style},Outline={outline_width},Shadow={shadow_depth},"
        f"Alignment={alignment_int},MarginV={margin_v},MarginL={margin_l},MarginR={margin_r}"
    )


def build_subtitles_filter(srt_path, style_string, fontsdir=None):
    """Build FFmpeg `subtitles=...:fontsdir=...:force_style='...'` filter spec.

    Used by:
    - MF_BurnSubtitle: pass directly to `-vf`
    - MF_ComposeVideo compile (when subtitle op is in chain): append into video
      filter chain alongside drawtext / overlay

    `fontsdir` defaults to plugin's FONT_DIR; override only for tests / odd setups.

    Path escape: `escape_filter_path` 處理 Windows `C:\\` 跟 filter graph 內 `:` 衝突。
    `force_style` 不需要 escape — 內部 `,` 是 ASS spec、不是 filter 分隔。
    """
    parts = [
        f"subtitles={escape_filter_path(srt_path)}",
        f"fontsdir={escape_filter_path(fontsdir or FONT_DIR)}",
        f"force_style='{style_string}'",
    ]
    return ":".join(parts)
