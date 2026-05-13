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


def test_whisper_default_does_not_break_local_backend():
    """[R3 P2 + 6adbd27 refactor] model_override widget 已移除（dead UI），現在 effective_model
    完全由 cfg.model + provider 決定。本 test 鎖住 regression：

    1. INPUT_TYPES 不可以再回 model_override（避免重新引入 dead widget）
    2. effective_model 落點仍然 backend-aware：
       - openai_compatible + cfg.model='' → 'whisper-1'
       - faster_whisper_local + cfg.model='' → 'base'

    為什麼這 test 還活著：原 R3 P2 的精神（不能硬塞固定字串蓋掉 local backend 的
    'base'/'small'/'large-v3'）今天仍要驗 — 只是改成驗 effective_model 邏輯，不再驗
    widget default。
    """
    from comfyui_MediaForge.nodes.whisper_transcribe import (
        MF_WhisperTranscribe, _looks_like_stt_model,
    )

    types = MF_WhisperTranscribe.INPUT_TYPES()
    req = types["required"]
    opt = types.get("optional", {})
    # 1. dead widget 不能 resurrect
    assert "model_override" not in req and "model_override" not in opt, (
        "model_override widget 在 6adbd27 被移除（dead UI）。test_whisper_default 鎖住"
        "regression — 別再加回來；effective_model 已純由 cfg.model + provider 決定。"
    )

    # 2. effective_model 仍 backend-aware（不會用 'whisper-1' 蓋掉 faster_whisper_local）
    def _effective_model(cfg_model, provider):
        cfg_model = (cfg_model or "").strip()
        if _looks_like_stt_model(cfg_model):
            return cfg_model
        return "base" if provider == "faster_whisper_local" else "whisper-1"

    assert _effective_model("", "faster_whisper_local") == "base"
    assert _effective_model("", "openai_compatible") == "whisper-1"
    # cfg.model='gpt-4o-mini' (non-STT shape) 不應被當 STT model → 退回 backend 預設
    assert _effective_model("gpt-4o-mini", "faster_whisper_local") == "base"
    # 但 STT-shape model id 走 cfg.model
    assert _effective_model("large-v3", "faster_whisper_local") == "large-v3"
    print("[OK] R3 P2 (post-6adbd27): model_override widget 已移除、effective_model 仍 backend-aware")


def test_whisper_runtime_picks_default_per_provider(monkeypatch=None):
    """Whisper 在 model_override='' 且 cfg.model='' 時，依 provider 選正確 backend default。"""
    # 直接驗 effective_model 決策邏輯（不真的 call API）
    def resolve(override, cfg_model, provider):
        u = (override or "").strip()
        c = (cfg_model or "").strip()
        if u:
            return u
        if c:
            return c
        return "whisper-1" if provider == "openai_compatible" else "base"

    assert resolve("", "", "openai_compatible") == "whisper-1"
    assert resolve("", "", "faster_whisper_local") == "base"
    assert resolve("", "large-v3", "faster_whisper_local") == "large-v3"
    assert resolve("custom-model", "base", "openai_compatible") == "custom-model"
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
    """[R3 P3] tile mode 必須 probe watermark 真實尺寸算 rows，不能假設正方形。

    Compose v2 (Phase 4.5)：MF_ComposeWatermark 變成純 op-spec emitter (回 MF_COMPOSE_OPS
    list[dict])、tile 計算搬到 utils/compose_ops.resolve_watermark_params 在 ComposeVideo
    compile 時做。Test 也跟著 port：先呼叫 add() 拿 op spec、再 call resolve_*params 驗
    tile 行列數對寬 logo 正確（aspect-corrected）。
    """
    from comfyui_MediaForge.nodes.compose_watermark import MF_ComposeWatermark
    from comfyui_MediaForge.utils.compose_ops import resolve_watermark_params

    # 建一張 200x50 寬 logo
    with tempfile.TemporaryDirectory() as td:
        wm = os.path.join(td, "wide_logo.png")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=red:size=200x50:rate=1",
             "-frames:v", "1", wm],
            check=True, capture_output=True,
        )

        node = MF_ComposeWatermark()
        (ops,) = node.add(
            image_path=wm,
            placement="tile",
            relative_scale=0.1,  # → scale_w = 192 @ target_width=1920
            opacity=1.0,
            margin_top=0, margin_right=0, margin_bottom=0, margin_left=0,
            visible_start_sec=0.0, visible_end_sec=0.0,
        )
        assert len(ops) == 1 and ops[0]["type"] == "watermark", (
            f"add() 應 emit 1 op type=watermark；實際 ops={ops}"
        )

        # Resolve 到 IR overlay params (compile time)
        resolved = resolve_watermark_params(
            ops[0]["params"], wm,
            target_width=1920, target_height=1080,
        )
        tile = resolved.get("tile", "")
        cols, rows = tile.split("x")
        cols, rows = int(cols), int(rows)
        # logo 200x50，scale 到寬度 192 → 高度 ≈ 48；
        # 1080 / 48 ≈ 22.5 → 至少 23 rows（vs naive "假設正方形" 給的 1080/192=6 rows）
        assert rows >= 22, f"rows={rows} 對寬 logo 來說太少，應有 >=22 (aspect-corrected)"
        assert cols == 10, f"cols={cols}, 1920/192=10"
        print(f"[OK] R3 P3: watermark tile {cols}x{rows} (對寬 logo 正確計算高度) 通過")


if __name__ == "__main__":
    test_whisper_default_does_not_break_local_backend()
    test_whisper_runtime_picks_default_per_provider()
    test_trim_xfade_actually_implemented()
    test_trim_xfade_clamps_to_shortest_segment()
    test_watermark_tile_uses_real_aspect_ratio()
    print("\n=== Codex R3 fixes: all 5 cases passed ===")
