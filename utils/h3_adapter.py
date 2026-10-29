"""core MiniMax H3 節點的薄 adapter，與鎖定原音的 latent 寫入（policy ctx_v1）。

只透過 nodes.NODE_CLASS_MAPPINGS 取得 core 類別、呼叫它們的 execute——不 import core 的私有 helper；
signature 或回傳形狀不符時 E_H3_NODES。素材 IMAGE / AUDIO 只在一次 encode 內存在，不進快取。
"""
import inspect
import math
from fractions import Fraction

import torch

from .timeline_materials import LOCK_POLICY
from .timeline_media import (decode_audio, load_audio_slice, load_image, load_video_slice, material_origin,
                             material_path, scale_filter)
from .timeline_messages import TimelineError
from .timeline_plan import H3_GRID, require_segment

LABEL = "Encode H3 Segment"
REF_NODE = "MiniMaxH3ReferenceToVideo"
GUIDE_NODE = "MiniMaxH3AddGuide"
_REQUIRED = {
    REF_NODE: {"clip", "prompt", "width", "height", "length", "ref_image_size", "vae", "audio_vae",
               "ref_images", "ref_videos", "ref_video_audios", "ref_audios"},
    GUIDE_NODE: {"positive", "latent", "frame_idx", "vae", "audio_vae", "image", "audio"},
}
H3_BASE_SHORT_EDGE = 768  # core adapt_canvas 的短邊；ref video 在這裡先縮到這個量級，core 再對齊到 32
TOKENS_PER_SECOND = 40
# encoder 在 causal attention 之前有雙側 padding 的卷積：左右都需要上下文，有限上下文是近似
LOCK_LEFT_TOKENS = 160
LOCK_RIGHT_TOKENS = 16


def core_nodes():
    import nodes
    out, bad = {}, []
    for name, params in _REQUIRED.items():
        cls = nodes.NODE_CLASS_MAPPINGS.get(name)
        fn = getattr(cls, "execute", None)
        if fn is None or not params <= set(inspect.signature(fn).parameters):
            bad.append(name)
        out[name] = cls
    if bad:
        raise TimelineError(LABEL, "E_H3_NODES", nodes=", ".join(bad))
    return out


def _result(output, count, name):
    result = getattr(output, "result", output)
    if not isinstance(result, tuple) or len(result) != count:
        raise TimelineError(LABEL, "E_H3_NODES", nodes=name)
    return result


def _ref_video_size(display_size, short_edge):
    """解碼時就縮小 ref video：0 = 縮到 H3 canvas 的短邊量級（core 仍會再對齊）；否則短邊 ≤ short_edge、32 對齊。"""
    w, h = display_size
    target = short_edge or H3_BASE_SHORT_EDGE
    if min(w, h) <= target:
        return None, None
    f = target / min(w, h)
    step = 32 if short_edge else 2
    size = (max(step, round(w * f / step) * step), max(step, round(h * f / step) * step))
    return scale_filter("stretch", *size), size


def needs_audio_vae(materials):
    return bool(materials["audios"] or materials["locks"]
                or any(v["soundtrack"] for v in materials["videos"])
                or any(g["soundtrack"] for g in materials["guides"]))


