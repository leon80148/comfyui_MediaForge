"""Regression tests for Codex Round 8 review findings.

跑法：python tests/test_codex_r8_fixes.py
"""
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
if _CUSTOM_NODES not in sys.path:
    sys.path.insert(0, _CUSTOM_NODES)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def test_prores_with_mp4_auto_corrected_to_mov():
    """[R8 P2] ProRes + .mp4 → MP4 muxer 不支援；自動改 .mov 不該 raise。"""
    from comfyui_MediaForge.nodes.save_video_frames import _ensure_compatible_container

    out = _ensure_compatible_container("/tmp/x.mp4", "prores_ks")
    assert out == "/tmp/x.mov", f"預期 /tmp/x.mov，但拿到 {out}"

    out2 = _ensure_compatible_container("/tmp/x.m4v", "prores_ks")
    assert out2 == "/tmp/x.mov"

    # 其他 codec 不受影響
    assert _ensure_compatible_container("/tmp/x.mp4", "libx264") == "/tmp/x.mp4"
    assert _ensure_compatible_container("/tmp/x.mp4", "libx265") == "/tmp/x.mp4"

    # ProRes + .mov 不變
    assert _ensure_compatible_container("/tmp/x.mov", "prores_ks") == "/tmp/x.mov"
    print("[OK] R8 P2: ProRes / MP4 → MOV 自動修正")


def test_save_prores_with_mp4_writes_mov():
    """端到端：MF_SaveVideoFrames 接 prores 指定 .mp4 output → 寫 .mov、不該 fail。"""
    import torch
    from comfyui_MediaForge.nodes.save_video_frames import MF_SaveVideoFrames

    with tempfile.TemporaryDirectory() as td:
        requested = os.path.join(td, "out.mp4")
        frames = torch.rand(10, 64, 64, 3)
        node = MF_SaveVideoFrames()
        (result,) = node.save(
            frames=frames, output_path=requested, fps=10.0,
            codec="prores (prores_ks)", encode_mode="crf", crf=23,
            bitrate_kbps=4000, target_size_mb=8.0,
            preset="medium", pix_fmt_override="",
        )
        # 應該回傳 .mov 路徑
        assert result.endswith(".mov"), f"output_path 應結尾 .mov，但 {result}"
        assert os.path.exists(result), f"檔案不存在：{result}"
        print(f"[OK] R8 P2: ProRes save .mp4 → 自動寫成 {os.path.basename(result)}")


def test_concat_transition_clamp_never_exceeds_shortest_clip():
    """[R8 P2] R6 的 `max(0.05, shortest*0.99)` floor 會在 shortest<50ms 時超過 clip。
    無 floor 的 clamp `shortest*0.99` 必須恆 < shortest。"""
    import comfyui_MediaForge.nodes.concat_videos as cv

    captured = {}

    def fake_run_ffmpeg(cmd, tag="FFmpeg"):
        captured["cmd"] = cmd
        return True
    cv.run_ffmpeg = fake_run_ffmpeg

    import comfyui_MediaForge.utils.ffmpeg as ff
    saved_probe = ff.probe
    saved_pvd = ff.probe_video_duration
    # 兩段短 clips：0.02s each (低於舊 floor 0.05s 的 corner case)
    ff.probe_video_duration = lambda p: 0.02
    ff.probe = lambda p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}

    try:
        with tempfile.TemporaryDirectory() as td:
            c1, c2 = os.path.join(td, "c1.mp4"), os.path.join(td, "c2.mp4")
            for p in (c1, c2):
                open(p, "wb").close()

            node = cv.MF_ConcatVideos()
            node.concat(
                video_paths=f"{c1}\n{c2}", output_path=os.path.join(td, "out.mp4"),
                mode="transcode", transition_sec=0.5, transition_type="fade",
                fps=24.0, width=320, height=180, crf=23,
            )
            cmd = captured["cmd"]
            fc = cmd[cmd.index("-filter_complex") + 1]
            import re
            m = re.search(r"xfade=transition=fade:duration=([\d.]+)", fc)
            assert m, f"應啟用 xfade（0.02s clip 仍能用 ~0.0198s xfade）:\n{fc}"
            dur = float(m.group(1))
            # 關鍵驗證：duration 必須 < shortest clip (0.02s)；舊 floor 會給 0.05 > 0.02
            assert dur < 0.02, (
                f"R8 P2 regression: transition duration={dur} >= shortest clip 0.02s — "
                "舊 floor (0.05) 行為又冒出來"
            )
            print(f"[OK] R8 P2: shortest=0.02s + transition=0.5s → clamped to {dur:.5f}s (< 0.02)")
    finally:
        ff.probe = saved_probe
        ff.probe_video_duration = saved_pvd


