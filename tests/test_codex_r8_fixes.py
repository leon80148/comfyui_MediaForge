"""Regression tests for Codex Round 8 review findings.

R8 P2 #1 (ProRes container) 原本驗 `_ensure_compatible_container` helper；
post-hoc fix 已被 codec-aware `resolve_output_path(prefix, ext)` 模式取代
(save_video_frames 內 inline 決定 `ext = ".mov" if codec=="prores_ks" else ".mp4"`)。
本檔案 P3-7 改寫後驗的是「ext 由 codec 決定」這個契約。

跑法：python tests/test_codex_r8_fixes.py
"""
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _CUSTOM_NODES not in sys.path:
    sys.path.insert(0, _CUSTOM_NODES)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
import conftest  # noqa: E402,F401  (installs folder_paths stub for standalone runs)
from conftest import unpack  # noqa: E402


def test_resolve_output_path_codec_aware_ext_independence():
    """[R8 P2 → P3-7 改寫] resolve_output_path 對相同 prefix 用不同 ext 必須 counter
    各自獨立 — `save_prores` 走 .mov、`save_h264` 走 .mp4 不會互相搶 counter。

    舊 helper `_ensure_compatible_container` 已移除；現在 ext 是由 caller (save_video_frames
    依 codec 選 `.mov`/`.mp4`)直接傳給 resolve_output_path。本 test 驗該層契約。
    """
    from utils.output_path import resolve_output_path

    # 兩次 .mov 都應 counter 遞增（同 prefix 同 ext）
    p1 = resolve_output_path("MediaForge/test_r8_codec_ext", ".mov")
    open(p1, "wb").close()
    p2 = resolve_output_path("MediaForge/test_r8_codec_ext", ".mov")
    open(p2, "wb").close()
    assert p1 != p2 and p1.endswith(".mov") and p2.endswith(".mov"), (
        f"counter 應遞增：{p1} vs {p2}"
    )

    # 同 prefix 但 .mp4 → counter 從 1 起跳（與 .mov 獨立）
    p3 = resolve_output_path("MediaForge/test_r8_codec_ext", ".mp4")
    open(p3, "wb").close()
    assert p3.endswith("_00001.mp4"), (
        f".mp4 counter 應從 1 起跳（不受 .mov 影響）: {p3}"
    )

    # 清掉自製檔避免污染後續 run
    for p in (p1, p2, p3):
        try:
            os.unlink(p)
        except OSError:
            pass

    print("[OK] R8 P2 / P3-7: resolve_output_path codec-aware ext counter 獨立")


def test_save_prores_writes_mov_via_filename_prefix():
    """[R8 P2 端到端] MF_SaveVideoFrames 接 prores codec → 自動產出 .mov、不該 fail。"""
    import torch
    from comfyui_MediaForge.nodes.save_video_frames import MF_SaveVideoFrames

    with tempfile.TemporaryDirectory() as _td:
        frames = torch.rand(10, 64, 64, 3)
        node = MF_SaveVideoFrames()
        (result,) = unpack(node.save(
            frames=frames, filename_prefix="MediaForge/test_r8_save_prores",
            fps=10.0,
            codec="prores (prores_ks)", encode_mode="crf", crf=23,
            bitrate_kbps=4000, target_size_mb=8.0,
            preset="medium", pix_fmt_override="",
        ))
        assert result.endswith(".mov"), f"prores 應產出 .mov 檔，但拿到 {result}"
        assert os.path.exists(result), f"檔案不存在：{result}"
        print(f"[OK] R8 P2: ProRes save 自動寫成 {os.path.basename(result)}")


def test_save_h264_writes_mp4_via_filename_prefix():
    """副驗：非 prores codec 仍走 .mp4 — codec-aware ext 切換邏輯雙向都對。"""
    import torch
    from comfyui_MediaForge.nodes.save_video_frames import MF_SaveVideoFrames

    with tempfile.TemporaryDirectory() as _td:
        frames = torch.rand(10, 64, 64, 3)
        node = MF_SaveVideoFrames()
        (result,) = unpack(node.save(
            frames=frames, filename_prefix="MediaForge/test_r8_save_h264",
            fps=10.0,
            codec="h264 (libx264)", encode_mode="crf", crf=23,
            bitrate_kbps=4000, target_size_mb=8.0,
            preset="ultrafast", pix_fmt_override="",
        ))
        assert result.endswith(".mp4"), f"libx264 應產出 .mp4 檔，但拿到 {result}"
        assert os.path.exists(result), f"檔案不存在：{result}"
        print(f"[OK] R8 P2 副驗: h264 save 寫成 {os.path.basename(result)}")