def write_locks(latent, locks, fps, audio_vae):
    """把鎖音原聲編碼寫進 AV latent 的 audio stream，該範圍 noise_mask = 0（§7.1）。"""
    samples = latent["samples"]
    video, audio = samples.unbind()
    audio = audio.clone()
    mask = torch.ones_like(audio, dtype=torch.float32)
    rate = getattr(audio_vae, "audio_sample_rate", 32000)
    per_token = rate // TOKENS_PER_SECOND
    num, den = fps
    tokens, targets = [], []
    for lock in locks:
        k0, k1 = lock["tokens"]
        snap = lock["snapshot"]
        # 以 audio VAE 取樣率的 sample 座標定義上下文（§7.1 ctx_v1）：A = token K0 在素材中的起點
        offset = Fraction((lock["source_start"] - lock["frame_idx"]) * den, num) + Fraction(k0, TOKENS_PER_SECOND)
        a = math.floor(offset * rate + Fraction(1, 2))
        n = snap["duration_us"] * rate // 1_000_000
        k = k1 - k0
        left = min(LOCK_LEFT_TOKENS, a // per_token)
        right = min(LOCK_RIGHT_TOKENS, max(0, (n - a - per_token * k) // per_token))
        count = (k + left + right) * per_token  # 800 的整數倍：core 公共 VAE.encode 不會再置中裁切
        origin = material_origin(snap, "audio_start_us")
        start = Fraction(a - per_token * left, rate)
        wave = decode_audio(material_path(lock, LABEL), origin + start, origin, count, rate, 2, LABEL)
        z = audio_vae.encode(wave.movedim(1, -1))
        if z.ndim != audio.ndim or z.shape[-1] != count // per_token or tuple(z.shape[1:-1]) != tuple(audio.shape[1:-1]):
            raise TimelineError(LABEL, "E_H3_NODES", nodes=f"audio VAE（輸出形狀 {tuple(z.shape)}）")
        audio[..., k0:k1] = z[..., left:left + k1 - k0].to(device=audio.device, dtype=audio.dtype)
        mask[..., k0:k1] = 0.0
        tokens.append([k0, k1])
        targets.append(audio[..., k0:k1].clone())  # 獨立 storage：取樣器回傳新 tensor 時才比得出漂移
    out = dict(latent)
    out["samples"] = type(samples)((video, audio))
    out["noise_mask"] = type(samples)((torch.ones_like(video, dtype=torch.float32), mask))
    out["mf_lock"] = {"policy": LOCK_POLICY, "tokens": tokens, "target": targets}
    return out


def encode_segment(segment, clip, vae, width, height, ref_image_size, audio_vae=None, ref_video_short_edge=0):
    """一段 H3 conditioning + 空 AV latent（含鎖音）；素材只在這裡載入、用完即放。"""
    require_segment(segment, ("encode",), LABEL)
    if segment["effective_grid"] != H3_GRID:
        raise ValueError(f"[{LABEL}] 只支援 H3（grid = {H3_GRID}）；其他模型請用 Load Segment Media 自己接 conditioning。")
    materials = segment["materials"]
    if audio_vae is None and needs_audio_vae(materials):
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段有音訊類素材（參考音訊 / 配對音軌 / 鎖音），請連接 audio_vae。")
    classes = core_nodes()
    fps = tuple(segment["fps"])
    number = segment["index"] + 1
    rate = getattr(audio_vae, "audio_sample_rate", 32000) if audio_vae is not None else None
    per_token = (rate or 32000) // TOKENS_PER_SECOND

    images = {f"ref_image_{k}": load_image(p, LABEL) for k, p in enumerate(materials["pictures"])}
    videos, soundtracks = {}, {}
    for k, v in enumerate(materials["videos"]):
        scale, size = _ref_video_size(v["snapshot"]["display_size"], ref_video_short_edge)
        videos[f"ref_video_{k}"] = load_video_slice(v, v["source_start"], v["effective_frames"], fps, LABEL, number,
                                                     scale, size)
        if v["soundtrack"]:
            # core 以 ref_video_N 的號碼配對 ref_video_audio_N
            soundtracks[f"ref_video_audio_{k}"] = load_audio_slice(v, v["source_start"], v["effective_frames"], fps,
                                                                   LABEL, rate, per_token, "source_start_us")
    audios = {f"ref_audio_{k}": load_audio_slice(a, a["source_start"], a["frames"], fps, LABEL, rate, per_token)
              for k, a in enumerate(materials["audios"])}
    positive, latent = _result(classes[REF_NODE].execute(
        clip=clip, prompt=segment["prompt"], width=width, height=height, length=segment["length"],
        ref_image_size=ref_image_size, vae=vae, audio_vae=audio_vae, ref_images=images, ref_videos=videos,
        ref_video_audios=soundtracks, ref_audios=audios), 2, REF_NODE)
    del images, videos, soundtracks, audios

    anchors = sorted([("keyframe", x) for x in materials["keyframes"]] + [("guide", x) for x in materials["guides"]],
                     key=lambda pair: pair[1]["frame_idx"])
    for role, item in anchors:
        if role == "keyframe":
            image, audio = load_image(item, LABEL), None
        else:
            image = load_video_slice(item, item["source_start"], item["frames"], fps, LABEL, number,
                                     scale_filter("fill", width, height), (width, height))
            audio = (load_audio_slice(item, item["source_start"], item["frames"], fps, LABEL, rate, per_token,
                                      "source_start_us") if item["soundtrack"] else None)
        positive = _result(classes[GUIDE_NODE].execute(
            positive=positive, latent=latent, frame_idx=item["frame_idx"], vae=vae, audio_vae=audio_vae,
            image=image, audio=audio), 1, GUIDE_NODE)[0]
        del image, audio

    locks = [x for x in materials["locks"] if x["tokens"][0] < x["tokens"][1]]
    if locks:
        latent = write_locks(latent, locks, fps, audio_vae)
    return positive, latent
