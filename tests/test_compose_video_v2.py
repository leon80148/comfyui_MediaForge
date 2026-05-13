"""End-to-end tests for MF_ComposeVideo (Compose pipeline v2).

跑法:python tests/test_compose_video_v2.py
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
from conftest import unpack  # noqa: E402


def _has_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _make_test_video(path, dur=2, w=320, h=180, fps=24, has_audio=True):
    """Build a quick testsrc clip (optional silent audio for audio-chain tests)。"""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=duration={dur}:size={w}x{h}:rate={fps}"]
    if has_audio:
        cmd.extend(["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"])
    cmd.extend(["-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p"])
    if has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "128k"])
    cmd.append(path)
    subprocess.run(cmd, check=True, capture_output=True)


def _make_test_image(path, w=80, h=80):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=red:size={w}x{h}:rate=1",
         "-frames:v", "1", path],
        check=True, capture_output=True,
    )


def _setup_output_dir(td):
    """ComposeVideo writes to folder_paths.get_output_directory() — monkey-patch to td。"""
    import folder_paths
    folder_paths.get_output_directory = lambda: td


def test_compose_video_minimal_passthrough():
    """無 overlay / 無 audio_ops → 純粹 transcode source。target_*=0 對齊 source dims。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        _make_test_video(src, dur=1, w=320, h=180, fps=24, has_audio=False)
        _setup_output_dir(td)

        result = unpack(MF_ComposeVideo().compose(
            video_path=src,
            filename_prefix="test_v2/minimal",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast",
            keep_audio=True,
        ))
        out_path = result[0]
        assert os.path.exists(out_path), f"輸出檔不存在:{out_path}"

        # 用 ffprobe 確認 dims = source dims (0-sentinel 生效)
        from comfyui_MediaForge.utils.ffmpeg import probe
        info = probe(out_path)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (v["width"], v["height"]) == (320, 180), f"target=0 應沿用 source dims、拿到 {v['width']}x{v['height']}"
        print(f"[OK] minimal passthrough → {out_path} ({os.path.getsize(out_path)} bytes)")


