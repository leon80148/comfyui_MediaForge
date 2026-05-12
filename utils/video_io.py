"""Tensor ↔ FFmpeg pipe helpers — MediaForge's bridge between file world and ComfyUI tensor world.

Hard contracts (per ROADMAP v2.1):
- IMAGE: torch.Tensor [B, H, W, C], float32, range [0, 1]
- AUDIO: {'waveform': torch.Tensor [B, C, T], 'sample_rate': int}
         waveform 一律 float32, normalized to [-1, 1]

設計選擇：rawvideo over stdout pipe (非 PNG tempdir)。
理由：避免磁碟 I/O 瓶頸；single subprocess pipe 比 N 個檔案 I/O 快數倍；
代價：整段影片要 fit in RAM。對 ComfyUI workflow 場景 (通常 <60s clip + tensor 已經要進 GPU) 是可接受 trade-off。
若日後遇到 >5min 1080p 素材記憶體爆，可加 chunked decode 模式。
"""
import json
import subprocess

import numpy as np
import torch

from .ffmpeg import get_video_display_dims, probe, probe_video_duration, resolve_ffmpeg_cmd


# x264-style preset → SVT-AV1 numeric preset (0=最慢/最佳壓縮, 13=最快/最差壓縮)
# 為什麼 module-level：避免 compose_finalize / video_io 各維護一份 map drift。
_SVTAV1_PRESET_MAP = {
    "ultrafast": "12", "superfast": "11", "veryfast": "10",
    "faster": "9", "fast": "8",
    "medium": "8",
    "slow": "6", "slower": "5", "veryslow": "3",
}


def svtav1_preset_from_name(name):
    """把 x264-style preset 字串映射到 SVT-AV1 numeric preset；未知值退到 medium (8)。"""
    return _SVTAV1_PRESET_MAP.get(name, "8")


def _ffprobe_video_stream(path):
    info = probe(path)
    if info is None:
        raise RuntimeError(f"[MediaForge.video_io] ffprobe 無法解析：{path}")
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError(f"[MediaForge.video_io] 找不到 video stream：{path}")
    return v, info


def _parse_fps(rate_str):
    # FFmpeg r_frame_rate / avg_frame_rate 是 "num/den" 形式
    try:
        num, den = rate_str.split("/")
        d = float(den)
        return float(num) / d if d != 0 else 0.0
    except (ValueError, AttributeError, ZeroDivisionError):
        return 0.0


# 安全上限：rgb24 raw bytes 不該超過此值；caller 仍要為 peak memory 預留空間。
# R10 P2 fix：原本 4 GiB 是 raw 上限，但 decode 路徑同時持有 proc.stdout (bytes copy) +
# uint8 numpy + float32 tensor，peak 約 raw × 5。若 raw 拉到 4 GiB，peak >20 GiB → OOM。
# 把 raw cap 降到 1 GiB → peak ≤ 5 GiB，仍能涵蓋 ~16s 1080p (足夠 ComfyUI 短片 workflow)；
# 長片請用 max_frames / target_fps 降採樣。
MAX_DECODE_BYTES = 1 * 1024 * 1024 * 1024
# decode 過程峰值倍率：proc.stdout (1x) + np.frombuffer.copy() (1x) + tensor float32 (4x) ≈ 6x
DECODE_PEAK_RATIO = 6


