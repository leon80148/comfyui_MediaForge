"""Encoder configuration — CPU + NVENC codec list, dynamic capability probe, ffmpeg arg builder.

集中放 codec metadata 跟 encoder-specific 參數 mapping。為什麼存在：
- 之前 encode args 散在 utils/video_io.py 跟 nodes/compose_finalize.py 兩處、邏輯幾乎一樣
- NVENC 加進來時兩個地方都要改、容易 drift
- 還有 codec capability auto-detect 需要單一 cache 點

NVENC preset 跟 libx264 風格名字不同（p1=最快 → p7=最慢）— 此 module 處理映射。
Rate control 也不同（NVENC 沒 -crf、走 -rc vbr -cq <n> 達到 CRF-style 等效）。
"""
import subprocess

from .ffmpeg import get_ffmpeg_bin


# x264-style preset name → NVENC preset (p1=fastest / p7=slowest)
# NVENC 新版 API 用 p1..p7（driver >= 460）；舊 alias 是 fast/medium/slow/hp/hq 等、避開混淆
_NVENC_PRESET_MAP = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2",
    "faster": "p3", "fast": "p3",
    "medium": "p4",
    "slow": "p5", "slower": "p6", "veryslow": "p7",
}


def nvenc_preset_from_name(name):
    """x264-style preset → NVENC p1-p7；未知值退 p4 (medium)。"""
    return _NVENC_PRESET_MAP.get(name, "p4")


# CPU codec catalogue — 永遠提供（軟體 encoder、無硬體依賴）
_CODECS_CPU = {
    "h264 (libx264)":      ("libx264",    "yuv420p"),
    "hevc (libx265)":      ("libx265",    "yuv420p"),
    "av1 (libsvtav1)":     ("libsvtav1",  "yuv420p"),
    "prores (prores_ks)":  ("prores_ks",  "yuv422p10le"),
}

# NVENC codec catalogue — 只在 ffmpeg binary 含對應 encoder 時加入 dropdown
# av1_nvenc 需要 Ada Lovelace (RTX 4000+)；老 NVIDIA 卡會 runtime fail（看不到 NVENC capable devices）
_CODECS_NVENC = {
    "h264 NVIDIA GPU (h264_nvenc)":  ("h264_nvenc",  "yuv420p"),
    "hevc NVIDIA GPU (hevc_nvenc)":  ("hevc_nvenc",  "yuv420p"),
    "av1 NVIDIA GPU (av1_nvenc)":    ("av1_nvenc",   "yuv420p"),
}


_ENCODER_SET_CACHE = None  # None = 還沒 probe / set = probe 結果


def get_available_encoders():
    """Probe `ffmpeg -encoders` 取所有 video encoder 名字。Cache per-process。

    Probe 一次大約 100ms、cache 後 0ms。ffmpeg binary 換了（不太可能 mid-process）
    cache 不會自動 invalidate — 重啟 ComfyUI 解決。
    """
    global _ENCODER_SET_CACHE
    if _ENCODER_SET_CACHE is not None:
        return _ENCODER_SET_CACHE

    binary = get_ffmpeg_bin()
    if not binary:
        _ENCODER_SET_CACHE = set()
        return _ENCODER_SET_CACHE

    try:
        r = subprocess.run(
            [binary, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        out = set()
        for raw_line in (r.stdout or "").splitlines():
            line = raw_line.strip()
            # 格式 "<flags> <name>  <desc>"；flags 以 V 開頭就是 video encoder
            parts = line.split(maxsplit=2)
            if len(parts) >= 2 and parts[0].startswith("V"):
                out.add(parts[1])
        _ENCODER_SET_CACHE = out
    except Exception as e:
        print(f"[MediaForge.encoder] ffmpeg -encoders probe 失敗：{e}")
        _ENCODER_SET_CACHE = set()
    return _ENCODER_SET_CACHE


def get_available_codecs(include_gpu=True):
    """Return display_name → (codec_id, pix_fmt) dict。

    CPU codec 永遠在；NVENC 視 ffmpeg binary 能力決定加不加。`include_gpu=False`
    可強制 CPU-only（給未來需要排除 GPU 的場景）。
    """
    codecs = dict(_CODECS_CPU)
    if include_gpu:
        encoders = get_available_encoders()
        for display_name, (codec_id, pix_fmt) in _CODECS_NVENC.items():
            if codec_id in encoders:
                codecs[display_name] = (codec_id, pix_fmt)
    return codecs


def build_encoder_args(codec_id, *, crf=None, bitrate=None, preset=None):
    """Build `-c:v <codec> ...` args based on encoder family.

    Returns list of args ready to extend into ffmpeg cmd。Caller responsible for
    `-pix_fmt` (從 codec map 拿) 跟 audio args / extra_args (e.g. ProRes profile)。

    Rate control 各家殊途：
    - libx264 / libx265:  -crf X  -preset name
    - libsvtav1:          -crf X  -preset <numeric>   (numeric 走 svtav1_preset_from_name)
    - *_nvenc:            -rc vbr -cq X  -preset p1..p7   (cq = CRF-style；rc=vbr 啟用 cq)
    - prores_ks:          (caller 自己加 -profile:v；本函式不碰)
    """
    # Lazy import 避開 utils.encoder ↔ utils.video_io 的 module load 循環
    from .video_io import svtav1_preset_from_name

    args = ["-c:v", codec_id]

    if codec_id in ("libx264", "libx265"):
        if preset:
            args.extend(["-preset", preset])
        if bitrate:
            args.extend(["-b:v", str(bitrate)])
        elif crf is not None:
            args.extend(["-crf", str(crf)])

    elif codec_id == "libsvtav1":
        if preset:
            args.extend(["-preset", svtav1_preset_from_name(preset)])
        if bitrate:
            args.extend(["-b:v", str(bitrate)])
        elif crf is not None:
            args.extend(["-crf", str(crf)])

    elif codec_id.endswith("_nvenc"):
        if preset:
            args.extend(["-preset", nvenc_preset_from_name(preset)])
        if bitrate:
            # bitrate mode：VBR 配 bitrate target，硬體控制
            args.extend(["-rc", "vbr", "-b:v", str(bitrate)])
        elif crf is not None:
            # CRF-equivalent mode：NVENC 用 -rc vbr -cq <n>，n 範圍 0-51 同 libx264 -crf
            args.extend(["-rc", "vbr", "-cq", str(crf)])

    # prores_ks 等其他 codec：caller 透過 extra_args 自己處理 -profile:v 等
    return args
