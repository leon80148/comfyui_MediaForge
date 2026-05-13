"""Regression tests for Codex Round 5 review findings.

跑法：python tests/test_codex_r5_fixes.py
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


def test_drawtext_handles_apostrophe_via_textfile():
    """[R5 P2] drawtext with `Bob's` and `100%` should not break filter parse.
    用 textfile= 載 text 不會有 single-quote / % escape 問題。"""
    from utils.compose_ir import ComposeIR, compile_ir

    ir = ComposeIR()
    ir.add_video_input("/tmp/fake.mp4", is_main=True)
    ir.append_op("drawtext", {"text": "Bob's report — 100%", "fontsize": 24})

    script, _, cleanup = compile_ir(ir)
    try:
        # script 不該含原始 text (text 在 textfile 內)
        assert "Bob's" not in script, f"原 text 被內嵌進 script，可能 escape 失敗:\n{script}"
        assert "textfile=" in script, f"未走 textfile= 路徑:\n{script}"
        # cleanup 路徑應該真的有寫入正確 text
        assert len(cleanup) == 1
        with open(cleanup[0], encoding="utf-8") as f:
            written = f.read()
        assert written == "Bob's report — 100%"
        print("[OK] R5 P2: drawtext apostrophe + % via textfile= 通過")
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass


def test_drawtext_end_to_end_with_tricky_chars():
    """真 ffmpeg 跑：drawtext text 含 ' 跟 % 也能編碼出檔。"""
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
        ir.append_op("drawtext", {"text": "It's 50%!", "fontsize": 20, "fontcolor": "white"})

        script, final_label, cleanup = compile_ir(ir)
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-i", src, "-filter_complex", script,
               "-map", f"[{final_label}]", "-an",
               "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
               "-pix_fmt", "yuv420p", "-t", "1.0", out]
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True)
            assert proc.returncode == 0, (
                f"ffmpeg failed:\nstderr={proc.stderr.decode('utf-8', errors='replace')}\n"
                f"script={script}"
            )
            assert os.path.getsize(out) > 1000
        finally:
            for p in cleanup:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        print("[OK] R5 P2: drawtext e2e with 'It's 50%!' 通過")


def test_probe_video_duration_uses_video_stream_not_container():
    """[R5 P2] 影片時長 1s + 音軌時長 3s → probe_video_duration 回 ~1s。"""
    from utils.ffmpeg import probe_duration, probe_video_duration

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "v1s_a3s.mp4")
        # 用 lavfi 合成 1s video + 3s audio mux 在一起
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k", src],
            check=True, capture_output=True,
        )

        container_dur = probe_duration(src)
        video_dur = probe_video_duration(src)
        # container 應 ~3s (max stream)，video 應 ~1s
        assert container_dur is not None and container_dur > 2.5, (
            f"container_dur={container_dur} should be ~3"
        )
        assert video_dur is not None and 0.8 < video_dur < 1.3, (
            f"video_dur={video_dur} should be ~1"
        )
        print(f"[OK] R5 P2: probe_video_duration={video_dur:.3f}s vs container={container_dur:.3f}s")


def test_concat_trims_audio_to_video_duration():
    """[R5 P2] 兩段 1s 影片、第一段帶 3s 音軌 → concat 後應 ~2s 不該 ~4s。"""
    from comfyui_MediaForge.nodes.concat_videos import MF_ConcatVideos

    with tempfile.TemporaryDirectory() as td:
        # clip1: 1s video + 3s audio
        c1 = os.path.join(td, "c1.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=320x180:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
             "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k", c1],
            check=True, capture_output=True,
        )
        # clip2: 1s video + 1s audio
        c2 = os.path.join(td, "c2.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=320x180:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
             "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k", c2],
            check=True, capture_output=True,
        )
        node = MF_ConcatVideos()
        (out,) = node.concat(
            video_paths=f"{c1}\n{c2}",
            filename_prefix="MediaForge/test_r5_concat_audio_trim",
            mode="transcode", transition_sec=0.0, transition_type="fade",
            fps=24.0, width=320, height=180, crf=23,
        )
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", out],
            check=True, capture_output=True, text=True,
        )
        d = float(json.loads(info.stdout)["format"]["duration"])
        # 期待 ~2s (1s + 1s)；若 audio 沒 trim 會被拉到 ~4s (3s + 1s)
        assert 1.5 < d < 2.5, f"concat 輸出 duration={d:.3f}s, 應為 ~2s (audio 未 trim 時會 ~4s)"
        print(f"[OK] R5 P2: concat audio trimmed to per-clip video duration ({d:.2f}s)")


def test_whisper_provider_aware_fallback():
    """[R5 P2] faster_whisper_local + blank override + cfg.model='large-v3' → 用 'large-v3'。
    與 R4 fix 並存：openai_compatible 在同樣 cfg 下仍走 'whisper-1' STT 預設。"""
    def resolve(override, cfg_model, provider):
        u = (override or "").strip()
        c = (cfg_model or "").strip()
        if u:
            return u
        if provider == "faster_whisper_local":
            return c or "base"
        return "whisper-1"

    # R5 case: local backend 尊重 cfg.model
    assert resolve("", "large-v3", "faster_whisper_local") == "large-v3"
    assert resolve("", "small", "faster_whisper_local") == "small"
    assert resolve("", "", "faster_whisper_local") == "base"
    # R4 case: openai_compat 不沾 cfg.model
    assert resolve("", "gpt-4o-mini", "openai_compatible") == "whisper-1"
    assert resolve("", "whisper-1", "openai_compatible") == "whisper-1"
    # user override 任何 provider 都優先
    assert resolve("custom", "x", "faster_whisper_local") == "custom"
    print("[OK] R5 P2: Whisper provider-aware fallback 通過")


if __name__ == "__main__":
    test_drawtext_handles_apostrophe_via_textfile()
    test_drawtext_end_to_end_with_tricky_chars()
    test_probe_video_duration_uses_video_stream_not_container()
    test_concat_trims_audio_to_video_duration()
    test_whisper_provider_aware_fallback()
    print("\n=== Codex R5 fixes: all 5 cases passed ===")
