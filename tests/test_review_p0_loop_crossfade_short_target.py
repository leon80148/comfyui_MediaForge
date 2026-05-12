"""[P0-2] MF_LoopVideo crossfade mode：target <= xfade_dur 邊界要退階到 strict。

當 `target - xfade_dur` ≤ 0，公式 `ceil((target - xfade)/(eff - xfade))` 退化成
非正整數、`max(2, ...)` 強塞 n=2，但兩段 xfade 完全重疊輸出時長 = eff_dur（不到 target）。
靜默產出比預期短的影片是 silent data correctness issue —
應該偵測這條件、退到 strict mode 並印警告。
"""
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
sys.path.insert(0, _PLUGIN_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest  # noqa: F401, E402


def test_loop_crossfade_short_target_falls_back():
    """target_duration < (xfade + 1 seg overlap) 應退階到 strict mode。"""
    from comfyui_MediaForge.nodes.loop_video import MF_LoopVideo

    # eff_dur=10s, xfade=1s, target=0.5s（短於一個 segment-xfade overlap）
    parts, vlbl, albl = MF_LoopVideo._build_filter(
        mode="crossfade",
        eff_dur=10.0, target=0.5, xfade_dur=1.0,
        speed=1.0, reverse=False, has_audio=False, audio_volume=1.0,
    )
    # crossfade 模式下若 target <= xfade，現行行為 n=ceil((-0.5)/9)=ceil(<0)=0、max(2)=2，
    # 接出來只一段 xfade。理想：偵測 target<=xfade、退到 strict 走 loop=0（單段）。
    # 用 filter graph 內容判斷：strict mode 的關鍵 filter 是 `loop=loop=` 而非 `xfade=`。
    joined = ";".join(parts)
    has_xfade = "xfade=transition=fade" in joined
    has_loop = "loop=loop=" in joined
    assert has_loop and not has_xfade, (
        f"expect strict-mode fallback (loop filter) when target<=xfade, "
        f"but got xfade={has_xfade}, loop={has_loop}\nfilter:\n{joined}"
    )
    print("[OK] P0-2: crossfade target<=xfade 退階到 strict mode")


if __name__ == "__main__":
    test_loop_crossfade_short_target_falls_back()
    print("\n=== Review P0-2: loop crossfade short-target fallback passed ===")
