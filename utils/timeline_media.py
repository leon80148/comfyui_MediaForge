"""Timeline 素材解碼：來源影片區段、素材切片、圖片（LoadSegmentFrames / LoadSegmentMedia / EncodeH3Segment 共用）。

FFmpeg 一律走 timeline_source 的安全輸入旗標。時間軸幀 f ↔ 素材時間 f·den/num 秒（相對第一個
video 幀）；先 accurate seek 再以 fps=num/den 重取樣——同 fps 的來源逐幀精確，不同 fps 則是決定
性的最近幀取樣。
"""
import subprocess
from fractions import Fraction

import numpy as np
import torch
from PIL import Image, ImageOps, UnidentifiedImageError

from .ffmpeg import resolve_ffmpeg_cmd
from .timeline_messages import TimelineError
from .timeline_source import (ffmpeg_input_args, opaque_fingerprint, parse_audio_probe, parse_probe_output,
                              resolve_timeline_source, run_probe)

AUDIO_PREROLL = Fraction(1)  # 秒；遠大於常見音訊封包（AAC ≈ 21 ms、PCM-in-MKV ≈ 85 ms）
EDIT_LABEL = "Edit Timeline"
# EXIF Orientation 5–8 是轉 90° / 270°：寬高對調
_EXIF_SWAPS_AXES = {5, 6, 7, 8}


def _available_us(stream_us, total_us, origin_us, format_start_us):
    """從時間原點起可用的時長：stream duration 優先，否則容器 duration 扣掉原點前的部分。"""
    if stream_us is not None:
        return stream_us
    if total_us is None:
        return None
    return total_us - max(0, (origin_us or 0) - (format_start_us or 0))


def timeline_frames(duration_us, fps):
    num, den = fps
    return None if duration_us is None else max(0, duration_us * num // (den * 1_000_000))


def image_fields(path):
    """圖片尺寸（套用 EXIF 方向，與 core LoadImage 一致）；只讀檔頭。讀不了 → ValueError。"""
    try:
        with Image.open(path) as img:
            width, height = img.size
            orientation = img.getexif().get(0x0112)
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError(f"無法讀取圖片（{e}）") from e
    if orientation in _EXIF_SWAPS_AXES:
        width, height = height, width
    return {"width": width, "height": height}


def video_fields(info):
    """parse_probe_output() → 快照 / probe route 共用的 video 欄位（duration_us = 所選 stream 的有效時長）。"""
    return {**{k: info[k] for k in ("fps", "source_start_us", "format_start_us", "video_ordinal", "has_audio",
                                    "audio_sample_rate", "audio_channels", "audio_start_us")},
            "display_size": list(info["display_size"]),
            "duration_us": _available_us(info["video_duration_us"], info["duration_us"], info["source_start_us"],
                                         info["format_start_us"])}


def audio_fields(info):
    """parse_audio_probe() → 快照 / probe route 共用的 audio 欄位。"""
    return {**{k: info[k] for k in ("audio_sample_rate", "audio_channels", "audio_start_us", "format_start_us")},
            "duration_us": _available_us(info["audio_duration_us"], info["duration_us"], info["audio_start_us"],
                                         info["format_start_us"])}


def describe_material(material, fps, input_dir=None):
    """素材快照（plan 用，不含絕對路徑）。任何讀不到 / 不支援的狀況都是 E_MATERIAL。"""
    def fail(reason):
        raise TimelineError(EDIT_LABEL, "E_MATERIAL", name=material["name"], reason=reason)

    try:
        path = resolve_timeline_source(material["ref"], input_dir)
    except (ValueError, FileNotFoundError) as e:
        fail(str(e))
    snap = {"id": material["id"], "kind": material["kind"], "name": material["name"], "ref": material["ref"],
            "fingerprint": opaque_fingerprint(path)}
    if material["kind"] == "image":
        try:
            snap.update(image_fields(path))
        except ValueError as e:
            fail(str(e))
        return snap
    try:
        stdout = run_probe(path)
    except RuntimeError as e:
        fail(str(e))
    if material["kind"] == "video":
        info = parse_probe_output(stdout)
        if info is None:
            fail("沒有影像串流")
        snap.update(video_fields(info))
    else:
        info = parse_audio_probe(stdout)
        if info is None or not info["audio_sample_rate"] or not info["audio_channels"]:
            fail("沒有可用的音訊串流")
        snap.update(audio_fields(info))
    frames = timeline_frames(snap["duration_us"], fps)
    if frames is None or snap["duration_us"] <= 0:
        fail("無法取得長度")
    snap["frames"] = frames
    return snap


def scale_filter(resize, width, height):
    if resize == "fit":
        return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if resize == "fill":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    if resize == "stretch":
        return f"scale={width}:{height},setsar=1"
    return None


def run_capture(cmd, label, what):
    proc = subprocess.run(resolve_ffmpeg_cmd(cmd), capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-30:])
        raise RuntimeError(f"[{label}] FFmpeg {what}失敗 (exit {proc.returncode})：\n{tail}")
    return proc.stdout


