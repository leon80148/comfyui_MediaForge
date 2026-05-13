"""Regression test for P0-7: whisper_transcribe must raise (not silent-empty SRT)
when input has no audio stream.

Spec：
1. If input file has zero audio streams → raise with friendly message *before*
   calling ffmpeg / model（不是把 ffmpeg 的「Output file does not contain any
   stream」吐給使用者，那看不出是 input 缺 audio 還是 ffmpeg 壞）。
2. Defensive：抽完 WAV 後驗 size > WAV header (~44 bytes)；0-byte / 太小 raise。

跑法：
    python tests/test_review_p0_whisper_no_audio.py
    python -m pytest tests/test_review_p0_whisper_no_audio.py
"""
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


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _make_silent_video(path):
    """純 video 無 audio stream（lavfi testsrc）。"""
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
        "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", path,
    ], check=True, capture_output=True)


def test_extract_wav_raises_on_video_without_audio_stream():
    """Spec：input 無 audio stream → _extract_wav 必須 raise 帶友善訊息。

    目前 ffmpeg 行為：returncode != 0 + "Output file does not contain any stream"。
    我們應該先 probe 偵測「no audio stream」場景、給友善訊息（指明是 input 缺音軌、
    不是 ffmpeg 配置問題），而非直接把 ffmpeg stderr tail 吐給使用者。
    """
    if not _has_ffmpeg():
        print("[SKIP] 找不到 ffmpeg，略過")
        return

    from comfyui_MediaForge.nodes.whisper_transcribe import _extract_wav

    with tempfile.TemporaryDirectory() as td:
        silent = os.path.join(td, "silent.mp4")
        _make_silent_video(silent)

        try:
            _extract_wav(silent)
        except RuntimeError as e:
            msg = str(e)
            # 友善訊息應該指明「沒 audio stream」，而非只丟 ffmpeg 的 generic 訊息
            assert ("沒有" in msg or "no audio" in msg.lower() or "audio stream" in msg.lower()), (
                f"P0-7 regression: error message 沒 hint 出 audio stream 缺失：\n{msg}"
            )
            print(f"[OK] P0-7: no-audio video → 友善 raise (msg head: {msg[:80]!r}…)")
            return
        raise AssertionError("[FAIL] _extract_wav 對無 audio video 應 raise，但沒有")


def test_extract_wav_raises_on_zero_byte_output():
    """Defense-in-depth：若 ffmpeg 不知怎麼吐 0-byte WAV (舊版 ffmpeg / static-ffmpeg
    fallback)，size check 還能擋下。Mock subprocess.run 模擬。"""
    from unittest.mock import patch
    from comfyui_MediaForge.nodes import whisper_transcribe as wt

    class _FakeProc:
        def __init__(self):
            self.returncode = 0   # 假裝成功
            self.stderr = b""

    with tempfile.TemporaryDirectory() as td:
        # 創一個假的 input file 通過 os.path.exists（_extract_wav 沒 check input；
        # 這裡只是給 ffmpeg subprocess 跑時不會 fail，但我們 mock 掉了所以不重要）
        fake_input = os.path.join(td, "fake.mp4")
        open(fake_input, "wb").close()

        # 跳過 audio-stream probe（讓 _extract_wav 真的進到 subprocess.run），
        # 然後 mock subprocess.run 模擬 ffmpeg 成功但留 0-byte output。
        def _bypass_probe(_):
            return True  # 假裝 input 有 audio

        def _fake_run(cmd, *a, **kw):
            # cmd 最後一個 arg 是 output path；建 0-byte 檔案
            out = cmd[-1]
            open(out, "wb").close()
            return _FakeProc()

        with patch.object(wt, "_input_has_audio_stream", create=True, side_effect=_bypass_probe):
            with patch("subprocess.run", side_effect=_fake_run):
                try:
                    wt._extract_wav(fake_input)
                except RuntimeError as e:
                    msg = str(e)
                    assert ("0" in msg or "空" in msg or "byte" in msg.lower() or "size" in msg.lower()), (
                        f"P0-7 regression: 0-byte WAV 應 raise size-related msg：\n{msg}"
                    )
                    print(f"[OK] P0-7 defense-in-depth: 0-byte WAV → raise (msg: {msg[:80]!r}…)")
                    return
                raise AssertionError("[FAIL] 0-byte WAV 應 raise 但沒有")


def test_extract_wav_works_on_video_with_audio():
    """Sanity：影片有 audio stream 時 _extract_wav 應正常產出 valid WAV。"""
    if not _has_ffmpeg():
        print("[SKIP] 找不到 ffmpeg，略過 sanity test")
        return

    from comfyui_MediaForge.nodes.whisper_transcribe import _extract_wav

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "with_audio.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", src,
        ], check=True, capture_output=True)

        wav = _extract_wav(src)
        try:
            assert os.path.exists(wav)
            assert os.path.getsize(wav) > 1000, (
                f"WAV size 太小 ({os.path.getsize(wav)} bytes)；可能 extract 走 silent path"
            )
            print(f"[OK] P0-7 sanity: with-audio video → valid WAV ({os.path.getsize(wav)} bytes)")
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass


if __name__ == "__main__":
    test_extract_wav_raises_on_video_without_audio_stream()
    test_extract_wav_raises_on_zero_byte_output()
    test_extract_wav_works_on_video_with_audio()
    print("\n=== P0-7 whisper no-audio guard: all cases passed ===")