def decode_video_to_tensor(path, target_fps=0.0, max_frames=0):
    """Decode video → IMAGE tensor [B,H,W,C] float32 in [0,1] + fps + metadata dict.

    target_fps=0 → 沿用原始 fps；max_frames=0 → 不限制。
    """
    v, info = _ffprobe_video_stream(path)
    # 用共用 helper 處理 rotation；確保 LoadVideoFrames / ProbeMedia 行為一致 (R9/R10)
    width, height = get_video_display_dims(v)
    if width == 0 or height == 0:
        raise RuntimeError(f"[MediaForge.video_io] 無法取得影片解析度：{path}")

    # 為什麼不直接 or-chain：ffprobe 對某些 VFR / 編輯軟體輸出回傳 avg_frame_rate="0/0"，
    # 而 "0/0" 是 truthy string，會被 or 接住、parse 出 0.0 — 把 fps=0 沖到下游 save/compose。
    # 改為各自 parse、選第一個 > 0 的。
    src_fps = (
        _parse_fps(v.get("avg_frame_rate") or "0/1")
        or _parse_fps(v.get("r_frame_rate") or "0/1")
        or 0.0
    )
    effective_fps = target_fps if target_fps > 0 else src_fps

    # 預估 raw decode 大小，超 MAX_DECODE_BYTES 直接 raise。
    # R9 P2 fix：原本用 format.duration (container 整體) — 若 audio 比 video 長 (e.g.,
    # 編輯軟體留尾巴音軌)、會用 audio 長度估算 frames、可能誤殺合法短影片。
    # 改用 video stream 自己的時長。
    dur_sec = probe_video_duration(path) or 0.0
    if dur_sec > 0:
        est_frames = int(round((effective_fps or src_fps or 30) * dur_sec))
        if max_frames > 0:
            est_frames = min(est_frames, max_frames)
        est_bytes = est_frames * width * height * 3
        est_peak = est_bytes * DECODE_PEAK_RATIO
        if est_bytes > MAX_DECODE_BYTES:
            raise RuntimeError(
                f"[MediaForge.video_io] 預估解碼需 {est_bytes / 1024 / 1024 / 1024:.2f} GiB raw RGB "
                f"(peak ~{est_peak / 1024 / 1024 / 1024:.1f} GiB inc. tensor copies)，"
                f"超過安全上限 {MAX_DECODE_BYTES / 1024 / 1024 / 1024:.1f} GiB"
                f"（{width}x{height}, ~{est_frames} frames）。"
                "請用 max_frames 截短或 target_fps 降採樣再試。"
            )

    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if target_fps > 0:
        cmd.extend(["-vf", f"fps={target_fps}"])
    if max_frames > 0:
        cmd.extend(["-frames:v", str(max_frames)])
    cmd.extend(["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])

    proc = subprocess.run(resolve_ffmpeg_cmd(cmd), check=False, capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr.decode("utf-8", errors="replace")).strip().splitlines()[-30:])
        raise RuntimeError(f"[MediaForge.video_io] FFmpeg 解碼失敗 (exit {proc.returncode}):\n{tail}")

    frame_bytes = width * height * 3
    total = len(proc.stdout)
    if total == 0:
        raise RuntimeError(f"[MediaForge.video_io] FFmpeg 解碼成功但無輸出 (空影片?)：{path}")
    if total % frame_bytes != 0:
        raise RuntimeError(
            f"[MediaForge.video_io] rawvideo 輸出長度 {total} 不是 frame_bytes={frame_bytes} 倍數，"
            "可能是 pix_fmt / 解析度 mismatch"
        )
    n_frames = total // frame_bytes
    arr = np.frombuffer(proc.stdout, dtype=np.uint8).reshape(n_frames, height, width, 3)
    # uint8 → float32 [0,1]；contiguous 後給 torch 才不會觸發隱式 copy
    tensor = torch.from_numpy(arr.copy()).float() / 255.0

    meta = {
        "src_fps": src_fps,
        "effective_fps": float(effective_fps),
        "width": width,
        "height": height,
        "n_frames": n_frames,
        "src_codec": v.get("codec_name", ""),
        "duration_sec": float(info.get("format", {}).get("duration", 0.0) or 0.0),
    }
    return tensor, meta


def decode_audio_to_dict(path, target_sr=0):
    """Decode media audio → {'waveform': Tensor[B=1, C, T] float32 in [-1,1], 'sample_rate': int}.

    若 path 沒有 audio stream → 回傳 None (caller 決定處置；canonical 不接受空 stream)。
    target_sr=0 → 沿用原始 sample rate。
    """
    info = probe(path)
    if info is None:
        raise RuntimeError(f"[MediaForge.video_io] ffprobe 無法解析：{path}")
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if a is None:
        return None

    channels = int(a.get("channels", 0)) or 2
    src_sr = int(a.get("sample_rate", 0)) or 44100
    out_sr = target_sr if target_sr > 0 else src_sr

    cmd = [
        "ffmpeg", "-v", "error", "-i", path,
        "-vn",
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-ac", str(channels),
        "-ar", str(out_sr),
        "pipe:1",
    ]
    proc = subprocess.run(resolve_ffmpeg_cmd(cmd), check=False, capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr.decode("utf-8", errors="replace")).strip().splitlines()[-30:])
        raise RuntimeError(f"[MediaForge.video_io] FFmpeg audio decode 失敗 (exit {proc.returncode}):\n{tail}")

    if not proc.stdout:
        return None

    # interleaved float32 → [T, C] → transpose 成 [C, T] → unsqueeze batch=1 成 [1, C, T]
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    n_samples = raw.size // channels
    if n_samples == 0:
        return None
    raw = raw[: n_samples * channels].reshape(n_samples, channels).T  # [C, T]
    waveform = torch.from_numpy(raw.copy()).unsqueeze(0)  # [1, C, T]
    return {"waveform": waveform, "sample_rate": int(out_sr)}


