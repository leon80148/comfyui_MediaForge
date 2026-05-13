"""Regression tests for Codex Round 2 review findings.

跑法：python tests/test_codex_r2_fixes.py
"""
import os
import sys
import tempfile

# 用 parent (custom_nodes/) 作 sys.path root，讓 `comfyui_MediaForge.xxx` 包裝匯入可解析
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _CUSTOM_NODES not in sys.path:
    sys.path.insert(0, _CUSTOM_NODES)

# 也加 plugin dir 本身，方便 utils.xxx 直接匯入（給 IR / video_io test 用）
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# Standalone 跑（python tests/foo.py）時 pytest 不會 auto-load conftest，
# 手動 trigger 它的 folder_paths stub 安裝。
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
import conftest  # noqa: E402,F401
from conftest import unpack  # noqa: E402


def test_trim_concat_interleaves_video_audio_pairs():
    """[R2 P1] concat filter 要求 [v0][a0][v1][a1]...，不能 all-v 後接 all-a。"""
    from comfyui_MediaForge.nodes.trim_by_ranges import MF_TrimByRanges

    keep = [[0.0, 1.0], [2.0, 3.0], [4.0, 5.0]]
    cmd = MF_TrimByRanges._build_concat_command(
        "/tmp/fake.mp4", "/tmp/out.mp4", keep, xfade=0, has_audio=True,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    # 取 concat=... 那段的 input 部分
    concat_clause = [p for p in filter_complex.split(";") if "concat=n=" in p][0]
    inputs_part = concat_clause.split("concat=")[0]

    # 預期：[v0][a0][v1][a1][v2][a2]
    expected = "[v0][a0][v1][a1][v2][a2]"
    assert inputs_part == expected, (
        f"concat 輸入排列錯誤\n  expected: {expected}\n  got: {inputs_part}\n  全段: {filter_complex}"
    )
    print("[OK] R2 P1: trim concat interleaves [v_i][a_i] pairs 通過")


def test_trim_concat_no_audio_video_only():
    """同上 — has_audio=False 時純 video labels 順序排列。"""
    from comfyui_MediaForge.nodes.trim_by_ranges import MF_TrimByRanges

    keep = [[0.0, 1.0], [2.0, 3.0]]
    cmd = MF_TrimByRanges._build_concat_command(
        "/tmp/fake.mp4", "/tmp/out.mp4", keep, xfade=0, has_audio=False,
    )
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    concat_clause = [p for p in filter_complex.split(";") if "concat=n=" in p][0]
    inputs_part = concat_clause.split("concat=")[0]
    assert inputs_part == "[v0][v1]"
    assert "concat=n=2:v=1:a=0[outv]" in concat_clause
    # 確認 maps 含 -an
    assert "-an" in cmd
    print("[OK] R2 P1: trim no-audio mode 通過")


def test_load_silent_video_returns_none_audio():
    """[R2 P2] silent video 應吐 audio=None，不能合成 1-sample 假音訊。"""
    import subprocess
    from comfyui_MediaForge.nodes.load_video_frames import MF_LoadVideoFrames

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "silent.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
             "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", src],
            check=True, capture_output=True,
        )

        node = MF_LoadVideoFrames()
        frames, audio, fps, w, h, n, meta = node.load(
            video_path=src, target_fps=0.0, max_frames=0,
            load_audio=True, audio_sr=0,
        )
        assert audio is None, f"silent video 應 None，但 audio={type(audio).__name__}"
        assert frames.shape[0] > 0
        print(f"[OK] R2 P2: silent video → audio=None (frames={frames.shape[0]}) 通過")


def test_save_video_drops_degenerate_audio_dict():
    """[R2 P2 補] Save 接到 T<=1 的 AUDIO dict 也要當成無音訊處理，避免 -shortest 截檔。"""
    import subprocess
    import torch
    from comfyui_MediaForge.nodes.save_video_frames import MF_SaveVideoFrames

    with tempfile.TemporaryDirectory() as _td:
        # 註：Save 節點透過 filename_prefix + counter 寫到 ComfyUI output_dir，不是這裡的 td。
        # 本 test 用 conftest 的 folder_paths stub（指 tempfile.gettempdir()/mf_test_output），
        # 由節點回傳值取得實際輸出路徑。
        frames = torch.rand(30, 64, 64, 3)  # 30 frames @ 30 fps = 1.0s
        # 假合成的 1-sample audio（模擬 v1.0 行為）
        fake_audio = {
            "waveform": torch.zeros((1, 2, 1), dtype=torch.float32),
            "sample_rate": 44100,
        }
        node = MF_SaveVideoFrames()
        (out,) = unpack(node.save(
            frames=frames, filename_prefix="MediaForge/test_r2_save_degenerate",
            fps=30.0,
            codec="h264 (libx264)", encode_mode="crf", crf=23,
            bitrate_kbps=4000, target_size_mb=8.0,
            preset="ultrafast", pix_fmt_override="",
            audio=fake_audio,
        ))
        # 用 ffprobe 驗 duration
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", out],
            check=True, capture_output=True, text=True,
        )
        import json
        d = json.loads(info.stdout)
        dur = float(d["format"]["duration"])
        # 應該接近 1.0s (30 frames @ 30 fps)，不該被截到 1/44100 ≈ 0 秒
        assert dur > 0.5, f"輸出 duration={dur:.4f}s，疑被退化 audio 的 -shortest 截短"
        print(f"[OK] R2 P2: degenerate audio dict 被剔除，duration={dur:.2f}s")


if __name__ == "__main__":
    test_trim_concat_interleaves_video_audio_pairs()
    test_trim_concat_no_audio_video_only()
    test_load_silent_video_returns_none_audio()
    test_save_video_drops_degenerate_audio_dict()
    print("\n=== Codex R2 fixes: all 4 cases passed ===")
