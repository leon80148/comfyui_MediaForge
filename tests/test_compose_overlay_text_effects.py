"""MF_ComposeOverlayText effect preset tests (2026-05-14)。

涵蓋:
- _resolve_position() 各 branch 行為
- MF_ComposeOverlayText.add() 跟 effect resolver 的整合
- effect=none 的 backward compat (與舊行為等價)
- 未知 effect 應 raise ValueError

跑法:python -m pytest tests/test_compose_overlay_text_effects.py -v --import-mode=importlib
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodes.compose_overlay_text import (  # noqa: E402
    POSITION_EFFECTS,
    MF_ComposeOverlayText,
    _resolve_position,
)


# -------- _resolve_position resolver --------

def test_none_returns_anchor_verbatim():
    """`none` 是 backward-compat 的 short circuit:原樣 return x_anchor / y_anchor。"""
    x, y = _resolve_position("none", "(w-text_w)/2", "h-text_h-40",
                             duration=1.5, start_sec=0.0)
    assert x == "(w-text_w)/2"
    assert y == "h-text_h-40"


def test_unknown_effect_raises_value_error():
    with pytest.raises(ValueError, match="未知 effect"):
        _resolve_position("zoom_in", "0", "0", duration=1.0, start_sec=0.0)


def test_slide_in_left_wraps_anchor_with_lt_branch():
    """`slide_in_left` 應產出 if(lt(...)) 條件式、anchor 出現在 else 分支。"""
    x, y = _resolve_position("slide_in_left", "(w-text_w)/2", "h-text_h-40",
                             duration=2.0, start_sec=0.0)
    assert x.startswith("if(lt(")
    assert "-text_w+" in x
    assert "(w-text_w)/2" in x  # anchor 出現在 else 分支
    # y 不變,因為 slide_in_left 只動 x
    assert y == "h-text_h-40"


def test_slide_in_right_uses_w_as_offscreen_start():
    """`slide_in_right` 起點 x=w (畫面右邊外),anchor 在 else。"""
    x, _ = _resolve_position("slide_in_right", "(w-text_w)/2", "20",
                             duration=2.0, start_sec=0.0)
    assert "if(lt(" in x
    assert "w-(w-" in x  # 應該有 w - (w - anchor) * progress 的形式


def test_slide_in_top_wraps_y_not_x():
    """`slide_in_top` 動 y、x 保持 anchor。"""
    x, y = _resolve_position("slide_in_top", "100", "200",
                             duration=1.0, start_sec=0.0)
    assert x == "100"
    assert "if(lt(" in y
    assert "-text_h+" in y


def test_slide_in_bottom_wraps_y_with_h_origin():
    """`slide_in_bottom` 起點 y=h (畫面底部外)。"""
    _, y = _resolve_position("slide_in_bottom", "100", "200",
                             duration=1.0, start_sec=0.0)
    assert "if(lt(" in y
    assert "h-(h-" in y


def test_marquee_horizontal_uses_mod_for_cycle():
    """`marquee_horizontal` 應用 mod() 達成循環、不該有 if(lt())。"""
    x, _ = _resolve_position("marquee_horizontal", "ignored", "h-text_h-40",
                             duration=8.0, start_sec=0.0)
    assert "mod(" in x
    assert "w+text_w" in x  # 循環長度 = w + text_w
    assert "if(" not in x  # 無條件循環,不該有 if


def test_start_sec_offset_in_t_expression():
    """start_sec>0 時 effect 表達式應該用 (t-start_sec) 而非裸 t。"""
    x, _ = _resolve_position("slide_in_left", "(w-text_w)/2", "0",
                             duration=2.0, start_sec=3.0)
    # 應該看到 (t-3.0) 而非 t/2.0
    assert "(t-3.0)" in x


# -------- MF_ComposeOverlayText.add() integration --------

def test_add_with_effect_none_matches_legacy_behavior():
    """`effect="none"` + 預設值 → params.x / params.y 等於使用者填的 expression
    (跟改動前舊行為等價,backward compat 驗證)。"""
    node = MF_ComposeOverlayText()
    (ops,) = node.add(
        text="hi", x_expr="(w-text_w)/2", y_expr="h-text_h-40",
        fontsize=36, fontcolor="white", borderw=2, bordercolor="black",
        effect="none", effect_duration=1.5,
        fontfile="", start_sec=0.0, end_sec=0.0,
    )
    assert len(ops) == 1
    assert ops[0]["type"] == "drawtext"
    assert ops[0]["params"]["x"] == "(w-text_w)/2"
    assert ops[0]["params"]["y"] == "h-text_h-40"
    # 全長顯示 (start=end=0) 不該注入 start_sec / end_sec
    assert "start_sec" not in ops[0]["params"]
    assert "end_sec" not in ops[0]["params"]


def test_add_with_slide_in_left_resolves_x():
    """effect 套用後 params["x"] 應該變成 if(lt(...)) wrapper、原本 anchor 在內部。"""
    node = MF_ComposeOverlayText()
    (ops,) = node.add(
        text="hi", x_expr="(w-text_w)/2", y_expr="h-text_h-40",
        fontsize=36, fontcolor="white", borderw=2, bordercolor="black",
        effect="slide_in_left", effect_duration=2.0,
        fontfile="", start_sec=0.0, end_sec=0.0,
    )
    p = ops[0]["params"]
    assert p["x"].startswith("if(lt(")
    assert "(w-text_w)/2" in p["x"]
    assert p["y"] == "h-text_h-40"  # y 不變


def test_add_appends_to_existing_chain():
    """有 upstream overlays 時應 append (z-order 後者在上),不覆蓋。"""
    node = MF_ComposeOverlayText()
    upstream = [{"type": "watermark", "params": {}}]
    (ops,) = node.add(
        text="hi", x_expr="0", y_expr="0",
        fontsize=36, fontcolor="white", borderw=2, bordercolor="black",
        effect="none", effect_duration=1.5,
        fontfile="", start_sec=0.0, end_sec=0.0,
        overlays=upstream,
    )
    assert len(ops) == 2
    assert ops[0]["type"] == "watermark"  # upstream 在底層
    assert ops[1]["type"] == "drawtext"  # 新加的在上層


def test_input_types_exposes_effect_widgets():
    """新加的 widget 必須出現在 INPUT_TYPES 的 required 區、各自有合理 default。"""
    schema = MF_ComposeOverlayText.INPUT_TYPES()
    req = schema["required"]
    assert "effect" in req, "effect dropdown 缺漏"
    assert "effect_duration" in req, "effect_duration FLOAT 缺漏"

    effect_def, effect_opts = req["effect"]
    assert effect_def == list(POSITION_EFFECTS), "effect dropdown 選項應為 POSITION_EFFECTS"
    assert effect_opts["default"] == "none", "effect default 應為 none 確保 backward compat"

    dur_type, dur_opts = req["effect_duration"]
    assert dur_type == "FLOAT"
    assert dur_opts["min"] > 0  # duration 不應該允許 0 / 負值


# Standalone run support (mirrors test_compose_ir.py style)
if __name__ == "__main__":
    import traceback
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[OK] {name}")
            except Exception:
                print(f"[FAIL] {name}")
                traceback.print_exc()