def encode_tensor_to_video(
    tensor,
    output_path,
    fps,
    *,
    audio=None,
    codec="libx264",
    pix_fmt="yuv420p",
    crf=18,
    bitrate=None,
    preset="medium",
    extra_args=None,
):
    """Encode IMAGE tensor [B,H,W,C] float32 [0,1] → video file, optionally muxing AUDIO dict.

    若 bitrate 給定 → bitrate mode；否則 CRF mode (預設)。
    `extra_args` 給呼叫方 inject codec-specific 旗標 (e.g., x265 params, ProRes profile)。
    """
    if tensor.ndim != 4 or tensor.shape[-1] != 3:
        raise ValueError(
            f"[MediaForge.video_io] tensor shape 必須是 [B,H,W,3]，但拿到 {tuple(tensor.shape)}"
        )
    if fps <= 0:
        raise ValueError(f"[MediaForge.video_io] fps 必須 > 0，但拿到 {fps}")

    n, h, w, _ = tensor.shape
    # float32 [0,1] → uint8 contiguous bytes
    arr = (tensor.detach().cpu().clamp(0.0, 1.0) * 255.0).round().to(torch.uint8).numpy()
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{w}x{h}", "-r", f"{fps}",
        "-i", "pipe:0",
    ]

    audio_tmp = None
    if audio is not None:
        audio_tmp = _audio_dict_to_pipe_args(audio, cmd)

    # 共用 builder 處理各家 encoder 的 preset / crf / bitrate 差異（含 NVENC）
    # 避開 module-load 循環：encoder.py 對 video_io.svtav1_preset_from_name 是 lazy import
    from .encoder import build_encoder_args
    cmd.extend(build_encoder_args(codec, crf=crf, bitrate=bitrate, preset=preset))
    cmd.extend(["-pix_fmt", pix_fmt])

    if extra_args:
        cmd.extend(list(extra_args))

    if audio is not None:
        # 為什麼不用 -shortest：若 audio 比 video 短 (常見於 SD 生圖序列 + 短背景音、
        # 或編輯過的素材)，-shortest 會把整段截到 audio 結尾、silently 丟掉 trailing 幀。
        # 改用 explicit `-t {video_duration}` 鎖 output 長度為 video 時長；
        # 短 audio 會被 ffmpeg 自動 mux 完早結束，但 video 仍輸出到結尾 (R4 P2 fix)。
        video_duration = n / float(fps)
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-t", f"{video_duration:.6f}"])

    cmd.append(output_path)

    proc = subprocess.Popen(resolve_ffmpeg_cmd(cmd), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr = b""
    write_err = None
    try:
        # FFmpeg 可能在 input 還沒 fully 餵完就死掉 (codec init 失敗、output 路徑不可寫等)；
        # 此時 communicate 內部會收 BrokenPipeError — 抓住、再去拿 stderr，才能給出有用的錯訊。
        # 為什麼用 communicate(input=...) 而非自己 write/close：communicate 會在 background thread
        # 同時 drain stderr，避免 stderr pipe 滿了反向 deadlock stdin write。
        try:
            _, stderr = proc.communicate(input=arr.tobytes())
        except BrokenPipeError as e:
            write_err = e
            # 再次嘗試 drain stderr — communicate 已 close stdin
            try:
                _, stderr = proc.communicate()
            except (BrokenPipeError, ValueError):
                pass
    finally:
        if audio_tmp is not None:
            _cleanup_audio_tmp(audio_tmp)

    if proc.returncode != 0 or write_err is not None:
        tail = "\n".join(stderr.decode("utf-8", errors="replace").strip().splitlines()[-30:])
        suffix = f"\n（stdin write 中斷：{write_err}）" if write_err else ""
        raise RuntimeError(
            f"[MediaForge.video_io] FFmpeg encode 失敗 (exit {proc.returncode}):\n{tail}{suffix}"
        )
    return output_path


def write_audio_dict_to_wav(audio_dict):
    """Validate AUDIO dict and write a temp 16-bit PCM .wav; returns the temp path.

    Caller decides how to feed the file to ffmpeg (e.g. `-i tmp.wav` for muxing)
    and is responsible for `os.unlink()` in a finally block.

    為什麼用 tmpfile 不用 second pipe — FFmpeg subprocess.Popen 只能餵一個 stdin；
    要同時 inject 兩路 raw stream 得用 named pipe (Unix only) 或 tmp wav。tmp wav 跨平台、簡單可靠。
    """
    import os
    import tempfile
    import wave

    waveform = audio_dict.get("waveform")
    sr = int(audio_dict.get("sample_rate") or 0)
    if waveform is None or sr <= 0:
        raise ValueError(
            "[MediaForge.video_io] AUDIO dict 缺 'waveform' 或 'sample_rate'，"
            "需符合 ComfyUI canonical {'waveform': Tensor[B,C,T], 'sample_rate': int}"
        )
    if waveform.ndim != 3:
        raise ValueError(
            f"[MediaForge.video_io] waveform 必須是 [B,C,T] 三維 tensor，但拿到 {tuple(waveform.shape)}"
        )

    # 只取 batch[0] — 多 batch audio 在 video mux 沒語意
    wav = waveform[0].detach().cpu().clamp(-1.0, 1.0).numpy()  # [C, T]
    pcm16 = (wav.T * 32767.0).round().astype(np.int16)  # [T, C]

    fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="mf_audio_")
    os.close(fd)
    with wave.open(tmp_path, "wb") as w:
        w.setnchannels(pcm16.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())

    return tmp_path


