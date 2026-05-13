"""Regression test for P1-4: LoopVideo / TrimByRanges / ConcatVideos(transcode)
must expose codec / crf / preset widgets and route them through build_encoder_args.

跑法：
    python tests/test_review_p1_codec_widgets.py
    python -m pytest tests/test_review_p1_codec_widgets.py
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


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _has_codec_crf_preset_widgets(input_types):
    """共用 schema 驗證：required 區段裡有 codec / crf / preset。"""
    req = input_types.get("required", {})
    missing = [k for k in ("codec", "crf", "preset") if k not in req]
    if missing:
        return False, f"缺 {missing}"
    # codec 必須是 dropdown (list of choices) — 形如 (choices_list, {"default": "..."})
    codec_spec = req["codec"]
    if not isinstance(codec_spec, tuple) or not isinstance(codec_spec[0], list):
        return False, f"codec 不是 dropdown：{codec_spec}"
    return True, "OK"


def test_loop_video_exposes_codec_widgets():
    from comfyui_MediaForge.nodes.loop_video import MF_LoopVideo
    ok, msg = _has_codec_crf_preset_widgets(MF_LoopVideo.INPUT_TYPES())
    assert ok, f"P1-4 regression — MF_LoopVideo: {msg}"
    print("[OK] P1-4: MF_LoopVideo 有 codec / crf / preset widgets")


def test_trim_by_ranges_exposes_codec_widgets():
    from comfyui_MediaForge.nodes.trim_by_ranges import MF_TrimByRanges
    ok, msg = _has_codec_crf_preset_widgets(MF_TrimByRanges.INPUT_TYPES())
    assert ok, f"P1-4 regression — MF_TrimByRanges: {msg}"
    print("[OK] P1-4: MF_TrimByRanges 有 codec / crf / preset widgets")


def test_concat_videos_exposes_codec_preset_widgets():
    from comfyui_MediaForge.nodes.concat_videos import MF_ConcatVideos
    types = MF_ConcatVideos.INPUT_TYPES()
    req = types.get("required", {})
    # ConcatVideos 之前就有 crf；P1-4 補 codec + preset
    assert "codec" in req, "P1-4: MF_ConcatVideos 缺 codec widget"
    assert "preset" in req, "P1-4: MF_ConcatVideos 缺 preset widget"
    assert "crf" in req, "MF_ConcatVideos crf widget 應仍存在"
    codec_spec = req["codec"]
    assert isinstance(codec_spec, tuple) and isinstance(codec_spec[0], list), (
        f"codec 不是 dropdown：{codec_spec}"
    )
    print("[OK] P1-4: MF_ConcatVideos 有 codec / crf / preset widgets")


def test_loop_video_uses_build_encoder_args_for_libx265():
    """端到端：給 LoopVideo 指定 libx265 codec → output 內 codec 必須是 hevc。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.loop_video import MF_LoopVideo
    from utils.ffmpeg import probe

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=24",
            "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", src,
        ], check=True, capture_output=True)

        node = MF_LoopVideo()
        (out,) = unpack(node.loop(
            video_path=src,
            filename_prefix="MediaForge/test_p1_loop_hevc",
            target_duration_sec=1.5, loop_mode="strict",
            crossfade_sec=1.0, speed=1.0, reverse=False, keep_audio=True,
            audio_volume=1.0,
            codec="hevc (libx265)", crf=28, preset="ultrafast",
        ))
        info = probe(out)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert v["codec_name"] in ("hevc", "h265"), (
            f"P1-4 LoopVideo libx265 失效：output codec_name={v['codec_name']!r}"
        )
        print(f"[OK] P1-4 e2e: LoopVideo + libx265 → {v['codec_name']}")


def test_trim_by_ranges_uses_build_encoder_args_for_libx265():
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.trim_by_ranges import MF_TrimByRanges
    from utils.ffmpeg import probe

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=24",
            "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", src,
        ], check=True, capture_output=True)

        node = MF_TrimByRanges()
        (out,) = unpack(node.trim(
            video_path=src,
            filename_prefix="MediaForge/test_p1_trim_hevc",
            mode="keep", ranges_json="[[0.0, 1.0]]", crossfade_sec=0.0,
            codec="hevc (libx265)", crf=28, preset="ultrafast",
        ))
        info = probe(out)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert v["codec_name"] in ("hevc", "h265"), (
            f"P1-4 TrimByRanges libx265 失效：output codec_name={v['codec_name']!r}"
        )
        print(f"[OK] P1-4 e2e: TrimByRanges + libx265 → {v['codec_name']}")


def test_concat_videos_transcode_uses_build_encoder_args_for_libx265():
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.concat_videos import MF_ConcatVideos
    from utils.ffmpeg import probe

    with tempfile.TemporaryDirectory() as td:
        c1 = os.path.join(td, "c1.mp4")
        c2 = os.path.join(td, "c2.mp4")
        for p in (c1, c2):
            subprocess.run([
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=duration=0.5:size=160x120:rate=24",
                "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", p,
            ], check=True, capture_output=True)

        node = MF_ConcatVideos()
        (out,) = unpack(node.concat(
            video_paths=f"{c1}\n{c2}",
            filename_prefix="MediaForge/test_p1_concat_hevc",
            mode="transcode", transition_sec=0.0, transition_type="fade",
            fps=24.0, width=160, height=120, crf=28,
            codec="hevc (libx265)", preset="ultrafast",
        ))
        info = probe(out)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert v["codec_name"] in ("hevc", "h265"), (
            f"P1-4 ConcatVideos libx265 失效：output codec_name={v['codec_name']!r}"
        )
        print(f"[OK] P1-4 e2e: ConcatVideos transcode + libx265 → {v['codec_name']}")


if __name__ == "__main__":
    test_loop_video_exposes_codec_widgets()
    test_trim_by_ranges_exposes_codec_widgets()
    test_concat_videos_exposes_codec_preset_widgets()
    test_loop_video_uses_build_encoder_args_for_libx265()
    test_trim_by_ranges_uses_build_encoder_args_for_libx265()
    test_concat_videos_transcode_uses_build_encoder_args_for_libx265()
    print("\n=== P1-4 codec/crf/preset widgets: all cases passed ===")
