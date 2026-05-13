"""Regression tests for Codex Round 7 review findings.

跑法：python tests/test_codex_r7_fixes.py
"""
import json
import os
import subprocess
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
import conftest  # noqa: E402,F401


def _ffprobe_duration(path):
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", path],
        check=True, capture_output=True, text=True,
    )
    return float(json.loads(info.stdout)["format"]["duration"])


def test_trim_empty_ranges_remove_mode_keeps_full_video():
    """[R7 P1] MF_DetectSilence 沒偵到靜音 → ranges=[]；mode=remove + 連線
    必須保留整段，不能 fallback 到 ranges_json 預設值。"""
    from comfyui_MediaForge.nodes.trim_by_ranges import MF_TrimByRanges

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k", src],
            check=True, capture_output=True,
        )
        node = MF_TrimByRanges()
        # 故意傳空 list (模擬 MF_DetectSilence 沒偵到任何靜音)
        (out,) = node.trim(
            video_path=src,
            filename_prefix="MediaForge/test_r7_trim_empty_remove",
            mode="remove",
            ranges_json="ignored, should not be used", crossfade_sec=0.0,
            ranges=[],  # ← 連線傳入空 list
        )
        dur = _ffprobe_duration(out)
        # 應接近 2s（整段保留）
        assert 1.8 < dur < 2.2, f"empty ranges + remove 應保留全段 (~2s)，但 dur={dur:.3f}s"
        print(f"[OK] R7 P1: empty SILENCE_RANGES + remove → 整段保留 ({dur:.2f}s)")


def test_trim_empty_ranges_keep_mode_raises():
    """[R7 P1] empty ranges + mode=keep 應 raise，不能 silently 出空檔。"""
    from comfyui_MediaForge.nodes.trim_by_ranges import MF_TrimByRanges

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
             "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", src],
            check=True, capture_output=True,
        )
        node = MF_TrimByRanges()
        try:
            node.trim(
                video_path=src,
                filename_prefix="MediaForge/test_r7_trim_empty_keep",
                mode="keep", ranges_json="ignored", crossfade_sec=0.0, ranges=[],
            )
        except ValueError as e:
            assert "沒有東西可保留" in str(e) or "mode=keep" in str(e)
            print("[OK] R7 P1: empty ranges + keep 正確 raise")
            return
        raise AssertionError("應 raise ValueError 但沒有")


def test_whisper_local_treats_chat_model_as_unset():
    """[R7 P2] faster_whisper_local + cfg.model='gpt-4o-mini' (MF_AIConfig 預設) →
    視為 unset，走 backend default 'base'。"""
    from comfyui_MediaForge.nodes.whisper_transcribe import _looks_like_stt_model

    # Chat models 不該被認作 STT
    assert not _looks_like_stt_model("gpt-4o-mini")
    assert not _looks_like_stt_model("gpt-4")
    assert not _looks_like_stt_model("claude-opus-4")
    assert not _looks_like_stt_model("llama-3.1-8b")
    assert not _looks_like_stt_model("gemini-pro")
    # STT models 應通過
    assert _looks_like_stt_model("base")
    assert _looks_like_stt_model("large-v3")
    assert _looks_like_stt_model("whisper-1")
    assert _looks_like_stt_model("distil-large-v2")
    # 空字串視為非 STT
    assert not _looks_like_stt_model("")
    print("[OK] R7 P2: _looks_like_stt_model 對 chat / STT 區分正確")


def test_concat_demuxer_uses_absolute_paths():
    """[R7 P2] concat list 檔內的路徑必須是 absolute；否則 ffmpeg 從 list 所在目錄解析、
    使用者 cwd 與 list 在 /tmp 時會找不到。"""
    import comfyui_MediaForge.nodes.concat_videos as cv

    with tempfile.TemporaryDirectory() as td:
        c1 = os.path.join(td, "c1.mp4")
        c2 = os.path.join(td, "c2.mp4")
        for p in (c1, c2):
            open(p, "wb").close()

        # 攔截 ffmpeg 跑、只驗 list file content
        captured = {}

        def fake_run_ffmpeg(cmd, tag="FFmpeg"):
            list_file = cmd[cmd.index("-i") + 1]
            with open(list_file, encoding="utf-8") as f:
                captured["list_content"] = f.read()
            return True

        orig = cv.run_ffmpeg
        cv.run_ffmpeg = fake_run_ffmpeg
        try:
            # 用相對路徑當輸入（即使 c1/c2 本身在 td 內）
            cwd = os.getcwd()
            os.chdir(td)
            try:
                node = cv.MF_ConcatVideos()
                node.concat(
                    video_paths="c1.mp4\nc2.mp4",
                    filename_prefix="MediaForge/test_r7_concat_demuxer_abs",
                    mode="copy", transition_sec=0.0, transition_type="fade",
                    fps=24.0, width=320, height=180, crf=23,
                )
            finally:
                os.chdir(cwd)

            content = captured["list_content"]
            for line in content.splitlines():
                # 每行格式 `file '<absolute>'`. POSIX absolute 以 `/` 起頭；
                # Windows absolute 以 drive letter `X:` (例 C:) 起頭。
                m_posix = line.startswith("file '/")
                # 比對 Windows 樣式: `file 'X:\\...'` 或 `file 'X:/...'`
                m_win = (len(line) > 8 and line.startswith("file '")
                         and line[7:9] == ":" + os.sep[0:1] if False  # noqa
                         else False)
                # 更可靠的 Windows check：第 7~9 char 為 `<letter>:` 後接 / 或 \
                m_win = (line.startswith("file '") and len(line) > 9
                         and line[6].isalpha() and line[7] == ":"
                         and line[8] in ("/", "\\"))
                assert m_posix or m_win, (
                    f"list 內路徑非 absolute:\n{content}"
                )
            print("[OK] R7 P2: concat demuxer 用 absolute paths")
        finally:
            cv.run_ffmpeg = orig


def test_loop_audio_truncated_padded_to_video_duration():
    """[R7 P2] 源 video / audio 長度不同時，aloop 跟 video loop 才能同步。
    驗 filter graph 含 apad + atrim=duration=<eff_dur>。"""
    from comfyui_MediaForge.nodes.loop_video import MF_LoopVideo

    # 直接調 _build_filter 看輸出 filter parts
    parts, _, _ = MF_LoopVideo._build_filter(
        mode="strict", eff_dur=3.0, target=10.0, xfade_dur=1.0,
        speed=1.0, reverse=False, has_audio=True, audio_volume=1.0,
    )
    base_a_line = [p for p in parts if "[base_a]" in p][0]
    assert "apad=whole_dur=3.000000" in base_a_line, (
        f"base_a 沒 apad to eff_dur:\n{base_a_line}"
    )
    assert "atrim=duration=3.000000" in base_a_line, (
        f"base_a 沒 atrim to eff_dur:\n{base_a_line}"
    )
    print("[OK] R7 P2: loop_video base_a 對齊到 eff_dur (apad + atrim)")


if __name__ == "__main__":
    test_trim_empty_ranges_remove_mode_keeps_full_video()
    test_trim_empty_ranges_keep_mode_raises()
    test_whisper_local_treats_chat_model_as_unset()
    test_concat_demuxer_uses_absolute_paths()
    test_loop_audio_truncated_padded_to_video_duration()
    print("\n=== Codex R7 fixes: all 5 cases passed ===")
