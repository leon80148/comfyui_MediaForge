"""End-to-end smoke test for video_io tensor pipes.

依賴：ffmpeg + numpy + torch。會在 /tmp 建臨時檔，跑完自刪。
跑法：python tests/test_video_io_roundtrip.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import torch  # noqa: E402

from utils.ffmpeg import probe  # noqa: E402
from utils.video_io import (  # noqa: E402
    decode_audio_to_dict,
    decode_video_to_tensor,
    encode_tensor_to_video,
)


def make_test_video(path, n_frames=30, w=160, h=120, fps=30):
    """產生一段 N 幀的合成影片給 roundtrip 用。每幀填漸層顏色 + 嗡嗡音。"""
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=duration={n_frames / fps}:size={w}x{h}:rate={fps}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={n_frames / fps}",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        path,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        print(proc.stderr.decode("utf-8", errors="replace"))
        raise RuntimeError("test fixture ffmpeg gen failed")


def test_decode_video_to_tensor_shape_and_range():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        make_test_video(src, n_frames=30, w=160, h=120, fps=30)

        tensor, meta = decode_video_to_tensor(src)
        assert tensor.ndim == 4 and tensor.shape[-1] == 3, f"shape={tuple(tensor.shape)}"
        assert tensor.dtype == torch.float32, f"dtype={tensor.dtype}"
        assert 0.0 <= float(tensor.min()) <= float(tensor.max()) <= 1.0, \
            f"out of range: min={float(tensor.min())} max={float(tensor.max())}"
        assert meta["width"] == 160 and meta["height"] == 120
        assert meta["n_frames"] == 30
        assert abs(meta["src_fps"] - 30.0) < 0.5
        print(f"[OK] decode shape={tuple(tensor.shape)} dtype={tensor.dtype} meta_fps={meta['src_fps']}")


def test_decode_audio_canonical_format():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        make_test_video(src, n_frames=30, w=64, h=48, fps=30)

        audio = decode_audio_to_dict(src)
        assert audio is not None
        w = audio["waveform"]
        assert w.ndim == 3, f"AUDIO waveform 必須是 [B,C,T] 三維，但 shape={tuple(w.shape)}"
        assert w.shape[0] == 1, f"batch dim 必須是 1，但 {w.shape[0]}"
        assert w.dtype == torch.float32
        assert isinstance(audio["sample_rate"], int) and audio["sample_rate"] > 0
        assert float(w.abs().max()) <= 1.0, f"waveform 超出 [-1,1]: max={float(w.abs().max())}"
        print(f"[OK] audio canonical shape={tuple(w.shape)} sr={audio['sample_rate']}")


def test_encode_then_decode_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        make_test_video(src, n_frames=30, w=160, h=120, fps=30)
        tensor, meta = decode_video_to_tensor(src)
        audio = decode_audio_to_dict(src)

        out = os.path.join(td, "out.mp4")
        encode_tensor_to_video(
            tensor, out, fps=meta["src_fps"],
            audio=audio, codec="libx264", pix_fmt="yuv420p", crf=18, preset="ultrafast",
        )
        assert os.path.exists(out) and os.path.getsize(out) > 1000

        info = probe(out)
        assert info is not None
        v_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        a_stream = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
        assert v_stream["width"] == 160 and v_stream["height"] == 120
        assert a_stream is not None, "encoded output 缺 audio (mux 應成功)"

        # Roundtrip 比對：解碼回 tensor，比像素差距（容忍 H.264 lossy）
        tensor2, _ = decode_video_to_tensor(out)
        # 兩邊都該有 30 frames；H.264 lossy 編碼像素差 PSNR 通常 >40dB
        assert tensor2.shape[0] == tensor.shape[0]
        mse = float(((tensor - tensor2) ** 2).mean())
        psnr = float("inf") if mse == 0 else 10 * np.log10(1.0 / mse)
        print(f"[OK] roundtrip mse={mse:.6f} psnr={psnr:.2f} dB")
        assert psnr > 25.0, f"PSNR 太低（{psnr:.2f}dB），編解碼可能壞了"


def test_encode_no_audio():
    """測試 encode 不傳 audio 時純影像輸出。"""
    with tempfile.TemporaryDirectory() as td:
        tensor = torch.rand(15, 64, 64, 3)
        out = os.path.join(td, "out.mp4")
        encode_tensor_to_video(tensor, out, fps=30.0, codec="libx264", preset="ultrafast")
        assert os.path.exists(out)
        info = probe(out)
        assert info is not None
        has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
        assert not has_audio, "未傳 audio 但 output 帶 audio 軌"
        print("[OK] encode without audio")


def test_decode_no_audio_stream():
    """來源沒 audio → decode_audio_to_dict 應回 None。"""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "silent.mp4")
        import subprocess
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
            "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p",
            src,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        audio = decode_audio_to_dict(src)
        assert audio is None, f"silent video 應回 None，但拿到 {audio}"
        print("[OK] decode silent video → None")


if __name__ == "__main__":
    test_decode_video_to_tensor_shape_and_range()
    test_decode_audio_canonical_format()
    test_encode_then_decode_roundtrip()
    test_encode_no_audio()
    test_decode_no_audio_stream()
    print("\n=== video_io roundtrip: all 5 cases passed ===")
