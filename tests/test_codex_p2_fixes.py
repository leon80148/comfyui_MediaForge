"""Regression tests for Codex P2 review findings.

跑法：python tests/test_codex_p2_fixes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_svtav1_preset_mapping_avoids_x264_names():
    """[P2 #1] SVT-AV1 用 numeric preset 0–13；x264 字串會被 ffmpeg 拒。"""
    from utils.video_io import svtav1_preset_from_name, _SVTAV1_PRESET_MAP

    # 所有 UI preset 都必須對到純數字
    ui_presets = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow"]
    for p in ui_presets:
        v = svtav1_preset_from_name(p)
        assert v.isdigit(), f"{p} 對到非數字: {v!r}"
        n = int(v)
        assert 0 <= n <= 13, f"{p} 對到 SVT-AV1 範圍外: {n}"
        assert p in _SVTAV1_PRESET_MAP, f"{p} 未在 map 內定義"

    # 未知 preset 退階到 medium (8)
    assert svtav1_preset_from_name("unknown") == "8"
    print("[OK] P2 #1: SVT-AV1 preset map → numeric 0–13 通過")


def test_fps_zero_fallback_when_avg_is_zero_over_zero():
    """[P2 #2] avg_frame_rate='0/0' 是 truthy string；舊版會 parse 出 fps=0。
    現在應 fall back 到 r_frame_rate."""
    from utils.video_io import _parse_fps

    # 直接驗 helper
    assert _parse_fps("0/0") == 0.0, "0/0 應解到 0.0"
    assert _parse_fps("30/1") == 30.0
    assert _parse_fps("30000/1001") > 29.0  # 29.97 fps

    # 模擬 decode 場景：avg='0/0', r='25/1' → effective_fps 應為 25.0
    fake_v = {"avg_frame_rate": "0/0", "r_frame_rate": "25/1"}
    src_fps = (
        _parse_fps(fake_v.get("avg_frame_rate") or "0/1")
        or _parse_fps(fake_v.get("r_frame_rate") or "0/1")
        or 0.0
    )
    assert src_fps == 25.0, f"預期從 r_frame_rate 拿到 25.0，但拿到 {src_fps}"

    # 另一個邊界：avg='0/0', r='0/0' → 0.0
    fake_v2 = {"avg_frame_rate": "0/0", "r_frame_rate": "0/0"}
    src_fps2 = (
        _parse_fps(fake_v2.get("avg_frame_rate") or "0/1")
        or _parse_fps(fake_v2.get("r_frame_rate") or "0/1")
        or 0.0
    )
    assert src_fps2 == 0.0
    print("[OK] P2 #2: avg_frame_rate=0/0 → fall back to r_frame_rate 通過")


if __name__ == "__main__":
    test_svtav1_preset_mapping_avoids_x264_names()
    test_fps_zero_fallback_when_avg_is_zero_over_zero()
    print("\n=== Codex P2 fixes: all 2 cases passed ===")
