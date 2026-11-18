"""Timeline adapter for the installed Viggle Remake nodes; no model or sampler implementation."""
import inspect
import re

from .timeline_media import (_check_decode, decode_video_window, load_audio_slice, load_image,
                             material_origin, material_path, scale_filter)
from .timeline_plan import H3_FPS, H3_GRID, require_segment
from .timeline_materials import audio_sample_count
from .video_io import _check_decode_ram

LABEL = "Sample Remake Segments"
SIGMAS = {
    "swap": "1.0, 0.990476, 0.965243, 0.923077, 0.8, 0.631579, 0.0",
    "edit": "1.0, 0.983607, 0.96, 0.923077, 0.857143, 0.705882, 0.0",
}
_REQUIRED = {
    "RemakeConditioning": {"clip", "vae", "audio_vae", "source", "prompt", "ref_1", "pins", "frames", "short_edge",
                           "audio", "ref_2", "ref_3", "ref_4"},
    "RemakeKeyframeUntil": {"model", "keyframe_until"},
    "RemakeNoise": {"positive", "seed"},
}
PROMPT_SECTIONS = ("subject_definitions", "summary", "retention_analysis", "detailed_description",
                   "overall_soundscape", "non_diegetic_music")


def remake_nodes():
    import nodes
    out = {}
    for name, params in _REQUIRED.items():
        cls = nodes.NODE_CLASS_MAPPINGS.get(name)
        fn = getattr(cls, getattr(cls, "FUNCTION", "run"), None)
        if fn is None or not params <= set(inspect.signature(fn).parameters):
            raise ValueError(f"[{LABEL}] 缺少或不相容的 {name}，請安裝／更新官方 ComfyUI-Viggle-Remake 並重啟 ComfyUI。")
        out[name] = cls
    return out


def canvas(snapshot, short_edge, width=0, height=0):
    # Explicit timeline dimensions are prepared before calling the official node.
    # Its run() retains a 32-aligned source already on the requested short edge.
    if width or height:
        if any(type(v) is not int or not 32 <= v <= 8192 or v % 32 for v in (width, height)):
            raise ValueError(f"[{LABEL}] Timeline 寬高必須是 32–8192 間 32 的倍數。")
        return width, height
    w, h = snapshot["display_size"]
    if min(w, h) == short_edge and w % 32 == 0 and h % 32 == 0:
        return w, h
    long_edge = short_edge * 16 // 9 // 32 * 32
    return (long_edge, short_edge) if w >= h else (short_edge, long_edge)


def reference_pins(segment):
    """Only accept official Remake's all-pinned or all-unpinned reference layout."""
    m = segment["materials"]
    pictures = m["pictures"]
    by_ref = {p["ref"]: [] for p in pictures}
    if len(by_ref) != len(pictures):
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段：同一段請勿重複加入相同檔案作為多張參考圖。")
    for k in m["keyframes"]:
        if k["ref"] not in by_ref:
            raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段：關鍵幀也必須是該段的圖片參考素材。")
        by_ref[k["ref"]].append(k["frame_idx"])
    pins = [by_ref[p["ref"]] for p in pictures]
    if any(pins) and not all(len(p) == 1 for p in pins):
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段：每張參考圖都要有一個關鍵幀，或全部不設關鍵幀（官方 pins 規則）。")
    return ", ".join(str(p[0]) for p in pins) if any(pins) else ""


def validate_segment(segment):
    require_segment(segment, ("encode",), LABEL)
    n = segment["length"]
    if segment["effective_grid"] != H3_GRID or tuple(segment["fps"]) != H3_FPS:
        raise ValueError(f"[{LABEL}] Edit Timeline 必須使用 generate、24 fps、MiniMax H3。")
    if not 90 <= n <= 345 or (n - 5) % 17:
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段必須是 90–345 幀且為 17n+5，建議先用 107 或 124 幀。")
    m = segment["materials"]
    if len(m["videos"]) != 1 or m["videos"][0]["effective_frames"] != n:
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段需要一支完整覆蓋該段的參考影片。請把來源影片加進素材庫，設為跟隨時間軸的參考。")
    if not 1 <= len(m["pictures"]) <= 4:
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段需要 1–4 張改繪後的圖片參考。")
    if any(m[k] for k in ("audios", "guides", "locks")):
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段：Remake 使用來源影片音軌；目前不支援額外參考音訊、guide 或獨立鎖音。圖片關鍵幀可繼續使用。")
    if re.search(r"<Audio [0-9]+>", segment["prompt"]):
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段：Remake 的原音不是 <Audio N> 參考；請在 overall_soundscape 描述原音。")
    if any(section not in segment["prompt"] for section in PROMPT_SECTIONS):
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段：請使用官方六段式 prompt：{', '.join(PROMPT_SECTIONS)}。每段時間戳從 00:00 開始。")
    return reference_pins(segment)


def encode_remake(segment, clip, vae, audio_vae, short_edge, keep_source_audio, width=0, height=0):
    """Decode bounded material slices, then delegate all conditioning to RemakeConditioning."""
    pins = validate_segment(segment)
    cls = remake_nodes()["RemakeConditioning"]
    source = segment["materials"]["videos"][0]
    width, height = canvas(source["snapshot"], short_edge, width, height)
    pixels = segment["length"] * width * height
    _check_decode(pixels * 3, LABEL, "來源影片的 rgb24 ", "請縮短分段或使用 480。")
    _check_decode_ram(pixels * 3 * 4, width, height)
    snap = source["snapshot"]
    # Resample on the source's absolute 24 fps clock before selecting a window. Seeking and then restarting
    # an fps filter for each segment can shift non-24-fps / VFR footage at the seams.
    valid = min(segment["length"], snap.get("remake_valid_frames", source["source_start"] + segment["length"]) - source["source_start"])
    if valid <= 0:
        raise ValueError(f"[{LABEL}] 來源沒有可用畫面。")
    frames = decode_video_window(material_path(source, LABEL), source["source_start"], valid, H3_FPS,
                                 material_origin(snap, "source_start_us"), (width, height),
                                 [scale_filter("fill", width, height)], snap["video_ordinal"], LABEL)
    if frames.shape[0] != valid:
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段來源影片不足 {valid} 幀。")
    if valid < segment["length"]:
        import torch
        frames = torch.cat((frames, frames[-1:].expand(segment["length"] - valid, -1, -1, -1)), dim=0)
    audio = None
    if keep_source_audio and source["snapshot"].get("has_audio"):
        # Keep all PCM samples here: RemakeConditioning pads the end to the audio VAE's 800-sample grid itself.
        audio = load_audio_slice(source, source["source_start"], valid, H3_FPS, LABEL,
                                 sample_rate=32000, multiple=1, origin_key="source_start_us")
        if valid < segment["length"]:
            import torch.nn.functional as F
            keep = audio_sample_count(source["source_start"], valid, 32000, H3_FPS)
            total = audio_sample_count(source["source_start"], segment["length"], 32000, H3_FPS)
            audio = {**audio, "waveform": F.pad(audio["waveform"][..., :keep], (0, total - keep))}
    refs = {f"ref_{i + 1}": load_image(p, LABEL) for i, p in enumerate(segment["materials"]["pictures"])}
    fn = getattr(cls(), cls.FUNCTION)
    return fn(clip=clip, vae=vae, audio_vae=audio_vae, source=frames, prompt=segment["prompt"],
              pins=pins, frames=segment["length"], short_edge=min(width, height), audio=audio, **refs)
