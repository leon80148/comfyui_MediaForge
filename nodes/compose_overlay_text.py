"""MF_ComposeOverlayText — append drawtext op spec into MF_COMPOSE_OPS chain。

Chain pattern (替代舊的 MF_COMPOSE IR mutation):輸入 optional overlays 列表、
append 自己的 op spec、回傳更新後的列表給下游 (通常是 MF_ComposeVideo)。

Effect preset (2026-05-14):effect dropdown 把使用者填的 x_expr / y_expr
當「最終停靠位置 (anchor)」、在節點層 wrap 一層動畫表達式後再交給 compose_ir。
這樣 utils/compose_ir.py 完全不用改、既有 workflow JSON (沒有 effect 欄位) 用
default `none` 落到原樣 return,行為 100% 兼容。
"""


# Position effect presets。值是 dropdown enum、語意見 _resolve_position docstring。
# 為什麼用 tuple 不用 PLACEMENT_PRESETS 那樣的 dict:這幾個 effect 邏輯分歧到沒辦法
# 用 placeholder template 表達(if(lt(t, ...)) 結構各不相同),寫成 if-branch resolver
# 比硬塞進 dict 清楚。
POSITION_EFFECTS = (
    "none",
    "slide_in_left",
    "slide_in_right",
    "slide_in_top",
    "slide_in_bottom",
    "marquee_horizontal",
)


def _resolve_position(effect: str, x_anchor: str, y_anchor: str,
                      duration: float, start_sec: float) -> tuple[str, str]:
    """把 anchor 表達式套上 effect、回傳最終丟給 FFmpeg drawtext 的 x / y string。

    語意:
    - `none`: 原樣 return,backward compatibility。
    - `slide_in_{left,right,top,bottom}`: 前 `duration` 秒 (從 start_sec 起算) 線性
       從畫面外滑到 anchor、之後一直停在 anchor。
    - `marquee_horizontal`: 忽略 x_anchor (走 w → -text_w 跑馬燈),y_anchor 沿用;
       `duration` 表示跑一輪的秒數。

    為什麼用 t-start_sec 而非單純 t:讓 effect 起始點對齊使用者設的「文字開始顯示」
    時間 (FFmpeg drawtext 用 enable='between(t,start,end)' 控可見性、但表達式時間
    軸跟全片同步)。start_sec=0 時 t-0=t 自然 fallback。
    """
    if effect == "none":
        return x_anchor, y_anchor

    if effect not in POSITION_EFFECTS:
        raise ValueError(
            f"[Compose Overlay Text] 未知 effect={effect!r}。"
            f" 允許值:{POSITION_EFFECTS}"
        )

    # FFmpeg expression: local time since text became visible
    t = f"(t-{float(start_sec)})"
    dur = float(duration)

    if effect == "slide_in_left":
        # 進場:x 從 -text_w 線性走到 x_anchor;之後停在 x_anchor
        new_x = (
            f"if(lt({t},{dur}),"
            f"-text_w+(({x_anchor})+text_w)*({t}/{dur}),"
            f"{x_anchor})"
        )
        return new_x, y_anchor

    if effect == "slide_in_right":
        # 進場:x 從 w 線性走到 x_anchor
        new_x = (
            f"if(lt({t},{dur}),"
            f"w-(w-({x_anchor}))*({t}/{dur}),"
            f"{x_anchor})"
        )
        return new_x, y_anchor

    if effect == "slide_in_top":
        new_y = (
            f"if(lt({t},{dur}),"
            f"-text_h+(({y_anchor})+text_h)*({t}/{dur}),"
            f"{y_anchor})"
        )
        return x_anchor, new_y

    if effect == "slide_in_bottom":
        new_y = (
            f"if(lt({t},{dur}),"
            f"h-(h-({y_anchor}))*({t}/{dur}),"
            f"{y_anchor})"
        )
        return x_anchor, new_y

    if effect == "marquee_horizontal":
        # 從右往左循環:cycle 起點 x=w (剛入畫)、終點 x=-text_w (出畫)
        # 用 mod() 達成無限循環、cycle 長度 = duration 秒、總位移 = w + text_w
        new_x = f"w-mod({t}*((w+text_w)/{dur}),w+text_w)"
        return new_x, y_anchor

    # Should be unreachable thanks to POSITION_EFFECTS guard above。
    raise ValueError(f"[Compose Overlay Text] 未支援的 effect={effect!r}")


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
                # Effect preset:none = 原樣用 x_expr/y_expr;slide_in_* / marquee_*
                # 把 x_expr/y_expr 當 anchor、節點層 wrap 動畫表達式。詳見 README。
                "effect": (list(POSITION_EFFECTS), {"default": "none"}),
                "effect_duration": ("FLOAT", {"default": 1.5, "min": 0.1, "max": 60.0, "step": 0.1}),
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
            borderw, bordercolor, effect, effect_duration, fontfile,
            start_sec, end_sec, overlays=None):
        # 套 effect (none → 原樣;其他 → wrap 動畫表達式)
        final_x, final_y = _resolve_position(
            effect, x_expr, y_expr,
            duration=effect_duration, start_sec=start_sec,
        )

        params = {
            "text": text,
            "x": final_x,
            "y": final_y,
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
