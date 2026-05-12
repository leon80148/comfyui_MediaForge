"""Compose IR Prerequisite Spike — ROADMAP Phase 4 BLOCKER 的 acceptance test cases.

跑法：python -m pytest tests/test_compose_ir.py -v
獨立執行 (不裝 pytest)：python tests/test_compose_ir.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.compose_ir import ComposeIR, compile_ir  # noqa: E402


def test_case_1_two_overlays_linear_chain():
    """Spike 要求 (a)：兩個 overlay 在同個 stream — 改解讀為 sequential chain
    (A → B-on-top-of-A)，這才是 Compose IR 實際語意；parallel fan-out 會產出
    dangling output，被 _validate_linear_chain 擋下。"""
    ir = ComposeIR(target_fps=30, target_width=1280, target_height=720)
    ir.add_video_input("/tmp/main.mp4", is_main=True, has_audio=True)

    a_out = ir.append_op("drawtext", {"text": "Hello", "start_sec": 0, "end_sec": 5})
    b_out = ir.append_op("drawtext", {"text": "World", "start_sec": 6, "end_sec": 10})

    script, final, cleanup = compile_ir(ir)
    try:
        # R5 後 drawtext 改走 textfile=，text 本體在 tmp file，不在 filter graph 字串
        assert "drawtext=" in script and "textfile=" in script, (
            f"drawtext op 應 emit drawtext + textfile=:\n{script}"
        )
        # 驗 textfile 內容
        texts = []
        for p in cleanup:
            with open(p, encoding="utf-8") as f:
                texts.append(f.read())
        assert "Hello" in texts and "World" in texts, f"textfiles 沒拿到對應 text: {texts}"

        assert final == b_out, f"final 應該是最後一個 op {b_out}，但拿到 {final}"
        # linear chain：split 不應該對 depends_on 啟用
        assert "split=" not in script, f"linear chain 不該插 split:\n{script}"
        print(f"[OK] Case 1: 兩個 overlay sequential chain ({a_out} → {b_out}) 通過")
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass


def test_case_1b_parallel_depends_on_rejected():
    """補充：若使用者誤建 parallel fan-out (兩個 op 都 depends_on=main)，
    必須在 compile 時 raise，而不是產出 dangling output。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True)
    ir.append_op("drawtext", {"text": "A"})
    ir.append_op("drawtext", {"text": "B"}, depends_on=ir.main_label)
    try:
        compile_ir(ir)
    except RuntimeError as e:
        assert "linear chain" in str(e)
        print("[OK] Case 1b: parallel depends_on 正確被 reject")
        return
    raise AssertionError("compile_ir 對 parallel fan-out 應 raise 但沒 raise")


def test_case_2_temporal_overlay():
    """Spike 要求 (b)：overlay 在時間 5–10s — 驗證 enable='between(t,...)' 正確生成。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True)
    ir.append_op("drawtext", {"text": "Timed", "start_sec": 5.0, "end_sec": 10.0})
    script, _, cleanup = compile_ir(ir)
    try:
        assert "between(t,5.0,10.0)" in script, f"未含 temporal enable 表達式:\n{script}"
        print("[OK] Case 2: temporal overlay enable expression 通過")
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass


def test_case_3_normalization_pass():
    """Spike 要求 (c) 的代理：normalization pass (setpts / fps / format) 自動插入。

    原始 spec 是 crossfade transition；此 IR scope 不直接支援 crossfade
    （xfade 在 Concat / Loop 節點 layer 處理，Compose 走 stack overlay 路線）。
    驗證 normalization 是相同設計目標——保證後續 op 不必各自重複煩 setpts/fps/format。
    """
    ir = ComposeIR(target_fps=24, target_width=640, target_height=360)
    ir.add_video_input("/tmp/main.mp4", is_main=True)
    ir.append_op("drawtext", {"text": "X"})
    script, _, cleanup = compile_ir(ir)
    try:
        assert "setpts=PTS-STARTPTS" in script
        assert "fps=24" in script
        assert "format=yuv420p" in script
        assert "scale=640:360" in script
        print("[OK] Case 3: normalization pass (setpts / fps / format / scale) 通過")
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass


def test_case_4_label_uniqueness():
    """額外：label allocator 對複雜 chain 仍唯一。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True)
    labels = [ir.append_op("drawtext", {"text": f"L{i}"}) for i in range(10)]
    assert len(set(labels)) == 10, f"label collision: {labels}"
    script, _, cleanup = compile_ir(ir)
    try:
        for lab in labels:
            assert f"[{lab}]" in script, f"label {lab} 沒出現在 script"
        print("[OK] Case 4: label uniqueness 通過")
    finally:
        for p in cleanup:
            try:
                os.unlink(p)
            except OSError:
                pass


def test_case_5_overlay_with_watermark_image():
    """額外：overlay op with extra_input（watermark image）→ format=rgba + 正確 chain。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True)
    wm_label = ir.add_image_input("/tmp/wm.png")
    ir.append_op(
        "overlay",
        {"x": "10", "y": "10", "scale_w": 100, "start_sec": 0, "end_sec": 3},
        extra_input=wm_label,
    )
    script, _, _cleanup = compile_ir(ir)
    assert "format=rgba" in script
    assert "overlay=" in script
    assert "between(t,0.0,3.0)" in script
    print("[OK] Case 5: overlay with watermark image 通過")


def test_case_6_watermark_alpha_and_tile():
    """Watermark preset 用 alpha + tile 兩個 IR feature。"""
    ir = ComposeIR(target_width=1920, target_height=1080)
    ir.add_video_input("/tmp/main.mp4", is_main=True)
    wm_label = ir.add_image_input("/tmp/wm.png")
    ir.append_op(
        "overlay",
        {"x": "0", "y": "0", "scale_w": 200, "alpha": 0.5, "tile": "10x6"},
        extra_input=wm_label,
    )
    script, _, _cleanup = compile_ir(ir)
    assert "colorchannelmixer=aa=0.5000" in script
    assert "tile=10x6" in script
    print("[OK] Case 6: watermark alpha + tile 通過")


def test_case_7_ir_clone_isolation():
    """clone() 後 mutate 不影響原物件 — Compose 節點吃 IR/吐 IR 的關鍵假設。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True)
    ir.append_op("drawtext", {"text": "Original"})
    snapshot_len = len(ir.ops)

    ir2 = ir.clone()
    ir2.append_op("drawtext", {"text": "Cloned"})

    assert len(ir.ops) == snapshot_len, "原 IR 被誤動 mutate"
    assert len(ir2.ops) == snapshot_len + 1
    assert ir2.ops[-1].params["text"] == "Cloned"
    print("[OK] Case 7: IR clone isolation 通過")


if __name__ == "__main__":
    test_case_1_two_overlays_linear_chain()
    test_case_1b_parallel_depends_on_rejected()
    test_case_2_temporal_overlay()
    test_case_3_normalization_pass()
    test_case_4_label_uniqueness()
    test_case_5_overlay_with_watermark_image()
    test_case_6_watermark_alpha_and_tile()
    test_case_7_ir_clone_isolation()
    print("\n=== Compose IR Spike: all 8 cases passed ===")