def decode_video(path, seek, count, fps, size, scale, label, stream=0):
    num, den = fps
    vf = f"fps={num}/{den}" + (f",{scale}" if scale else "")
    # -map 用 probe 選定的 video 序號（跳過封面圖）；不指定時 FFmpeg 可能選到 default 的其他串流
    out = run_capture(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{float(seek):.9f}"] + ffmpeg_input_args(path)
                      + ["-map", f"0:v:{stream}", "-vf", vf, "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "rgb24",
                         "pipe:1"],
                      label, "解碼畫面")
    w, h = size
    n = len(out) // (w * h * 3)
    arr = np.frombuffer(out[: n * w * h * 3], dtype=np.uint8).reshape(n, h, w, 3)
    return torch.from_numpy(arr.copy()).float() / 255.0


def decode_audio(path, seek, video_start, count, sample_rate, channels, source_channels, label):
    # 輸入端 -ss 跳到「不晚於目標的 video 關鍵幀」所在的 cluster：音訊可能晚一個封包才開始，第一個
    # video 幀之前的音訊則整段讀不到；raw PCM 又沒有時間戳。所以往前多解一段——pre-roll 會落到第一個
    # video 幀之前時就從檔頭讀——aresample 依 PTS 補齊空白（音軌晚於起點時也靠它），再以 sample 數精確裁掉前段
    pre = seek if seek - AUDIO_PREROLL < video_start else AUDIO_PREROLL
    # 重取樣放在最前面：atrim / apad 以「進到它們的取樣率」計數，-ar 是在 filter chain 最後才插入
    af = (f"aresample={sample_rate}:async=1:first_pts=0,atrim=start_sample={round(pre * sample_rate)},"
          f"asetpts=PTS-STARTPTS,apad=whole_len={count},atrim=end_sample={count}")
    if source_channels == 1 and channels == 2:
        af += ",pan=stereo|c0=c0|c1=c0"  # -ac 2 would upmix mono at -3 dB; the spec duplicates it
    head = ["-ss", f"{float(seek - pre):.9f}"] if seek > pre else []
    out = run_capture(["ffmpeg", "-v", "error", "-nostdin"] + head + ffmpeg_input_args(path)
                      + ["-map", "0:a:0", "-af", af, "-ac", str(channels), "-ar", str(sample_rate), "-f", "f32le", "pipe:1"],
                      label, "解碼音訊")
    pcm = np.frombuffer(out, dtype=np.float32).reshape(-1, channels).T
    return torch.from_numpy(pcm.copy()).unsqueeze(0)


# ---- 素材切片（manifest item → IMAGE / AUDIO；EncodeH3Segment 與 LoadSegmentMedia 共用）----

IMAGE_MAX_SHORT_EDGE = 2048  # core H3 `max` 模式的上限；更大的圖先在這裡縮，避免整張高解析度進 RAM


def material_path(item, label):
    path = resolve_timeline_source(item["ref"])
    if opaque_fingerprint(path) != item["fingerprint"]:
        raise TimelineError(label, "E_SOURCE_CHANGED")
    return path


def material_origin(snapshot, key):
    # -ss 以容器起點為 0，素材時間以 video / audio 第一個 PTS 為 0
    return Fraction((snapshot[key] or 0) - (snapshot["format_start_us"] or 0), 1_000_000)


def load_image(item, label, max_short_edge=IMAGE_MAX_SHORT_EDGE):
    """→ IMAGE [1,H,W,3]（套用 EXIF 方向；短邊超過上限時等比縮小，資源政策）。"""
    with Image.open(material_path(item, label)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        short = min(img.size)
        if max_short_edge and short > max_short_edge:
            scale = max_short_edge / short
            img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
        arr = np.asarray(img, dtype=np.uint8)
    return torch.from_numpy(arr.copy()).float().div_(255.0).unsqueeze(0)


def load_video_slice(item, start, count, fps, label, segment, scale=None, size=None):
    """素材 video 第 start 幀（時間軸幀）起 count 幀；scale / size 為 FFmpeg 縮放（None = 原尺寸）。"""
    snap = item["snapshot"]
    path = material_path(item, label)
    num, den = fps
    seek = material_origin(snap, "source_start_us") + Fraction(start * den, num)
    size = tuple(size) if size else tuple(snap["display_size"])
    frames = decode_video(path, seek, count, fps, size, scale, label, snap["video_ordinal"])
    if frames.shape[0] < count:
        raise TimelineError(label, "E_SOURCE_SHORTFALL", segment=segment)
    return frames


def load_audio_slice(item, start, count_frames, fps, label, sample_rate=None, multiple=1, origin_key="audio_start_us"):
    """素材音訊：第 start 幀起 count_frames 幀 → AUDIO（雙聲道）；multiple > 1 時從起點往後裁到整數倍。"""
    snap = item["snapshot"]
    path = material_path(item, label)
    num, den = fps
    rate = sample_rate or snap["audio_sample_rate"]
    count = (2 * count_frames * rate * den + num) // (2 * num)
    count -= count % multiple
    origin = material_origin(snap, origin_key)
    wave = decode_audio(path, origin + Fraction(start * den, num), origin, count, rate, 2, snap["audio_channels"], label)
    return {"waveform": wave, "sample_rate": rate}