def test_compose_video_with_watermark():
    """Watermark chain → ComposeVideo,輸出應該包含 overlay。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo
    from comfyui_MediaForge.nodes.compose_watermark import MF_ComposeWatermark

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        wm = os.path.join(td, "wm.png")
        _make_test_video(src, dur=1, has_audio=False)
        _make_test_image(wm)
        _setup_output_dir(td)

        # Build overlay chain
        overlays = MF_ComposeWatermark().add(
            image_path=wm, placement="bottom_right", relative_scale=0.15,
            opacity=0.7, margin_top=10, margin_right=10, margin_bottom=10, margin_left=10,
            visible_start_sec=0.0, visible_end_sec=0.0,
        )[0]

        result = unpack(MF_ComposeVideo().compose(
            video_path=src,
            filename_prefix="test_v2/watermark",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast",
            keep_audio=False,
            overlays=overlays,
        ))
        out_path, script = result
        assert os.path.exists(out_path)
        # Script 應含 colorchannelmixer (opacity 0.7) + overlay
        assert "colorchannelmixer=aa=0.7000" in script
        assert "overlay=" in script
        print("[OK] compose with watermark passes")


def test_compose_video_subtitle_plus_watermark_single_encode():
    """字幕 + 浮水印同 chain → 單次 encode 完成兩個 effect (核心賣點驗證)。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_burn_subtitle import MF_ComposeBurnSubtitle
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo
    from comfyui_MediaForge.nodes.compose_watermark import MF_ComposeWatermark

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        wm = os.path.join(td, "wm.png")
        srt = os.path.join(td, "sub.srt")
        _make_test_video(src, dur=1, has_audio=False)
        _make_test_image(wm)
        with open(srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
        _setup_output_dir(td)

        # Chain: Watermark → BurnSubtitle
        wm_ops = MF_ComposeWatermark().add(
            image_path=wm, placement="bottom_right", relative_scale=0.15,
            opacity=0.7, margin_top=10, margin_right=10, margin_bottom=10, margin_left=10,
            visible_start_sec=0.0, visible_end_sec=0.0,
        )[0]
        full_ops = MF_ComposeBurnSubtitle().add(
            srt_path=srt,
            font="msjh.ttc", font_size=24,
            font_color_hex="#FFFFFF", bold=True, italic=False, letter_spacing=0.0,
            outline_color_hex="#000000", outline_width=2, shadow_depth=1, border_style=1,
            back_color_hex="#000000",
            alignment="bottom_center (2)", margin_v=20, margin_l=50, margin_r=50,
            overlays=wm_ops,
        )[0]
        # 確認 chain 長度
        assert len(full_ops) == 2, f"chain 應含 2 op、拿到 {len(full_ops)}"
        types = [op["type"] for op in full_ops]
        assert types == ["watermark", "subtitle"], f"順序錯:{types}"

        result = unpack(MF_ComposeVideo().compose(
            video_path=src,
            filename_prefix="test_v2/subtitle_watermark",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast",
            keep_audio=False,
            overlays=full_ops,
        ))
        out_path, script = result
        assert os.path.exists(out_path)
        # 單次 encode 證據:script 同時含 subtitles= 跟 overlay=
        assert "subtitles=" in script, "subtitle filter 缺、可能沒進 chain"
        assert "overlay=" in script, "watermark overlay filter 缺"
        print(f"[OK] subtitle + watermark single encode → {os.path.getsize(out_path)} bytes")


def test_compose_video_with_audio_chain():
    """Volume + AudioFade chain → ComposeVideo。確認音訊 chain 有跑、輸出含 audio。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_audio_fade import MF_ComposeAudioFade
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo
    from comfyui_MediaForge.nodes.compose_volume import MF_ComposeVolume

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        _make_test_video(src, dur=2, has_audio=True)  # source 自帶 audio
        _setup_output_dir(td)

        # audio chain:Volume(0.8) → Fade-in
        v_ops = MF_ComposeVolume().add(scale=0.8)[0]
        full_ops = MF_ComposeAudioFade().add(
            direction="in", start_sec=0, duration_sec=1.0, curve="tri",
            audio_ops=v_ops,
        )[0]

        result = unpack(MF_ComposeVideo().compose(
            video_path=src,
            filename_prefix="test_v2/audio_chain",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast",
            keep_audio=True,
            audio_ops=full_ops,
        ))
        out_path, script = result
        assert os.path.exists(out_path)
        assert "volume=0.800000" in script
        assert "afade=t=in" in script

        from comfyui_MediaForge.utils.ffmpeg import probe_has_audio_stream
        assert probe_has_audio_stream(out_path), "output 應含 audio"
        print("[OK] audio chain (Volume + Fade) → output has audio")


def test_compose_video_emits_ui_metadata():
    """ComposeVideo return shape:{ui: {images: [...]}, result: (path, script)}。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        _make_test_video(src, dur=1, has_audio=False)
        _setup_output_dir(td)

        raw = MF_ComposeVideo().compose(
            video_path=src, filename_prefix="test_v2/ui_meta",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast", keep_audio=False,
        )
        assert isinstance(raw, dict), f"應 emit dict UI 結構、拿到 {type(raw)}"
        assert "ui" in raw and "images" in raw["ui"], f"缺 ui.images:{raw}"
        assert "result" in raw and len(raw["result"]) == 2, f"result 應是 (path, script) 兩 tuple:{raw['result']}"
        ui_entries = raw["ui"]["images"]
        assert len(ui_entries) == 1
        entry = ui_entries[0]
        assert entry["type"] == "output"
        assert entry["filename"].endswith(".mp4")
        print(f"[OK] UI metadata: {entry}")


if __name__ == "__main__":
    test_compose_video_minimal_passthrough()
    test_compose_video_with_watermark()
    test_compose_video_subtitle_plus_watermark_single_encode()
    test_compose_video_with_audio_chain()
    test_compose_video_emits_ui_metadata()
    print("\n=== Compose Video v2 e2e: all 5 cases passed ===")