def _concat_xfade_clamp_case(clip_dur, transition_sec, prefix):
    """共用 helper：side-effect-free monkey-patch，跑一次 MF_ConcatVideos.concat
    並回傳「captured ffmpeg cmd + filter_complex 字串」給呼叫端做斷言。

    為什麼要抽出來：原本 3 個 R8 testcase 各自手動 patch + finally 還原，但每個 finally
    都漏了 `cv.run_ffmpeg` 還原 → 後續 test_review_p0_concat_special_paths 看到的 cv.run_ffmpeg
    是 fake (永遠 True)，concat 假裝成功不寫檔、assert os.path.exists 全 False。
    抽 helper 後保證每個 caller 都會走同一條 save+restore 路徑、不可能漏。
    """
    import comfyui_MediaForge.nodes.concat_videos as cv
    import comfyui_MediaForge.utils.ffmpeg as ff

    orig_cv_run = cv.run_ffmpeg
    orig_probe = ff.probe
    orig_pvd = ff.probe_video_duration
    captured = {}

    def fake_run_ffmpeg(cmd, tag="FFmpeg"):
        captured["cmd"] = cmd
        return True

    cv.run_ffmpeg = fake_run_ffmpeg
    ff.probe_video_duration = lambda p: clip_dur
    ff.probe = lambda p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
    try:
        with tempfile.TemporaryDirectory() as td:
            c1, c2 = os.path.join(td, "c1.mp4"), os.path.join(td, "c2.mp4")
            for p in (c1, c2):
                open(p, "wb").close()

            node = cv.MF_ConcatVideos()
            node.concat(
                video_paths=f"{c1}\n{c2}",
                filename_prefix=f"MediaForge/{prefix}",
                mode="transcode", transition_sec=transition_sec, transition_type="fade",
                fps=24.0, width=320, height=180, crf=23,
            )
    finally:
        cv.run_ffmpeg = orig_cv_run
        ff.probe = orig_probe
        ff.probe_video_duration = orig_pvd

    cmd = captured["cmd"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    return cmd, fc


def test_concat_transition_clamp_never_exceeds_shortest_clip():
    """[R8 P2] R6 的 `max(0.05, shortest*0.99)` floor 會在 shortest<50ms 時超過 clip。
    無 floor 的 clamp `shortest*0.99` 必須恆 < shortest。"""
    import re

    _, fc = _concat_xfade_clamp_case(
        clip_dur=0.02, transition_sec=0.5, prefix="test_r8_clamp_short",
    )
    m = re.search(r"xfade=transition=fade:duration=([\d.]+)", fc)
    assert m, f"應啟用 xfade（0.02s clip 仍能用 ~0.0198s xfade）:\n{fc}"
    dur = float(m.group(1))
    assert dur < 0.02, (
        f"R8 P2 regression: transition duration={dur} >= shortest clip 0.02s — "
        "舊 floor (0.05) 行為又冒出來"
    )
    print(f"[OK] R8 P2: shortest=0.02s + transition=0.5s → clamped to {dur:.5f}s (< 0.02)")


def test_concat_transition_disabled_for_microscopic_clips():
    """另外驗：clip 真的太短（< 1ms 等級）時，整段降階為純 concat。"""
    _, fc = _concat_xfade_clamp_case(
        clip_dur=0.0005, transition_sec=0.5, prefix="test_r8_microscopic",
    )
    assert "xfade=" not in fc, f"clip < 1ms 應走純 concat:\n{fc}"
    assert "concat=n=2:v=1:a=1" in fc
    print("[OK] R8 P2 補: < 1ms clip 自動降階純 concat")


def test_concat_transition_clamp_moderate_clips():
    """補：moderate 短 clip (0.3s) + 1s transition → clamp 到 0.297s，不該 fall back 純 concat。"""
    import re

    _, fc = _concat_xfade_clamp_case(
        clip_dur=0.3, transition_sec=1.0, prefix="test_r8_clamp_moderate",
    )
    m = re.search(r"xfade=transition=fade:duration=([\d.]+)", fc)
    assert m, f"應啟用 xfade:\n{fc}"
    dur = float(m.group(1))
    assert dur < 0.3, f"clamp 到 0.3 之下，但 dur={dur}"
    assert dur > 0.001
    print(f"[OK] R8 P2 補: 0.3s clip + 1s transition → clamped to {dur:.4f}s")


if __name__ == "__main__":
    test_resolve_output_path_codec_aware_ext_independence()
    test_save_prores_writes_mov_via_filename_prefix()
    test_save_h264_writes_mp4_via_filename_prefix()
    test_concat_transition_clamp_never_exceeds_shortest_clip()
    test_concat_transition_disabled_for_microscopic_clips()
    test_concat_transition_clamp_moderate_clips()
    print("\n=== Codex R8 fixes (P3-7 rewritten): all 6 cases passed ===")
