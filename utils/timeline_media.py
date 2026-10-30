"""Timeline 素材解碼：來源影片區段、素材切片、圖片（LoadSegmentFrames / LoadSegmentMedia / EncodeH3Segment 共用）。

FFmpeg 一律走 timeline_source 的安全輸入旗標。時間軸幀 f ↔ 素材時間 f·den/num 秒（相對第一個
video 幀）；先 accurate seek 再以 fps=num/den 重取樣——同 fps 的來源逐幀精確，不同 fps 則是決定
性的最近幀取樣。
"""
import json
import math
import subprocess
from fractions import Fraction

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image, ImageOps, UnidentifiedImageError

from .ffmpeg import resolve_ffmpeg_cmd
from .timeline_materials import audio_sample_count
from .timeline_messages import TimelineError, format_message
from .timeline_source import (ffmpeg_input_args, opaque_fingerprint, parse_audio_probe, parse_probe_output,
                              resolve_timeline_source, run_probe)
from .video_io import MAX_DECODE_BYTES

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
    """素材快照（plan 用，不含絕對路徑）。任何讀不到 / 不支援的狀況都是 E_MATERIAL（detail.reason 是穩定原因碼）。"""
    def fail(code, reason):
        raise TimelineError(EDIT_LABEL, "E_MATERIAL", name=material["name"], reason=reason,
                            detail={"reason": code, "entity": {"kind": "material", "id": material["id"]}, "field": "ref"})

    try:
        path = resolve_timeline_source(material["ref"], input_dir)
    except FileNotFoundError as e:
        fail("not_found", str(e))
    except ValueError as e:
        fail("outside_input", str(e))
    snap = {"id": material["id"], "kind": material["kind"], "name": material["name"], "ref": material["ref"],
            "fingerprint": opaque_fingerprint(path)}
    if material["kind"] == "image":
        try:
            snap.update(image_fields(path))
        except ValueError as e:
            fail("unreadable_image", str(e))
        return snap
    try:
        stdout = run_probe(path)
    except RuntimeError as e:
        fail("probe_failed", str(e))
    if material["kind"] == "video":
        info = parse_probe_output(stdout)
        if info is None:
            fail("no_video_stream", "沒有影像串流")
        snap.update(video_fields(info))
    else:
        info = parse_audio_probe(stdout)
        if info is None or not info["audio_sample_rate"] or not info["audio_channels"]:
            fail("no_audio_stream", "沒有可用的音訊串流")
        snap.update(audio_fields(info))
    frames = timeline_frames(snap["duration_us"], fps)
    if frames is None or snap["duration_us"] <= 0:
        fail("unknown_length", "無法取得長度")
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


def resize_image(image, mode, width, height):
    """IMAGE 版的 scale_filter（fit / fill / stretch）：單張圖走 FFmpeg 會多一次編解碼，這裡直接用 torch 雙三次縮放。"""
    x = image.movedim(-1, 1)
    h, w = x.shape[-2:]
    if mode == "stretch":
        return F.interpolate(x, size=(height, width), mode="bicubic", align_corners=False).clamp(0, 1).movedim(1, -1)
    scale = min(width / w, height / h) if mode == "fit" else max(width / w, height / h)
    nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
    x = F.interpolate(x, size=(nh, nw), mode="bicubic", align_corners=False).clamp(0, 1)
    if mode == "fill":
        top, left = (nh - height) // 2, (nw - width) // 2
        return x[..., top:top + height, left:left + width].movedim(1, -1)
    out = x.new_zeros(x.shape[0], x.shape[1], height, width)
    top, left = (height - nh) // 2, (width - nw) // 2
    out[..., top:top + nh, left:left + nw] = x
    return out.movedim(1, -1)


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
    return torch.from_numpy(arr.copy()).float().div_(255.0)


def _seek_unsafe(path, sample_rate, label):
    """輸入端 -ss 會改變 sample 內容嗎：第一條 audio stream 的時間戳比一個 sample 粗（Matroska / WebM 以 1 ms 計），
    或需要重取樣（seek 後重新起算的重取樣相位與從頭重取樣不同）。"""
    out = run_capture(["ffprobe", "-v", "error"] + ffmpeg_input_args(path)
                      + ["-select_streams", "a:0", "-show_entries", "stream=time_base,sample_rate", "-of", "json"],
                      label, "讀取音訊時間基準")
    stream = json.loads(out)["streams"][0]
    rate = int(stream["sample_rate"])
    return rate != sample_rate or Fraction(stream["time_base"]) > Fraction(1, rate)


