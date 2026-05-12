"""End-to-end Compose pipeline smoke test — exercises actual ffmpeg.

跑法：python tests/test_compose_e2e.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.compose_ir import ComposeIR, compile_ir, write_filter_script_if_long  # noqa: E402
from utils.ffmpeg import probe  # noqa: E402


def make_test_video(path, dur=2, w=320, h=180, fps=24):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=duration={dur}:size={w}x{h}:rate={fps}",
            "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
            path,
        ],
        check=True, capture_output=True,
    )


def make_test_image(path, w=80, h=80):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=red:size={w}x{h}:rate=1",
            "-frames:v", "1",
            path,
        ],
        check=True, capture_output=True,
    )


def run_compose(ir, output_path):
    """模擬 MF_ComposeFinalize 的核心邏輯：compile → ffmpeg。"""
    ir = ir.clone()
    script, final_label, drawtext_cleanup = compile_ir(ir)
    filter_arg_value, tmp_path = write_filter_script_if_long(script)

    cmd = ["ffmpeg", "-y", "-v", "error"]
    sorted_inputs = sorted(ir.inputs.values(), key=lambda x: x.index)
    for ent in sorted_inputs:
        if ent.source_type == "image_path":
            cmd.extend(["-loop", "1", "-i", ent.path])
        else:
            cmd.extend(["-i", ent.path])

    if tmp_path is None:
        cmd.extend(["-filter_complex", filter_arg_value])
    else:
        cmd.extend(["-filter_complex_script", filter_arg_value])

    cmd.extend(["-map", f"[{final_label}]", "-an",
                "-c:v", "libx264", "-crf", "20", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-shortest", output_path])

    try:
        proc = subprocess.run(cmd, check=False, capture_output=True)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        for p in drawtext_cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass
    if proc.returncode != 0:
        print("--- ffmpeg cmd ---")
        print(" ".join(cmd))
        print("--- filter_complex ---")
        print(script)
        print("--- stderr ---")
        print(proc.stderr.decode("utf-8", errors="replace"))
        raise RuntimeError("compose e2e ffmpeg failed")
    return script


def test_compose_text_overlay():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        out = os.path.join(td, "out.mp4")
        make_test_video(src)

        ir = ComposeIR(target_fps=24, target_width=320, target_height=180)
        ir.add_video_input(src, is_main=True, has_audio=False)
        ir.append_op("drawtext", {
            "text": "Test Caption",
            "x": "(w-text_w)/2", "y": "h-text_h-10",
            "fontsize": 24, "fontcolor": "white",
            "borderw": 2, "bordercolor": "black",
        })

        run_compose(ir, out)
        info = probe(out)
        assert info is not None
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert v["width"] == 320 and v["height"] == 180
        print(f"[OK] compose drawtext → {out} ({os.path.getsize(out)} bytes)")


def test_compose_two_overlays_chained():
    """兩個 drawtext 依序串接（這是 ComfyUI 自然 workflow，不是 parallel fan-out）。"""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        out = os.path.join(td, "out.mp4")
        make_test_video(src)

        ir = ComposeIR(target_fps=24, target_width=320, target_height=180)
        ir.add_video_input(src, is_main=True, has_audio=False)
        ir.append_op("drawtext", {"text": "A", "fontsize": 24, "fontcolor": "yellow"})
        ir.append_op("drawtext", {"text": "B", "fontsize": 24, "fontcolor": "cyan"})

        script = run_compose(ir, out)
        # R5 後 drawtext 改走 textfile=；不再 inline text='A'，只能驗 drawtext op 數
        assert script.count("drawtext=") == 2, (
            f"應有 2 個 drawtext op，但 count={script.count('drawtext=')}\n{script}"
        )
        assert script.count("textfile=") == 2
        # linear chain：split 不應該對 main 啟用
        assert "split=" not in script, f"linear chain 不該有 split:\n{script}"
        print("[OK] compose two-overlay sequential chain")


def test_compose_watermark_alpha_and_temporal():
    """Watermark with opacity 0.5 + temporal window [0.5, 1.5]."""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        wm = os.path.join(td, "wm.png")
        out = os.path.join(td, "out.mp4")
        make_test_video(src, dur=2)
        make_test_image(wm)

        ir = ComposeIR(target_fps=24, target_width=320, target_height=180)
        ir.add_video_input(src, is_main=True, has_audio=False)
        wm_label = ir.add_image_input(wm)
        ir.append_op("overlay", {
            "x": "10", "y": "10", "scale_w": 40,
            "alpha": 0.5,
            "start_sec": 0.5, "end_sec": 1.5,
        }, extra_input=wm_label)

        script = run_compose(ir, out)
        assert "colorchannelmixer=aa=0.5000" in script
        assert "between(t,0.5,1.5)" in script
        print("[OK] compose watermark alpha+temporal")


if __name__ == "__main__":
    test_compose_text_overlay()
    test_compose_two_overlays_chained()
    test_compose_watermark_alpha_and_temporal()
    print("\n=== Compose e2e: all 3 cases passed ===")