def mux_path_with_audio_dict(video_path, audio_dict):
    """Pre-mux 既有 video 檔案 + AUDIO dict → 一個 temp .mp4。

    用途：dual-input file-consumer node 在 path mode 同時收到 `video_path` (檔案) 跟
    `audio` (AUDIO dict) 時，原本 path-mode 分支完全忽略 audio dict → silent drop。
    這個 helper 把兩者合成 temp.mp4，下游 filter graph 仍只需讀 `[0:v]` + `[0:a]`，
    幾乎不用改動 — 跟 tensor mode 走 encode_tensor_to_tempfile 後的 source_path 對稱。

    為什麼 `-c:v copy` + AAC encode audio：video stream container-mux 即可、零 transcode
    開銷；audio 從 PCM WAV 一次轉 AAC 也很快。下游節點之後跑自己的 filter graph 才會
    re-encode video — 這層的 video copy 是純 transit。

    Caller 必須在 finally 裡 `os.unlink()` 回傳的 path（同 encode_tensor_to_tempfile 的契約）。
    """
    import os
    import subprocess
    import tempfile

    from .ffmpeg import resolve_ffmpeg_cmd

    wav_path = write_audio_dict_to_wav(audio_dict)
    fd, tmp_mp4 = tempfile.mkstemp(suffix=".mp4", prefix="mf_path_audio_mux_")
    os.close(fd)
    try:
        cmd = resolve_ffmpeg_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-i", video_path,    # input 0：video (可能含 audio，但會被忽略)
            "-i", wav_path,      # input 1：dict-supplied audio
            "-map", "0:v",       # 拿 input 0 的 video
            "-map", "1:a",       # 用 input 1 的 audio，丟掉 input 0 自帶的 audio (若有)
            "-c:v", "copy",      # 零 transcode 開銷
            "-c:a", "aac", "-b:a", "192k",
            tmp_mp4,
        ])
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            try:
                os.unlink(tmp_mp4)
            except OSError:
                pass
            tail = "\n".join((r.stderr or "").strip().splitlines()[-30:])
            raise RuntimeError(
                f"[MediaForge.video_io] path + audio dict 預合成失敗 (exit {r.returncode}):\n{tail}"
            )
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
    return tmp_mp4


def _audio_dict_to_pipe_args(audio_dict, cmd):
    """Thin wrapper: write AUDIO dict to temp wav + append `-i <tmpfile>` to cmd.

    Kept for encode_tensor_to_video's pipe-mux path. New callers that want to
    build their own ffmpeg cmd should use write_audio_dict_to_wav() directly.
    """
    tmp_path = write_audio_dict_to_wav(audio_dict)
    cmd.extend(["-i", tmp_path])
    return tmp_path


def _cleanup_audio_tmp(tmp_path):
    import os
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


def encode_tensor_to_tempfile(frames, fps, audio=None):
    """Encode IMAGE batch (+ optional AUDIO dict) to a temp .mp4 for use as an
    intermediate in file-native FFmpeg pipelines.

    Why this exists: nodes like MF_BurnSubtitle / MF_LoopVideo / MF_TrimByRanges
    run `ffmpeg -i input.mp4 -vf ...`. When upstream is an in-memory tensor (from
    VHS / AnimateDiff / etc.), we materialise it to a temp .mp4 once, FFmpeg
    processes that, caller unlinks in finally. Mirrors the temp-WAV pattern in
    detect_silence.py / whisper_transcribe.py but for video.

    Quality is fixed h264/yuv420p/crf=18 — downstream re-encodes anyway, so
    chasing higher quality here only wastes time.

    Caller is responsible for `os.unlink()` (use try/finally).
    """
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".mp4", prefix="mf_tensor_")
    os.close(fd)
    encode_tensor_to_video(
        frames, tmp, fps=fps, audio=audio,
        codec="libx264", pix_fmt="yuv420p", crf=18, preset="medium",
    )
    return tmp


def get_video_metadata_json(path):
    """Convenience: full ffprobe JSON as plain string for metadata-output ports."""
    info = probe(path)
    if info is None:
        return "{}"
    try:
        return json.dumps(info, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"
