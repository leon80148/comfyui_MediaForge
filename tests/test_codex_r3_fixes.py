"""Regression tests for Codex Round 3 review findings.

跑法：python tests/test_codex_r3_fixes.py
"""
import os
import subprocess
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
if _CUSTOM_NODES not in sys.path:
    sys.path.insert(0, _CUSTOM_NODES)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def test_whisper_runtime_picks_default_per_provider(monkeypatch=None):
    """Whisper 在 cfg.model='' 時，依 provider 選正確 backend default；
    cfg.model 像 STT shape 則尊重它。對齊 production 的 _looks_like_stt_model 啟發式。"""
    from comfyui_MediaForge.nodes.whisper_transcribe import _looks_like_stt_model

    def resolve(cfg_model, provider):
        c = (cfg_model or "").strip()
        if _looks_like_stt_model(c):
            return c
        return "whisper-1" if provider == "openai_compatible" else "base"

    assert resolve("", "openai_compatible") == "whisper-1"
    assert resolve("", "faster_whisper_local") == "base"
    assert resolve("large-v3", "faster_whisper_local") == "large-v3"
    # chat-shape cfg.model 不該被當 STT
    assert resolve("gpt-4o-mini", "openai_compatible") == "whisper-1"
    print("[OK] R3 P2: effective_model resolution matrix 通過")


def test_trim_xfade_actually_implemented():
    """[R3 P2] crossfade_sec > 0 不能再 raise NotImplementedError；
    要實際 compile xfade/acrossfade chain 出來。"""
    from comfyui_MediaForge.nodes.trim_by_ranges import MF_TrimByRanges

    keep = [[0.0, 2.0], [3.0, 5.0], [7.0, 9.0]]
    cmd = MF_TrimByRanges._build_concat_command(
        "/tmp/fake.mp4", "/tmp/out.mp4", keep, xfade=0.3, has_audio=True,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    # 應該含 xfade=transition=fade 與 acrossfade=d=
    assert "xfade=transition=fade" in fc, f"xfade 沒出現:\n{fc}"
    assert "acrossfade=d=" in fc, f"acrossfade 沒出現:\n{fc}"
    # 應該不再含純 concat=n=... (改走 chained xfade)
    assert "concat=n=" not in fc, f"xfade 路徑不該走 concat filter:\n{fc}"
    print("[OK] R3 P2: trim crossfade_sec > 0 走 chained xfade 通過")


def test_trim_xfade_clamps_to_shortest_segment():
    """xfade 不能超過最短段、會被 clamp。"""
    from comfyui_MediaForge.nodes.trim_by_ranges import MF_TrimByRanges

    # 短段 0.5s 但要求 xfade=1.0s → 應 clamp
    keep = [[0.0, 0.5], [1.0, 3.0]]
    cmd = MF_TrimByRanges._build_concat_command(
        "/tmp/fake.mp4", "/tmp/out.mp4", keep, xfade=1.0, has_audio=False,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    # duration 應 <= 0.5 * 0.99 = 0.495
    import re
    m = re.search(r"xfade=transition=fade:duration=([\d.]+)", fc)
    assert m, f"xfade duration 沒找到\n{fc}"
    dur = float(m.group(1))
    assert dur < 0.5, f"xfade duration={dur} 沒 clamp 到 < shortest seg (0.5s)"
    print(f"[OK] R3 P2: xfade clamp to shortest seg → {dur:.4f}s")


def test_watermark_tile_uses_real_aspect_ratio():
    """[R3 P3] tile mode 必須 probe watermark 真實尺寸算 rows，不能假設正方形。"""
    from comfyui_MediaForge.nodes.compose_watermark import MF_ComposeWatermark
    from comfyui_MediaForge.utils.compose_ir import ComposeIR

    # 建一張 200x50 寬 logo
    with tempfile.TemporaryDirectory() as td:
        wm = os.path.join(td, "wide_logo.png")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=red:size=200x50:rate=1",
             "-frames:v", "1", wm],
            check=True, capture_output=True,
        )

        ir = ComposeIR(target_width=1920, target_height=1080)
        ir.add_video_input("/tmp/fake.mp4", is_main=True)

        node = MF_ComposeWatermark()
        (out_ir,) = node.watermark(
            compose=ir,
            image_path=wm,
            placement="tile",
            relative_scale=0.1,  # → scale_w = 192
            opacity=1.0,
            margin_top=0, margin_right=0, margin_bottom=0, margin_left=0,
            visible_start_sec=0.0, visible_end_sec=0.0,
        )
        op = out_ir.ops[-1]
        tile = op.params.get("tile", "")
        cols, rows = tile.split("x")
        cols, rows = int(cols), int(rows)
        # logo 200x50，scale 到寬度 192 → 高度 ≈ 48；
        # 1080 / 48 ≈ 22.5 → 至少 23 rows（vs naive "假設正方形" 給的 1080/192=6 rows）
        assert rows >= 22, f"rows={rows} 對寬 logo 來說太少，應有 >=22 (aspect-corrected)"
        assert cols == 10, f"cols={cols}, 1920/192=10"
        print(f"[OK] R3 P3: watermark tile {cols}x{rows} (對寬 logo 正確計算高度) 通過")


if __name__ == "__main__":
    test_whisper_runtime_picks_default_per_provider()
    test_trim_xfade_actually_implemented()
    test_trim_xfade_clamps_to_shortest_segment()
    test_watermark_tile_uses_real_aspect_ratio()
    print("\n=== Codex R3 fixes: all 4 cases passed ===")
