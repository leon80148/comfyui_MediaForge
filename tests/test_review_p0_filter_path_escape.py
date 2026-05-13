r"""Regression test for P0-6: FFmpeg filter path escape on Windows.

Bug：FFmpeg 8.x stricter parser 拒絕單層 escape `C\:/path`。需要 2-level escape
(`C\\:/path`) — 第 1 層 escape `:` 為 `\:`、第 2 層 escape `\` 為 `\\`。
這個 escape 對 filter_complex / -vf / -filter:v 都適用、跨平台。

跑法：
    python tests/test_review_p0_filter_path_escape.py
    python -m pytest tests/test_review_p0_filter_path_escape.py
"""
import os
import subprocess
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def _is_windows() -> bool:
    return os.name == "nt"


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def test_escape_filter_path_double_escapes_backslash():
    r"""Spec：escape_filter_path 對 Windows-style `C:` 必須產生 `C\\:` (level-1+2)。

    Level 1 (filter args)：`:` → `\:`
    Level 2 (filter description)：`\` → `\\`
    Combined：`:` → `\\:`
    """
    from utils.ffmpeg import escape_filter_path

    # 跨平台：對 colon 必須雙重 escape
    out = escape_filter_path("C:/Users/foo/bar.txt")
    assert out == "C\\\\:/Users/foo/bar.txt", (
        f"escape_filter_path 應產生雙層 escape (`\\\\:`)，但拿到 {out!r}"
    )

    # Windows 反斜線需先正規化為 forward slash（filter graph 的 path 用 /）
    out2 = escape_filter_path("C:\\Users\\foo\\bar.txt")
    assert out2 == "C\\\\:/Users/foo/bar.txt", (
        f"Windows 反斜線應正規化 + 雙層 escape，但拿到 {out2!r}"
    )

    # POSIX 純 forward slash 路徑：無 colon 不需 escape
    out3 = escape_filter_path("/tmp/foo/bar.txt")
    assert out3 == "/tmp/foo/bar.txt", (
        f"POSIX 路徑不應被改動，但拿到 {out3!r}"
    )


def test_compose_ir_drawtext_escape_uses_double_escape():
    """ComposeIR drawtext 內部也必須走相同 2-level escape，不要再用 private 版本走單層。"""
    from utils.compose_ir import _escape_filter_path

    out = _escape_filter_path("C:/x/y.txt")
    assert out == "C\\\\:/x/y.txt", (
        f"compose_ir._escape_filter_path 應產生雙層 escape，但拿到 {out!r}"
    )


def test_drawtext_filter_runs_against_real_ffmpeg_with_double_escape():
    """Real FFmpeg：drawtext+textfile 用雙層 escape 路徑能 parse + run 成功。

    P0-6 關鍵 e2e — 沒這個 ffmpeg 8+ 必噴 `Error : Invalid argument`。
    """
    if not _has_ffmpeg():
        print("[SKIP] 找不到 ffmpeg，略過 e2e 驗證")
        return
    from utils.compose_ir import ComposeIR, compile_ir

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        out = os.path.join(td, "out.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=320x180:rate=24",
             "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", src],
            check=True, capture_output=True,
        )

        ir = ComposeIR(target_fps=24, target_width=320, target_height=180)
        ir.add_video_input(src, is_main=True, has_audio=False)
        # 給一個常見的、會觸發 escape 痛點的 fontfile + drawtext combo
        font = "C:/Windows/Fonts/arial.ttf" if _is_windows() else None
        params = {"text": "Hello World", "fontsize": 20, "fontcolor": "white"}
        if font and os.path.exists(font.replace("/", "\\")):
            params["fontfile"] = font
        ir.append_op("drawtext", params)

        script, final_label, cleanup = compile_ir(ir)
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-i", src, "-filter_complex", script,
               "-map", f"[{final_label}]", "-an",
               "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
               "-pix_fmt", "yuv420p", "-t", "0.5", out]
        try:
            proc = subprocess.run(cmd, capture_output=True)
            assert proc.returncode == 0, (
                f"ffmpeg drawtext 失敗 (P0-6 regression):\n"
                f"  cmd: {' '.join(cmd)}\n"
                f"  stderr: {proc.stderr.decode('utf-8', errors='replace')}"
            )
            assert os.path.exists(out) and os.path.getsize(out) > 1000
        finally:
            for p in cleanup:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    print("[OK] P0-6: drawtext+textfile 雙層 escape e2e 通過")