def decode_audio(path, seek, origin, count, sample_rate, channels, source_channels, label):
    """最多 count 個 sample；來源提早結束時回傳較短的 PCM（尾端不補），由呼叫端決定補靜音或截短上下文。

    seek / origin 是 -ss 軸（容器起點 = 0）上的時間；素材第 k 個 sample 在 origin + k / sample_rate。
    第一個 sample = half_up((seek − origin)·sample_rate)，與 audio_sample_count 的端點同一個捨入。
    """
    first = math.floor((seek - origin) * sample_rate + Fraction(1, 2))
    preroll = int(AUDIO_PREROLL * sample_rate)
    # 輸入端 -ss 跳到「不晚於目標的 video 關鍵幀」所在的 cluster：音訊可能晚一個封包才開始，第一個
    # video 幀之前的音訊則整段讀不到；raw PCM 又沒有時間戳。所以往前多解一段——pre-roll 會落到素材原點
    # 之前時就從檔頭讀——aresample 依 PTS 補齊空白（音軌晚於起點時也靠它），再以 sample 數精確裁掉前段。
    # -ss 放在素材的 sample 格點上：FFmpeg 把 -ss 截到微秒、再取最近的 sample，不在格點上就會差一個。
    # 時間戳比一個 sample 粗時，seek 後的 sample 依量化過的封包時間戳擺放（FFmpeg 6.1 + MKV 實測差 5–21 個）：
    # 改成一律從檔頭解、只靠 sample 數裁切。async=1 除了起點，只在誤差 > 0.1 s 時補 / 裁，封包時間戳的
    # 次毫秒誤差不會挪動 sample。需要重取樣時也從檔頭解：從 seek 點重新起算的重取樣相位與整檔不同。
    # 代價：解碼量隨目標位置成長（demuxer 要讀過前面整段，含交錯的 video 封包）
    if first < preroll or _seek_unsafe(path, sample_rate, label):
        ss, skip = 0, math.floor(origin * sample_rate + Fraction(1, 2)) + first
    else:
        ss, skip = origin + Fraction(first - preroll, sample_rate), preroll
    # 重取樣放在最前面：atrim 以「進到它的取樣率」計數，-ar 是在 filter chain 最後才插入
    af = (f"aresample={sample_rate}:async=1:first_pts=0,atrim=start_sample={skip},"
          f"asetpts=PTS-STARTPTS,atrim=end_sample={count}")
    if source_channels == 1 and channels == 2:
        af += ",pan=stereo|c0=c0|c1=c0"  # -ac 2 would upmix mono at -3 dB; the spec duplicates it
    head = ["-ss", f"{float(ss):.9f}"] if ss > 0 else []
    out = run_capture(["ffmpeg", "-v", "error", "-nostdin"] + head + ffmpeg_input_args(path)
                      + ["-map", "0:a:0", "-af", af, "-ac", str(channels), "-ar", str(sample_rate), "-f", "f32le", "pipe:1"],
                      label, "解碼音訊")
    pcm = np.frombuffer(out, dtype=np.float32).reshape(-1, channels).T
    return torch.from_numpy(pcm.copy()).unsqueeze(0)


def pad_audio(wave, count, label):
    """素材音訊提早結束（實際解碼比 metadata 短）→ 尾端補靜音到 count 並 W_AUDIO_PAD（§4.3）。"""
    missing = count - wave.shape[-1]
    if missing <= 0:
        return wave
    print(format_message(label, "W_AUDIO_PAD", samples=missing))
    return F.pad(wave, (0, missing))


# ---- 素材切片（manifest item → IMAGE / AUDIO；EncodeH3Segment 與 LoadSegmentMedia 共用）----

IMAGE_MAX_SHORT_EDGE = 2048  # core H3 `max` 模式的上限；更大的圖先在這裡縮，避免整張高解析度進 RAM


def _check_decode(nbytes, label, what, hint):
    # 與 Load Segment Frames 同一個上限（raw bytes）；在配置任何記憶體之前拒絕
    if nbytes > MAX_DECODE_BYTES:
        raise ValueError(f"[{label}] {what}解碼需 {nbytes / 2 ** 30:.2f} GiB，超過上限 {MAX_DECODE_BYTES / 2 ** 30:.0f} GiB。{hint}")


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
        # img.size 只讀檔頭；float32 RGB 是解碼後最大的那份
        _check_decode(img.width * img.height * 3 * 4, label, f"圖片 {img.width}x{img.height} ", "請先縮小圖片。")
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
    # raw rgb24 計上限（同 Load Segment Frames）；float32 tensor 再多 4 倍
    _check_decode(count * size[0] * size[1] * 3, label, f"素材影片 {count} 幀 × {size[0]}x{size[1]} 的 rgb24 ",
                  "請降低 ref_video_short_edge 或 resize 尺寸，或縮短參考範圍。")
    frames = decode_video(path, seek, count, fps, size, scale, label, snap["video_ordinal"])
    if frames.shape[0] < count:
        raise TimelineError(label, "E_SOURCE_SHORTFALL", segment=segment)
    return frames


def load_audio_slice(item, dst_start, frames, fps, label, sample_rate=None, multiple=1, origin_key="audio_start_us"):
    """素材音訊 → AUDIO（雙聲道）：自 source_start 起連續取樣，長度 = 目的區間 [dst_start, dst_start + frames)
    的全域端點差（§4.3，與 manifest 的 samples 同一算式），multiple > 1 時往後裁到整數倍；提早結束補靜音。"""
    snap = item["snapshot"]
    path = material_path(item, label)
    num, den = fps
    rate = sample_rate or snap["audio_sample_rate"]
    count = audio_sample_count(dst_start, frames, rate, fps, multiple)
    _check_decode(count * 2 * 4, label, f"素材音訊 {count} samples（{rate} Hz）", "請縮短參考範圍。")
    origin = material_origin(snap, origin_key)
    wave = decode_audio(path, origin + Fraction(item["source_start"] * den, num), origin, count, rate, 2,
                        snap["audio_channels"], label)
    return {"waveform": pad_audio(wave, count, label), "sample_rate": rate}
