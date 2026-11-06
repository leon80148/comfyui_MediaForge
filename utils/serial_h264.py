"""Serial H.264 export behind 💾 Save Video: one MP4 per run, with the decoders, swscale, libx264 and AAC each on one thread.

The node is a V3 class and has to import comfy_api at module top; everything else lives here and needs only
av / numpy / torch, so it can be tested without ComfyUI.
"""
import json
import math
import ntpath
import os
import re
import uuid
from fractions import Fraction

import av
import numpy as np
import torch
from av.video.reformatter import ColorPrimaries, ColorRange, ColorTrc, Colorspace, VideoReformatter

LABEL = "Save Video · Serial H.264"
FINAL_NAME = "video.mp4"
PARTIAL_NAME = "video.partial.mp4"

# The tags ComfyUI writes for its sRGB / HDR / HDR PQ videos: primaries, transfer, AVColorSpace tag (1 = BT.709,
# 9 = BT.2020 non-constant), then the swscale matrix by name and the name the report uses.
_COLORS = {
    "sRGB": (ColorPrimaries.BT709, ColorTrc.IEC61966_2_1, 1, "ITU709", "bt709"),
    "HDR": (ColorPrimaries.BT2020, ColorTrc.ARIB_STD_B67, 9, "BT2020", "bt2020nc"),
    "HDR PQ": (ColorPrimaries.BT2020, ColorTrc.SMPTE2084, 9, "BT2020", "bt2020nc"),
}
# 10-bit keeps 16-bit RGB until swscale converts to YUV.
_FORMATS = {8: ("rgb24", "yuv420p", 255.0, np.uint8), 10: ("rgb48le", "yuv420p10le", 65535.0, np.uint16)}
_LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1"}
# x264's own defaults, spelled out so the report can state them; they match the original node's files.
_X264_OPTIONS = {"crf": "23", "preset": "medium"}


def prepare_output_dir(output_root, filename_prefix):
    """Create <output_root>/<filename_prefix>-<uuid hex>/ and return (absolute dir, subfolder for /view).

    Every component is created and resolved one at a time, so a symlink or junction pointing out of the output
    directory is refused before anything is written through it.
    """
    prefix = str(filename_prefix).strip().replace("\\", "/")
    parts = [part for part in prefix.split("/") if part not in ("", ".")]
    if prefix.startswith("/") or any(ntpath.splitdrive(part)[0] for part in parts):
        raise ValueError(f"[{LABEL}] filename_prefix 必須是 output 目錄內的相對路徑，收到 {filename_prefix!r}。請拿掉開頭的 / 或磁碟代號。")
    if ".." in parts:
        raise ValueError(f"[{LABEL}] filename_prefix 不能含 ..，收到 {filename_prefix!r}。請改成 output 目錄內的相對路徑。")
    if not parts:
        raise ValueError(f"[{LABEL}] filename_prefix 不能是空的。請填像 MediaForge/serial_video 這樣的前綴。")

    root = os.path.realpath(output_root)
    current = root
    for part in parts[:-1]:
        current = _enter(root, os.path.join(current, part), filename_prefix)
    name = f"{parts[-1]}-{uuid.uuid4().hex}"
    run_dir = _enter(root, os.path.join(current, name), filename_prefix, exclusive=True)
    return run_dir, "/".join(parts[:-1] + [name])


def _enter(root, path, filename_prefix, exclusive=False):
    try:
        os.mkdir(path)
    except FileExistsError:
        if exclusive:
            raise
    real = os.path.realpath(path)
    if not _inside(real, root):
        raise ValueError(f"[{LABEL}] filename_prefix {filename_prefix!r} 經由符號連結或 junction 指到 output 目錄外（{path}）。請改用 output 目錄內的資料夾。")
    if not os.path.isdir(real):
        raise ValueError(f"[{LABEL}] {path} 已存在但不是資料夾。請改用別的 filename_prefix。")
    return real


def _inside(path, root):
    try:
        common = os.path.commonpath([path, root])
    except ValueError:  # different drives on Windows
        return False
    return os.path.normcase(common) == os.path.normcase(root)


