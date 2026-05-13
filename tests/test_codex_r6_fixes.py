"""Regression tests for Codex Round 6 review findings.

跑法：python tests/test_codex_r6_fixes.py
"""
import os
import sys

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


def test_concat_transition_clamped_to_shortest_clip():
    """[R6 P2] transition_sec > shortest clip → 必須 clamp，不該 emit invalid xfade。"""
    # 直接驗 filter graph 構造 — 不真 ffmpeg run（會慢）
    # 用 mock: 替 _probe 跟 probe_video_duration 注入 fake 答案
    import comfyui_MediaForge.nodes.concat_videos as cv

    # Monkey-patch ffmpeg probe to avoid hitting disk
    saved = []
    try:
        # 先側錄 _concat_transcode 行為：直接呼叫並攔截 ffmpeg call
        captured = {}

        def fake_run_ffmpeg(cmd, tag="FFmpeg"):
            captured["cmd"] = cmd
            return True  # 假裝成功
        cv.run_ffmpeg = fake_run_ffmpeg

        # Mock probe / probe_video_duration import path 內部使用
        import comfyui_MediaForge.utils.ffmpeg as ff
        orig_pv = ff.probe_video_duration
        orig_probe_fn = ff.probe
        ff.probe_video_duration = lambda p: 0.5  # 每段都是 0.5s
        ff.probe = lambda p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
        saved = [("probe_video_duration", orig_pv), ("probe", orig_probe_fn)]

        # 兩段 0.5s clips + 1s transition (應 clamp 到 0.495s)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            c1 = os.path.join(td, "c1.mp4")
            c2 = os.path.join(td, "c2.mp4")
            for p in (c1, c2):
                open(p, "wb").close()  # 騙過 exists check

            node = cv.MF_ConcatVideos()
            node.concat(
                video_paths=f"{c1}\n{c2}",
                filename_prefix="MediaForge/test_r6_concat_clamp",
                mode="transcode", transition_sec=1.0, transition_type="fade",
                fps=24.0, width=320, height=180, crf=23,
            )
            cmd = captured["cmd"]
            fc = cmd[cmd.index("-filter_complex") + 1]
            import re
            m = re.search(r"xfade=transition=fade:duration=([\d.]+)", fc)
            assert m, f"xfade duration 沒找到\n{fc}"
            dur = float(m.group(1))
            # 應 clamp 到 0.5 * 0.99 = 0.495
            assert dur < 0.5, (
                f"transition duration={dur} 沒 clamp 到 < shortest clip (0.5s)\n{fc}"
            )
            print(f"[OK] R6 P2: concat transition clamped from 1.0s → {dur:.4f}s")
    finally:
        for name, val in saved:
            setattr(ff, name, val)


def test_compose_ir_clone_deep_copies_params():
    """[R6 P2] clone 後對新 IR 的 op.params 修改不能反污染源 IR。"""
    from utils.compose_ir import ComposeIR, compile_ir

    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True)
    ir.append_op("drawtext", {"text": "Original"})

    ir2 = ir.clone()
    # 修改 clone 的 params
    ir2.ops[0].params["text"] = "Modified"
    ir2.ops[0].params["new_key"] = "extra"

    assert ir.ops[0].params["text"] == "Original", (
        f"clone 後修改回流到源 IR: {ir.ops[0].params}"
    )
    assert "new_key" not in ir.ops[0].params, (
        f"clone 後 new key 滲透到源 IR: {ir.ops[0].params}"
    )

    # 更重要：跑 compile_ir 不能寫 _textfile_path 到源 IR
    _, _, cleanup = compile_ir(ir2)
    try:
        assert "_textfile_path" not in ir.ops[0].params, (
            f"compile 後源 IR.params 含 _textfile_path: {ir.ops[0].params}"
        )
        assert "_textfile_path" in ir2.ops[0].params  # clone 本身有
        print("[OK] R6 P2: ComposeIR.clone deep-copies op.params (compile mutation isolated)")
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass


def test_compose_ir_double_compile_no_collision():
    """進一步驗：對同一 source IR clone 兩次 → 各自 compile → 兩份 _textfile_path 不同檔。"""
    from utils.compose_ir import ComposeIR, compile_ir

    src = ComposeIR()
    src.add_video_input("/tmp/main.mp4", is_main=True)
    src.append_op("drawtext", {"text": "shared base text"})

    ir_a = src.clone()
    ir_b = src.clone()
    _, _, cleanup_a = compile_ir(ir_a)
    _, _, cleanup_b = compile_ir(ir_b)
    try:
        path_a = ir_a.ops[0].params.get("_textfile_path")
        path_b = ir_b.ops[0].params.get("_textfile_path")
        assert path_a and path_b and path_a != path_b, (
            f"兩次 compile 共用同一 tmp file: {path_a} vs {path_b}"
        )
        assert "_textfile_path" not in src.ops[0].params
        print(f"[OK] R6 P2: 兩個 clone 各自獨立 textfile ({os.path.basename(path_a)} vs {os.path.basename(path_b)})")
    finally:
        for p in cleanup_a + cleanup_b:
            try:
                os.unlink(p)
            except OSError:
                pass


if __name__ == "__main__":
    test_concat_transition_clamped_to_shortest_clip()
    test_compose_ir_clone_deep_copies_params()
    test_compose_ir_double_compile_no_collision()
    print("\n=== Codex R6 fixes: all 3 cases passed ===")
