"""Compose IR Prerequisite Spike — ROADMAP Phase 4 BLOCKER 的 acceptance test cases.

跑法：python -m pytest tests/test_compose_ir.py -v
獨立執行 (不裝 pytest)：python tests/test_compose_ir.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.compose_ir import ComposeIR, compile_audio_chain, compile_ir  # noqa: E402


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


def test_case_8_audio_chain_empty_returns_passthrough():
    """audio_ops 空 → compile_audio_chain 回 (None, main_audio_label)。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True, has_audio=True)
    script, final_a = compile_audio_chain(ir)
    assert script is None, f"空 audio_ops 應回 None,拿到 {script!r}"
    assert final_a == "0:a", f"應沿用 main_audio_label,拿到 {final_a!r}"
    print("[OK] Case 8: empty audio chain passthrough 通過")


def test_case_9_audio_chain_volume_only():
    """單一 volume op → [0:a]volume=...[a1]。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True, has_audio=True)
    ir.append_audio_op("volume", {"scale": 0.5})
    script, final_a = compile_audio_chain(ir)
    assert "volume=0.500000" in script, f"volume filter 沒生成:\n{script}"
    assert final_a == ir.audio_ops[-1].label
    print(f"[OK] Case 9: volume audio chain ({script}) 通過")


def test_case_10_audio_chain_amix_mix_mode():
    """amix keep_source=True → [0:a][bgm]amix=inputs=2:duration=first[aN]。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True, has_audio=True)
    bgm_label = ir.add_audio_input("/tmp/bgm.mp3")
    ir.append_audio_op("amix",
                       {"keep_source": True, "duration": "first", "bgm_volume": 1.0},
                       extra_audio_input=bgm_label)
    script, final_a = compile_audio_chain(ir)
    assert "amix=inputs=2:duration=first" in script, f"amix filter 沒生成:\n{script}"
    assert f"[{bgm_label}]" in script, f"bgm label {bgm_label} 應出現在 filter:\n{script}"
    print("[OK] Case 10: amix mix mode (source + BGM) 通過")


def test_case_11_audio_chain_amix_replace_mode():
    """amix keep_source=False → [bgm]anull[aN]、純粹用外部音源。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True, has_audio=True)
    bgm_label = ir.add_audio_input("/tmp/bgm.mp3")
    # keep_source=False: depends_on 直接指向 bgm、extra_audio_input=None
    ir.append_audio_op("amix",
                       {"keep_source": False},
                       depends_on=bgm_label)
    script, _final_a = compile_audio_chain(ir)
    assert f"[{bgm_label}]anull" in script, f"replace mode 應走 anull,拿到:\n{script}"
    print("[OK] Case 11: amix replace mode (BGM only) 通過")


def test_case_12_audio_chain_bgm_volume_adjust():
    """amix keep_source=True with bgm_volume=0.3 → volume filter 在 amix 之前。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True, has_audio=True)
    bgm_label = ir.add_audio_input("/tmp/bgm.mp3")
    ir.append_audio_op("amix",
                       {"keep_source": True, "bgm_volume": 0.3, "duration": "first"},
                       extra_audio_input=bgm_label)
    script, _ = compile_audio_chain(ir)
    assert "volume=0.300000" in script, f"BGM volume 0.3 沒生效:\n{script}"
    assert "amix=" in script, "amix 應 在 volume 之後:\n" + script
    # volume 出現位置 應該在 amix 之前
    assert script.index("volume=") < script.index("amix="), (
        f"volume filter 應在 amix 之前:\n{script}"
    )
    print("[OK] Case 12: BGM volume + amix 通過")


def test_case_13_audio_chain_full_pipeline():
    """Volume → AudioMix(BGM, keep_source) → Fade → Normalize 完整 chain。"""
    ir = ComposeIR()
    ir.add_video_input("/tmp/main.mp4", is_main=True, has_audio=True)
    bgm_label = ir.add_audio_input("/tmp/bgm.mp3")
    ir.append_audio_op("volume", {"scale": 1.2})  # boost source
    ir.append_audio_op("amix",
                       {"keep_source": True, "bgm_volume": 0.4, "duration": "first"},
                       extra_audio_input=bgm_label)
    ir.append_audio_op("afade", {"direction": "in", "duration_sec": 1.5, "curve": "qsin"})
    ir.append_audio_op("loudnorm", {"target_i": -16.0})
    script, final_a = compile_audio_chain(ir)
    # 4 個 op 各自的 filter 都該出現
    assert "volume=1.200000" in script, "source volume boost 沒生效"
    assert "amix=inputs=2" in script, "amix 沒生效"
    assert "afade=t=in" in script, "afade 沒生效"
    assert "loudnorm=I=-16.0" in script, "loudnorm 沒生效"
    assert final_a == ir.audio_ops[-1].label
    print(f"[OK] Case 13: full audio pipeline 4 ops 通過 (final={final_a})")


def test_case_14_subtitle_op_dispatch():
    """新加的 subtitle op kind → emit subtitles=...:fontsdir=...:force_style filter。"""
    import tempfile
    # 假 SRT (filter 內 escape path 即可、不需要真實檔)
    with tempfile.NamedTemporaryFile(suffix=".srt", delete=False, mode="w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        srt_path = f.name

    try:
        ir = ComposeIR()
        ir.add_video_input("/tmp/main.mp4", is_main=True)
        ir.append_op("subtitle", {
            "srt_path": srt_path,
            "style_string": "Fontname=Arial,Fontsize=24",
        })
        script, final_v, cleanup = compile_ir(ir)
        try:
            assert "subtitles=" in script, f"subtitles filter 沒生成:\n{script}"
            assert "fontsdir=" in script, f"fontsdir 沒生成:\n{script}"
            assert "force_style=" in script, f"force_style 沒生成:\n{script}"
            assert "Fontname=Arial" in script, "style_string 沒嵌進 force_style"
            print("[OK] Case 14: subtitle op dispatch 通過")
        finally:
            for p in cleanup:
                try:
                    os.unlink(p)
                except OSError:
                    pass
    finally:
        os.unlink(srt_path)


if __name__ == "__main__":
    test_case_1_two_overlays_linear_chain()
    test_case_1b_parallel_depends_on_rejected()
    test_case_2_temporal_overlay()
    test_case_3_normalization_pass()
    test_case_4_label_uniqueness()
    test_case_5_overlay_with_watermark_image()
    test_case_6_watermark_alpha_and_tile()
    test_case_7_ir_clone_isolation()
    test_case_8_audio_chain_empty_returns_passthrough()
    test_case_9_audio_chain_volume_only()
    test_case_10_audio_chain_amix_mix_mode()
    test_case_11_audio_chain_amix_replace_mode()
    test_case_12_audio_chain_bgm_volume_adjust()
    test_case_13_audio_chain_full_pipeline()
    test_case_14_subtitle_op_dispatch()
    print("\n=== Compose IR Spike: all 15 cases passed ===")
