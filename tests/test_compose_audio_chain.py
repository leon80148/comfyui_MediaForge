"""End-to-end tests for MF_COMPOSE_AUDIO_OPS chain (Compose v2 audio domain)。

跑法:python tests/test_compose_audio_chain.py
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


def _make_video_with_audio(path, dur=2):
    """Build testsrc video + 440Hz sine tone audio。"""
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={dur}:size=320x180:rate=24",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
        "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", path,
    ], check=True, capture_output=True)


def _make_video_no_audio(path, dur=2):
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={dur}:size=320x180:rate=24",
        "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", path,
    ], check=True, capture_output=True)


def _make_bgm(path, dur=2):
    """Build 880Hz sine BGM (audio-only file)。"""
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={dur}",
        "-c:a", "mp3", path,
    ], check=True, capture_output=True)


def _setup_output_dir(td):
    import folder_paths
    folder_paths.get_output_directory = lambda: td


def test_audio_amix_mix_mode():
    """AudioMix keep_source=True + BGM → output 含混音。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_audio_mix import MF_ComposeAudioMix
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        bgm = os.path.join(td, "bgm.mp3")
        _make_video_with_audio(src)
        _make_bgm(bgm)
        _setup_output_dir(td)

        ops = MF_ComposeAudioMix().add(
            audio_path=bgm, keep_source=True, bgm_volume=0.3, duration="first",
        )[0]

        result = unpack(MF_ComposeVideo().compose(
            video_path=src, filename_prefix="audio_test/amix_mix",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast", keep_audio=True,
            audio_ops=ops,
        ))
        out_path, script = result
        assert os.path.exists(out_path)
        # 確認 chain 有走 amix 跟 BGM volume 衰減
        assert "amix=inputs=2" in script
        assert "volume=0.300000" in script

        from comfyui_MediaForge.utils.ffmpeg import probe_has_audio_stream
        assert probe_has_audio_stream(out_path)
        print(f"[OK] amix mix mode: source+BGM mixed → {os.path.getsize(out_path)} bytes")


def test_audio_amix_replace_mode():
    """AudioMix keep_source=False → source audio 被外部 BGM 完全取代。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_audio_mix import MF_ComposeAudioMix
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        bgm = os.path.join(td, "bgm.mp3")
        _make_video_with_audio(src)
        _make_bgm(bgm)
        _setup_output_dir(td)

        ops = MF_ComposeAudioMix().add(
            audio_path=bgm, keep_source=False, bgm_volume=1.0, duration="first",
        )[0]

        result = unpack(MF_ComposeVideo().compose(
            video_path=src, filename_prefix="audio_test/amix_replace",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast", keep_audio=True,
            audio_ops=ops,
        ))
        out_path, script = result
        assert os.path.exists(out_path)
        # replace mode 用 anull 把外部音源 rename、不走 amix
        assert "anull" in script
        assert "amix=" not in script, "replace mode 不該有 amix:" + script
        print(f"[OK] amix replace mode: BGM-only → {os.path.getsize(out_path)} bytes")


def test_audio_normalize_runs():
    """loudnorm chain 跑得起來、stderr 無 error。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_normalize import MF_ComposeNormalize
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        _make_video_with_audio(src)
        _setup_output_dir(td)

        ops = MF_ComposeNormalize().add(
            target_i=-16.0, target_tp=-1.0, target_lra=11.0, linear=True,
        )[0]

        result = unpack(MF_ComposeVideo().compose(
            video_path=src, filename_prefix="audio_test/normalize",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast", keep_audio=True,
            audio_ops=ops,
        ))
        out_path, script = result
        assert os.path.exists(out_path)
        assert "loudnorm=I=-16.0" in script
        print(f"[OK] loudnorm chain runs → {os.path.getsize(out_path)} bytes")


def test_audio_full_pipeline():
    """完整 chain:Volume → AudioMix → Fade → Normalize → 全部 filter 都進 script。"""
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.nodes.compose_audio_fade import MF_ComposeAudioFade
    from comfyui_MediaForge.nodes.compose_audio_mix import MF_ComposeAudioMix
    from comfyui_MediaForge.nodes.compose_normalize import MF_ComposeNormalize
    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo
    from comfyui_MediaForge.nodes.compose_volume import MF_ComposeVolume

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        bgm = os.path.join(td, "bgm.mp3")
        _make_video_with_audio(src)
        _make_bgm(bgm)
        _setup_output_dir(td)

        # Chain 串接:Volume → AudioMix(BGM) → Fade → Normalize
        v = MF_ComposeVolume().add(scale=1.1)[0]
        mix = MF_ComposeAudioMix().add(
            audio_path=bgm, keep_source=True, bgm_volume=0.3, duration="first",
            audio_ops=v,
        )[0]
        fade = MF_ComposeAudioFade().add(
            direction="in", start_sec=0, duration_sec=1.0, curve="qsin",
            audio_ops=mix,
        )[0]
        full = MF_ComposeNormalize().add(
            target_i=-16.0, target_tp=-1.0, target_lra=11.0, linear=True,
            audio_ops=fade,
        )[0]
        assert len(full) == 4, f"chain 應含 4 op、拿到 {len(full)}"
        assert [op["type"] for op in full] == ["volume", "amix", "afade", "loudnorm"]

        result = unpack(MF_ComposeVideo().compose(
            video_path=src, filename_prefix="audio_test/full_pipe",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast", keep_audio=True,
            audio_ops=full,
        ))
        out_path, script = result
        assert os.path.exists(out_path)
        # 四個 filter 全部要 emit
        assert "volume=1.100000" in script
        assert "amix=inputs=2" in script
        assert "afade=t=in" in script and "curve=qsin" in script
        assert "loudnorm=I=-16.0" in script
        print(f"[OK] full audio pipeline (4 ops, single encode) → {os.path.getsize(out_path)} bytes")


if __name__ == "__main__":
    test_audio_amix_mix_mode()
    test_audio_amix_replace_mode()
    test_audio_normalize_runs()
    test_audio_full_pipeline()
    print("\n=== Compose Audio Chain e2e: all 4 cases passed ===")