def metadata_tags(extra_pnginfo, prompt):
    """Serialize each top-level field to strict JSON; return ({tag: text}, how many NaN / ±Infinity became null).

    A running prompt carries cache markers such as ``is_changed: [NaN]``; those are cleaned in a copy only, the
    prompt and workflow themselves stay as they are.
    """
    fields = {}
    if extra_pnginfo is not None:
        fields.update(extra_pnginfo)
    if prompt is not None:
        fields["prompt"] = prompt
    replaced = 0

    def clean(value):
        nonlocal replaced
        if isinstance(value, float) and not math.isfinite(value):
            replaced += 1
            return None
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return value

    tags = {}
    for key, value in fields.items():
        try:
            tags[str(key)] = json.dumps(clean(value), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as e:
            raise ValueError(f"[{LABEL}] 中繼資料欄位 {key!r} 無法寫成 JSON：{e}。請檢查上游節點，或用 --disable-metadata 啟動 ComfyUI。") from e
    return tags, replaced


def read_source(video):
    """Return (VideoComponents, source report).

    Core's VideoFromFile keeps its own trim / crop / rotation / audio-track rules (get_components_internal); only the
    container is opened here so every stream it may decode is set to one thread first.
    """
    from comfy_api.latest import InputImpl

    kind = type(video)
    if kind is InputImpl.VideoFromFile:
        with av.open(video.get_stream_source(), mode="r") as container:
            for stream in container.streams:
                if stream.type in ("video", "audio") and stream.codec_context is not None:
                    stream.codec_context.thread_count = 1
            components = video.get_components_internal(container)
            video_threads = container.streams.video[0].codec_context.thread_count
            audio_threads = [s.codec_context.thread_count for s in container.streams.audio if s.codec_context is not None]
        return components, {
            "type": "VideoFromFile",
            "decode": "core VideoFromFile.get_components_internal",
            "decode_threads": {"video": video_threads, "audio": max(audio_threads) if audio_threads else None},
            # Core converts decoded YUV to RGB itself with PyAV's default (automatic) swscale threads.
            "decode_rgb_conversion_threads": "core default, not controlled",
        }
    if kind is InputImpl.VideoFromComponents:
        return video.get_components(), {"type": "VideoFromComponents", "decode": "none, already decoded"}
    return video.get_components(), {
        "type": f"{kind.__module__}.{kind.__qualname__}",
        "decode": "delegated to the source's get_components()",
        "decode_threads": "unknown",
    }


def export(components, *, bit_depth, color_space, output_root, filename_prefix, tags, check_interrupt):
    """Validate everything, then encode to video.partial.mp4 and rename it to video.mp4 once it is closed and synced.

    Returns (absolute path, subfolder, report). On cancel or failure there is no video.mp4; the partial file stays
    when packets were already written, otherwise the empty run directory is removed.
    """
    checks = []
    images = components.images
    if not isinstance(images, torch.Tensor) or images.ndim != 4 or images.shape[0] == 0:
        shape = tuple(images.shape) if isinstance(images, torch.Tensor) else type(images).__name__
        raise ValueError(f"[{LABEL}] 畫面必須是非空的 [T, H, W, 3] 張量，收到 {shape}。請檢查上游節點。")
    frames, height, width, channels = images.shape
    if channels == 4 or getattr(components, "alpha", None) is not None:
        raise ValueError(f"[{LABEL}] 影片含 alpha 通道，H.264 無法保存透明度。請先用 Get Video Components 取出 images，再以 Create Video 重建（不接 alpha）。")
    if channels != 3:
        raise ValueError(f"[{LABEL}] 畫面必須是 RGB 三通道，收到 {channels} 通道。請檢查上游節點。")
    if not images.is_floating_point():
        raise ValueError(f"[{LABEL}] 畫面必須是浮點張量，收到 {images.dtype}。請檢查上游節點。")
    if width % 2 or height % 2:
        raise ValueError(f"[{LABEL}] yuv420p 需要偶數寬高，收到 {width}x{height}。請先裁切或縮放成偶數尺寸。")
    checks.append("rgb_shape")
    if bit_depth not in _FORMATS:
        raise ValueError(f"[{LABEL}] 不支援 {bit_depth}-bit 來源，只支援 8-bit 或 10-bit。請先轉成其中一種位深。")
    color = "sRGB" if color_space in (None, "auto") else color_space
    if color not in _COLORS:
        raise ValueError(f"[{LABEL}] 未支援的色彩空間 {color_space!r}，必須是 sRGB、HDR 或 HDR PQ。請檢查上游節點。")
    # PyAV 17 has no BT.2020 matrix and silently converts with BT.601 instead, which would tag wrong colours as HDR.
    matrix = getattr(Colorspace, _COLORS[color][3], None)
    if matrix is None:
        raise RuntimeError(f"[{LABEL}] PyAV {av.__version__} 的 RGB→YUV 轉換沒有 BT.2020 矩陣，{color} 需要 av 18 以上。"
                           "請執行 pip install -U av 後重啟 ComfyUI。")
    source_rate = Fraction(components.frame_rate)
    rate = Fraction(round(source_rate * 1000), 1000)  # core's rule: fps kept to three decimals
    if rate <= 0:
        raise ValueError(f"[{LABEL}] fps 必須大於 0，收到 {float(source_rate)}。請檢查上游節點。")
    for index in range(frames):
        if not torch.isfinite(images[index]).all():
            raise ValueError(f"[{LABEL}] 第 {index} 幀含 NaN 或無限值。請檢查上游節點的輸出。")
    checks.append("rgb_finite")
    audio = _check_audio(components.audio, frames, rate)
    if audio is not None:
        checks.append("audio_finite")

    run_dir, subfolder = prepare_output_dir(output_root, filename_prefix)
    checks.append("output_inside_root")
    partial = os.path.join(run_dir, PARTIAL_NAME)
    final = os.path.join(run_dir, FINAL_NAME)
    try:
        encoded = _encode(partial, images, rate, bit_depth, color, matrix, audio, tags, check_interrupt)
    except BaseException:
        # PyAV creates the file at the first muxed packet; a run that failed earlier leaves only an empty directory.
        if not os.listdir(run_dir):
            os.rmdir(run_dir)
        raise
    x264 = encoded["x264_threads"]
    if x264 is not None and x264.get("threads") == 1 and x264.get("lookahead_threads") == 1:
        checks.append("x264_single_threaded")
    checks.append("encoders_flushed_and_closed")
    with open(partial, "r+b") as f:  # Windows fsync needs a writable handle
        os.fsync(f.fileno())
    checks.append("fsync")
    check_interrupt()
    os.replace(partial, final)
    checks.append("atomic_rename")

    rgb_format, pix_fmt = _FORMATS[bit_depth][:2]
    report = {
        "status": "ok",
        "path": final,
        "subfolder": subfolder,
        "width": width,
        "height": height,
        "frames": frames,
        "fps": float(rate),
        "fps_fraction": f"{rate.numerator}/{rate.denominator}",
        "source_fps": float(source_rate),
        "source_fps_fraction": f"{source_rate.numerator}/{source_rate.denominator}",
        "bit_depth": bit_depth,
        "pix_fmt": pix_fmt,
        "color_space": color,
        "video": {"codec": "h264", "encoder": "libx264", "threads": encoded["video_threads"], "x264_reported_threads": x264,
                  "crf": int(_X264_OPTIONS["crf"]), "preset": _X264_OPTIONS["preset"]},
        "audio": None if audio is None else {
            "codec": "aac", "threads": encoded["audio_threads"], "sample_rate": audio["rate"], "channels": audio["channels"],
            "layout": audio["layout"], "samples": audio["total"], "trimmed_samples": audio["trimmed"],
            # PyAV cuts audio into AAC's 1024-sample frames with a libavfilter graph whose thread pool it leaves on auto;
            # audio filters never hand work to that pool.
            "frame_size_adapter": "PyAV libavfilter graph, thread pool left at its default (not configurable)",
        },
        "color_conversion": {"library": "swscale", "threads": 1, "from": rgb_format, "to": pix_fmt,
                             "matrix": _COLORS[color][4], "range": "tv"},
        "checks": checks,
    }
    return final, subfolder, report


def _check_audio(audio, frames, rate):
    if audio is None:
        return None
    waveform, sample_rate = audio["waveform"], int(audio["sample_rate"])
    if not isinstance(waveform, torch.Tensor) or waveform.ndim != 3 or waveform.shape[0] != 1:
        shape = tuple(waveform.shape) if isinstance(waveform, torch.Tensor) else type(waveform).__name__
        raise ValueError(f"[{LABEL}] 音訊波形必須是 [1, C, S]，收到 {shape}。請檢查上游節點。")
    channels, samples = waveform.shape[1], waveform.shape[2]
    if channels not in _LAYOUTS:
        raise ValueError(f"[{LABEL}] 音訊必須是 1、2 或 6 聲道，收到 {channels} 聲道。請先混音成支援的聲道數。")
    if samples == 0:
        raise ValueError(f"[{LABEL}] 音訊波形是空的。請檢查上游節點，或不要接音訊。")
    if not waveform.is_floating_point():
        raise ValueError(f"[{LABEL}] 音訊波形必須是浮點張量，收到 {waveform.dtype}。請檢查上游節點。")
    if not torch.isfinite(waveform).all():
        raise ValueError(f"[{LABEL}] 音訊含 NaN 或無限值。請檢查上游節點的輸出。")
    rates = av.codec.Codec("aac", "w").audio_rates
    if sample_rate not in rates:
        raise ValueError(f"[{LABEL}] AAC 不支援 {sample_rate} Hz，必須是 {', '.join(map(str, rates))} 之一。請先重新取樣。")
    total = min(samples, math.ceil(Fraction(sample_rate) * frames / rate))
    data = waveform[0, :, :total].detach().cpu()
    if data.dtype == torch.bfloat16:
        data = data.float()
    data = data.numpy().astype(np.float32, copy=False)
    return {"data": data, "rate": sample_rate, "channels": channels, "layout": _LAYOUTS[channels], "total": total,
            "trimmed": samples - total}


def _encode(path, images, rate, bit_depth, color, matrix, audio, tags, check_interrupt):
    rgb_format, pix_fmt, scale, dtype = _FORMATS[bit_depth]
    primaries, trc, tag = _COLORS[color][:3]
    frames, height, width, _ = images.shape
    container = av.open(path, mode="w", format="mp4", options={"movflags": "use_metadata_tags+faststart"})
    try:
        for key, value in tags.items():
            container.metadata[key] = value
        video = container.add_stream("libx264", rate=rate)
        video.width, video.height, video.pix_fmt = width, height, pix_fmt
        video.codec_context.thread_count = 1
        video.options = dict(_X264_OPTIONS)
        _tag_color(video.codec_context, primaries, trc, tag)
        sound = None
        if audio is not None:
            sound = container.add_stream("aac", rate=audio["rate"], layout=audio["layout"])
            sound.codec_context.thread_count = 1
        reformatter = VideoReformatter()
        first_packet = [None]

        def mux_video(packets):
            for packet in packets:
                if first_packet[0] is None:
                    first_packet[0] = _x264_threads(packet)
                container.mux(packet)

        fed = 0
        for index in range(frames):
            check_interrupt()
            rgb = av.VideoFrame.from_ndarray(_quantize(images[index], scale, dtype), format=rgb_format)
            # Exactly the stream's format and size, so PyAV never adds its own (multi-threaded) reformat.
            frame = reformatter.reformat(rgb, format=pix_fmt, dst_colorspace=matrix, dst_color_range=ColorRange.MPEG, threads=1)
            _tag_color(frame, primaries, trc, tag)
            frame.pts = index
            mux_video(video.encode(frame))
            if sound is not None:
                # Feed audio up to this frame's end so the file stays interleaved.
                end = min(audio["total"], math.ceil(Fraction(audio["rate"]) * (index + 1) / rate))
                fed = _feed_audio(container, sound, audio, fed, end)
        # Short clips sit in x264's lookahead until the flush, so the first packet may only come out here.
        mux_video(video.encode(None))
        if sound is not None:
            for packet in sound.encode(None):
                container.mux(packet)
        result = {
            "video_threads": video.codec_context.thread_count,
            "audio_threads": None if sound is None else sound.codec_context.thread_count,
            "x264_threads": first_packet[0] or None,
        }
        container.close()  # writes the trailer and the faststart pass; its failures count as encode failures
    except av.error.FFmpegError as e:
        _close_quietly(container)
        raise RuntimeError(f"[{LABEL}] 編碼失敗：{e}。暫存檔留在 {path}，請查看上方訊息後重試。") from e
    except BaseException:  # includes ComfyUI's InterruptProcessingException
        _close_quietly(container)
        raise
    return result


def _close_quietly(container):
    try:
        container.close()
    except Exception:
        pass  # keep the original error; the partial file stays for diagnosis


def _quantize(frame, scale, dtype):
    data = frame.detach().cpu()
    if data.dtype == torch.bfloat16:
        data = data.float()
    # numpy, not torch: torch would spread these element-wise steps over its own thread pool.
    data = np.clip(data.numpy().astype(np.float32, copy=False), 0.0, 1.0)
    data *= scale
    np.rint(data, out=data)
    return data.astype(dtype)


def _tag_color(target, primaries, trc, tag):
    target.color_primaries = primaries
    target.color_trc = trc
    target.colorspace = tag
    target.color_range = ColorRange.MPEG


def _feed_audio(container, stream, audio, start, end):
    if end <= start:
        return start
    frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(audio["data"][:, start:end]), format="fltp", layout=audio["layout"])
    frame.sample_rate = audio["rate"]
    frame.pts = start
    for packet in stream.encode(frame):
        container.mux(packet)
    return end


def _x264_threads(packet):
    """x264 writes its effective settings into an SEI in the first packet; read the thread counts back from it."""
    data = bytes(packet)
    start = data.find(b"x264 - core")
    if start < 0:
        return {}
    options = data[start:data.find(b"\x00", start)].decode("ascii", "replace")
    found = dict(re.findall(r"\b(threads|lookahead_threads|sliced_threads)=(\d+)", options))
    return {key: int(value) for key, value in found.items()}
