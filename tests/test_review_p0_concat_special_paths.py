"""Regression test for P0-8: concat demuxer Windows + special-char paths.

驗證：apostrophe / Chinese / spaces / 多種 mix 路徑透過 concat_videos.MF_ConcatVideos
都能跑成功（mode=copy 走 demuxer，list 檔內 escape 須能被 FFmpeg 解析）。

跑法：
    python tests/test_review_p0_concat_special_paths.py
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


def _make_clip(path):
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=duration=0.3:size=64x48:rate=10",
        "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", path,
    ], check=True, capture_output=True)


def _run_concat_copy(filenames, prefix):
    """直接呼叫 MF_ConcatVideos.concat() (mode=copy) 並驗 output 寫成。"""
    from comfyui_MediaForge.nodes.concat_videos import MF_ConcatVideos

    with tempfile.TemporaryDirectory() as td:
        paths = []
        for f in filenames:
            p = os.path.join(td, f)
            _make_clip(p)
            paths.append(p)
        node = MF_ConcatVideos()
        (out,) = node.concat(
            video_paths="\n".join(paths),
            filename_prefix=f"MediaForge/{prefix}",
            mode="copy", transition_sec=0.0, transition_type="fade",
            fps=24.0, width=320, height=180, crf=23,
        )
        assert os.path.exists(out) and os.path.getsize(out) > 1000, (
            f"output 不存在或太小: {out}"
        )
        return out


def test_concat_demuxer_handles_plain_paths():
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    out = _run_concat_copy(["plain1.mp4", "plain2.mp4"], "test_p08_plain")
    print(f"[OK] P0-8 sanity: plain paths → {os.path.basename(out)}")


def test_concat_demuxer_handles_paths_with_spaces():
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    out = _run_concat_copy(["with space 1.mp4", "with space 2.mp4"], "test_p08_spaces")
    print(f"[OK] P0-8: paths with spaces → {os.path.basename(out)}")


def test_concat_demuxer_handles_paths_with_apostrophe():
    """關鍵 case：FFmpeg concat demuxer 對 single-quote 內 `'` 必須走 close-escape-reopen
    pattern (`'foo'\\''bar'`). 之前實作 (`safe.replace("'", "'\\''")`) 已正確、
    本 test 鎖住該 escape 對 ffmpeg 8.x 仍可用、不會被新版 parser 改寫破掉。
    """
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    out = _run_concat_copy(["don't 1.mp4", "don't 2.mp4"], "test_p08_apostrophe")
    print(f"[OK] P0-8 KEY: paths with apostrophe → {os.path.basename(out)}")


def test_concat_demuxer_handles_chinese_paths():
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    out = _run_concat_copy(["中文1.mp4", "中文2.mp4"], "test_p08_chinese")
    print(f"[OK] P0-8: Chinese paths → {os.path.basename(out)}")


def test_concat_demuxer_handles_all_three_combined():
    """三毒：apostrophe + Chinese + spaces 同檔名。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    out = _run_concat_copy(
        ["mixed don't 中文 1.mp4", "mixed don't 中文 2.mp4"],
        "test_p08_combined",
    )
    print(f"[OK] P0-8 BOSS: all three combined → {os.path.basename(out)}")


if __name__ == "__main__":
    test_concat_demuxer_handles_plain_paths()
    test_concat_demuxer_handles_paths_with_spaces()
    test_concat_demuxer_handles_paths_with_apostrophe()
    test_concat_demuxer_handles_chinese_paths()
    test_concat_demuxer_handles_all_three_combined()
    print("\n=== P0-8 concat demuxer special-char paths: all cases passed ===")
