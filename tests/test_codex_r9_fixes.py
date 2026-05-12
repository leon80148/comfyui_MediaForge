"""Regression tests for Codex Round 9 review findings.

跑法：python tests/test_codex_r9_fixes.py
"""
import os
import subprocess
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
if _CUSTOM_NODES not in sys.path:
    sys.path.insert(0, _CUSTOM_NODES)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def test_decode_size_guard_uses_video_duration_not_container():
    """[R9 P2] short video + long audio 不該被 MAX_DECODE_BYTES 誤殺。"""
    from utils.video_io import decode_video_to_tensor

    with tempfile.TemporaryDirectory() as td:
        # 1s 4K video + 60s audio (container ~60s, video ~1s)
        # 4K 60s 估算 = 60*30*3840*2160*3 ≈ 45 GB → 會撞 MAX_DECODE_BYTES (4GB)
        # 但實際 video 是 1s = 0.75 GB → 應該通過
        src = os.path.join(td, "mixed.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=3840x2160:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
             "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k", src],
            check=True, capture_output=True, timeout=120,
        )

        # 用 video_duration 估算 → ~0.75 GB → 應該不撞 MAX_DECODE_BYTES
        try:
            tensor, meta = decode_video_to_tensor(src)
            assert tensor.shape[0] == 30, f"expected 30 frames @ 1s/30fps, got {tensor.shape[0]}"
            print(
                f"[OK] R9 P2: 1s video + 60s audio → decode OK ({tensor.shape[0]} frames, "
                f"video_dur={meta['src_fps']:.1f} fps)"
            )
        except RuntimeError as e:
            raise AssertionError(
                f"短 video + 長 audio 被誤殺: {e}"
            ) from e


def test_decode_handles_portrait_rotation_metadata_via_mock():
    """[R9 P2] portrait 影片 coded 1920x1080 + rotation 90 → decode 應 reshape 成 1080x1920。

    用 mock probe (modern ffmpeg 無法 inject rotation metadata 到 mp4 stream)。"""
    import utils.video_io as vio

    # Mock probe to return coded 1920x1080 + side_data rotation=90
    captured_dims = {}
    real_run = subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        # 攔截 ffmpeg rawvideo 輸出，返回剛好 1080*1920*3 bytes (display=1080x1920)
        if "rawvideo" in cmd:
            n_frames = 5
            data = bytes(n_frames * 1080 * 1920 * 3)
            class R:
                returncode = 0
                stdout = data
                stderr = b""
            return R()
        return real_run(cmd, **kwargs)

    saved_probe = vio.probe
    saved_pvd = vio.probe_video_duration
    saved_run = subprocess.run
    vio.probe = lambda p: {
        "streams": [{
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920, "height": 1080,
            "avg_frame_rate": "30/1",
            "r_frame_rate": "30/1",
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}],
        }],
        "format": {"duration": "0.16"},
    }
    vio.probe_video_duration = lambda p: 0.16
    subprocess.run = fake_subprocess_run

    try:
        tensor, meta = vio.decode_video_to_tensor("/tmp/fake_portrait.mp4")
        b, h, w, c = tensor.shape
        captured_dims["shape"] = (b, h, w, c)
        assert (h, w) == (1920, 1080), (
            f"rotation=90 應 swap 為 1080x1920 (h, w)=(1920, 1080)，但拿到 ({h}, {w})"
        )
        assert c == 3
        print(f"[OK] R9 P2: portrait video w/rotation 90 → tensor h={h}, w={w} (display 對齊)")
    finally:
        vio.probe = saved_probe
        vio.probe_video_duration = saved_pvd
        subprocess.run = saved_run


def test_decode_normal_landscape_unaffected():
    """補：無 rotation 的 landscape 影片 H,W 仍按 coded dims 解。"""
    from utils.video_io import decode_video_to_tensor

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "land.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=0.5:size=640x360:rate=10",
             "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", src],
            check=True, capture_output=True,
        )
        tensor, _ = decode_video_to_tensor(src)
        _, h, w, _ = tensor.shape
        assert (h, w) == (360, 640), f"landscape shape 異常: {(h, w)}"
        print(f"[OK] R9 P2 補: landscape video 維持 {h}x{w}")


if __name__ == "__main__":
    test_decode_size_guard_uses_video_duration_not_container()
    test_decode_handles_portrait_rotation_metadata_via_mock()
    test_decode_normal_landscape_unaffected()
    print("\n=== Codex R9 fixes: all 3 cases passed ===")