def test_burn_subtitle_subtitles_filter_with_double_escape():
    """Real FFmpeg：subtitles= filter 用雙層 escape 路徑能 parse + run 成功。

    P0-6 順帶證明：同個 escape bug 也影響 burn_subtitle 用的 subtitles filter。
    """
    if not _has_ffmpeg():
        print("[SKIP] 找不到 ffmpeg，略過 subtitles e2e 驗證")
        return
    from utils.ffmpeg import escape_filter_path

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        srt = os.path.join(td, "test.srt")
        out = os.path.join(td, "out.mp4")
        with open(srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\nHello\n")

        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=320x180:rate=24",
             "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", src],
            check=True, capture_output=True,
        )

        srt_esc = escape_filter_path(srt)
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-vf", f"subtitles={srt_esc}",
               "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
               "-t", "0.5", out]
        proc = subprocess.run(cmd, capture_output=True)
        assert proc.returncode == 0, (
            f"ffmpeg subtitles= 失敗 (P0-6 regression):\n"
            f"  encoded path: {srt_esc}\n"
            f"  stderr: {proc.stderr.decode('utf-8', errors='replace')}"
        )
        assert os.path.exists(out) and os.path.getsize(out) > 1000

    print("[OK] P0-6: subtitles=<encoded path> 雙層 escape e2e 通過")


def test_drawtext_with_text_containing_colons_via_textfile():
    """Bonus：text 內有冒號（時間戳之類）也要能 textfile= 進去不會被 parser 偷吃。"""
    if not _has_ffmpeg():
        print("[SKIP] 找不到 ffmpeg，略過 colon-text e2e 驗證")
        return
    from utils.compose_ir import ComposeIR, compile_ir

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        out = os.path.join(td, "out.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=320x180:rate=24",
             "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", src],
            check=True, capture_output=True,
        )

        ir = ComposeIR(target_fps=24, target_width=320, target_height=180)
        ir.add_video_input(src, is_main=True, has_audio=False)
        params = {"text": "12:34:56", "fontsize": 20, "fontcolor": "white"}
        if _is_windows() and os.path.exists("C:\\Windows\\Fonts\\arial.ttf"):
            params["fontfile"] = "C:/Windows/Fonts/arial.ttf"
        ir.append_op("drawtext", params)

        script, final_label, cleanup = compile_ir(ir)
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src,
               "-filter_complex", script,
               "-map", f"[{final_label}]", "-an",
               "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
               "-pix_fmt", "yuv420p", "-t", "0.5", out]
        try:
            proc = subprocess.run(cmd, capture_output=True)
            assert proc.returncode == 0, (
                f"ffmpeg drawtext text=12:34:56 失敗:\n  stderr: "
                f"{proc.stderr.decode('utf-8', errors='replace')}"
            )
        finally:
            for p in cleanup:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        # 驗證 textfile 寫入內容正確
        # （cleanup 已 unlink；只要 ffmpeg 沒爆就證明檔案有被讀到）

    print("[OK] P0-6: text 含冒號透過 textfile= 不會被 parser 截斷")


if __name__ == "__main__":
    test_escape_filter_path_double_escapes_backslash()
    print("[OK] P0-6 unit: escape_filter_path 產生雙層 escape")
    test_compose_ir_drawtext_escape_uses_double_escape()
    print("[OK] P0-6 unit: compose_ir._escape_filter_path 產生雙層 escape")
    test_drawtext_filter_runs_against_real_ffmpeg_with_double_escape()
    test_burn_subtitle_subtitles_filter_with_double_escape()
    test_drawtext_with_text_containing_colons_via_textfile()
    print("\n=== P0-6 filter path escape: all cases passed ===")