def test_concat_transition_disabled_for_microscopic_clips():
    """另外驗：clip 真的太短（< 1ms 等級）時，整段降階為純 concat。"""
    import comfyui_MediaForge.nodes.concat_videos as cv

    captured = {}

    def fake_run_ffmpeg(cmd, tag="FFmpeg"):
        captured["cmd"] = cmd
        return True
    cv.run_ffmpeg = fake_run_ffmpeg

    import comfyui_MediaForge.utils.ffmpeg as ff
    saved_probe = ff.probe
    saved_pvd = ff.probe_video_duration
    ff.probe_video_duration = lambda p: 0.0005  # 0.5ms
    ff.probe = lambda p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}

    try:
        with tempfile.TemporaryDirectory() as td:
            c1, c2 = os.path.join(td, "c1.mp4"), os.path.join(td, "c2.mp4")
            for p in (c1, c2):
                open(p, "wb").close()

            node = cv.MF_ConcatVideos()
            node.concat(
                video_paths=f"{c1}\n{c2}", output_path=os.path.join(td, "out.mp4"),
                mode="transcode", transition_sec=0.5, transition_type="fade",
                fps=24.0, width=320, height=180, crf=23,
            )
            cmd = captured["cmd"]
            fc = cmd[cmd.index("-filter_complex") + 1]
            assert "xfade=" not in fc, f"clip < 1ms 應走純 concat:\n{fc}"
            assert "concat=n=2:v=1:a=1" in fc
            print("[OK] R8 P2 補: < 1ms clip 自動降階純 concat")
    finally:
        ff.probe = saved_probe
        ff.probe_video_duration = saved_pvd


def test_concat_transition_clamp_moderate_clips():
    """補：moderate 短 clip (0.3s) + 1s transition → clamp 到 0.297s，不該 fall back 純 concat。"""
    import comfyui_MediaForge.nodes.concat_videos as cv

    captured = {}

    def fake_run_ffmpeg(cmd, tag="FFmpeg"):
        captured["cmd"] = cmd
        return True
    cv.run_ffmpeg = fake_run_ffmpeg

    import comfyui_MediaForge.utils.ffmpeg as ff
    saved_probe = ff.probe
    saved_pvd = ff.probe_video_duration
    ff.probe_video_duration = lambda p: 0.3
    ff.probe = lambda p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}

    try:
        with tempfile.TemporaryDirectory() as td:
            c1, c2 = os.path.join(td, "c1.mp4"), os.path.join(td, "c2.mp4")
            for p in (c1, c2):
                open(p, "wb").close()

            node = cv.MF_ConcatVideos()
            node.concat(
                video_paths=f"{c1}\n{c2}", output_path=os.path.join(td, "out.mp4"),
                mode="transcode", transition_sec=1.0, transition_type="fade",
                fps=24.0, width=320, height=180, crf=23,
            )
            cmd = captured["cmd"]
            fc = cmd[cmd.index("-filter_complex") + 1]
            import re
            m = re.search(r"xfade=transition=fade:duration=([\d.]+)", fc)
            assert m, f"應啟用 xfade:\n{fc}"
            dur = float(m.group(1))
            assert dur < 0.3, f"clamp 到 0.3 之下，但 dur={dur}"
            assert dur > 0.001
            print(f"[OK] R8 P2 補: 0.3s clip + 1s transition → clamped to {dur:.4f}s")
    finally:
        ff.probe = saved_probe
        ff.probe_video_duration = saved_pvd


if __name__ == "__main__":
    test_prores_with_mp4_auto_corrected_to_mov()
    test_save_prores_with_mp4_writes_mov()
    test_concat_transition_clamp_never_exceeds_shortest_clip()
    test_concat_transition_disabled_for_microscopic_clips()
    test_concat_transition_clamp_moderate_clips()
    print("\n=== Codex R8 fixes: all 5 cases passed ===")
