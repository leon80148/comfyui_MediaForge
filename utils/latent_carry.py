"""Latent tail-carry 引擎（純 tensor 運算，不載入模型、不取樣）。

LATENT["samples"] 可能是一般 5D tensor，或 core 的 NestedTensor（H3 / LTX-2 的 video + audio）。
兩者都有 `.is_nested`（torch.Tensor 原生屬性 / core wrapper 的旗標），所以不需要 import comfy
就能分辨——這也讓 Windows 測試機能用假容器跑。
"""
import torch

from .timeline_messages import TimelineError, format_message
from .timeline_plan import H3_GRID, h3_audio_tokens

EXTEND_LABEL = "Extend Latent"


def split_streams(samples):
    """→ (streams list, is_nested)。stream 0 一律是 video。"""
    if samples.is_nested:
        return list(samples.unbind()), True
    return [samples], False


def latent_space_id(samples):
    """結構簽章（stream 數、各 stream 的 channel 與空間尺寸）；只能擋明顯不符，不代表同一模型。"""
    streams, nested = split_streams(samples)
    video = streams[0]
    parts = [f"nested{len(streams)}" if nested else "plain", f"v{video.shape[1]}x{video.shape[-2]}x{video.shape[-1]}"]
    for audio in streams[1:]:
        parts.append("a" + "x".join(str(d) for d in audio.shape[1:-1]))
    return "|".join(parts)


def extract_carry_tail(samples, segment):
    """下一段要 carry 的 tail（CPU clone），由 MF_AppendSegment 寫進 state。

    先切片再 clone：小 view 會留住整段 storage，快取住多個 state 時記憶體會一路累積。
    """
    spec = segment["carry_out"]
    if spec is None:
        return None
    streams, nested = split_streams(samples)
    video = streams[0]
    k = spec["video_tokens"]
    tail = {"video": video[:, :, video.shape[2] - k:].detach().cpu().clone().contiguous(), "audio": None}
    if "audio_k0" in spec and nested and len(streams) == 2:
        tail["audio"] = streams[1][..., spec["audio_k0"]:].detach().cpu().clone().contiguous()
    return tail


def carry_drift(latent, carry, streams_carried):
    """取樣後的 latent 在 noise_mask == 0（完全保留）的續接位置，與 carry tail 的最大絕對差。

    兩段式 KSamplerAdvanced 的第二段以第一段帶殘留雜訊的輸出當保留內容，續接區會被雜訊取代——
    MF_AppendSegment 用這個值擋下。latent 沒有 noise_mask 時無從判斷，回傳 None。
    """
    if latent.get("noise_mask") is None:
        return None
    streams, nested = split_streams(latent["samples"])
    masks = _existing_masks(latent, nested, len(streams))
    drift = 0.0
    for name, index, time_dim in (("video", 0, 2), ("audio", 1, -1)):
        tail = carry.get(name)
        if name not in streams_carried or tail is None or index >= len(streams) or masks[index] is None:
            continue
        k = tail.shape[time_dim]
        head = streams[index].narrow(time_dim, 0, k).float()
        keep = _checked_mask(masks[index], streams[index].shape, name).narrow(time_dim, 0, k) == 0
        keep = keep.to(head.device).expand(head.shape)
        if keep.any():
            drift = max(drift, float((head - tail.to(head.device, torch.float32)).abs()[keep].max()))
    return drift


def _fail(message):
    raise ValueError(f"[{EXTEND_LABEL}] {message}")


def mask_weights(mode, tokens, taper):
    """K 個 carried token 的 noise_mask（0 = 保留、1 = 照常去噪），float32；呼叫端已驗 0 ≤ taper ≤ tokens。"""
    weights = torch.zeros(tokens, dtype=torch.float32)
    if mode == "taper" and taper:
        weights[tokens - taper:] = torch.arange(1, taper + 1, dtype=torch.float32) / (taper + 1)
    elif mode == "linear" and tokens > 1:
        weights = torch.arange(tokens, dtype=torch.float32) / (tokens - 1)
    return weights


def _existing_masks(latent, nested, count):
    mask = latent.get("noise_mask")
    if mask is None:
        return [None] * count
    if not mask.is_nested:
        # 普通 mask 在 nested latent 上只代表 video，其餘 stream 視為全 1（同 core sampler）
        return [mask] + [None] * (count - 1)
    masks = list(mask.unbind())
    if not nested or len(masks) > count:
        _fail(f"既有 noise_mask 是 {len(masks)} 個 stream 的 nested mask，與 target_latent 的 stream 結構不符。"
              "請檢查上游設定 noise_mask 的節點。")
    return masks + [None] * (count - len(masks))


def _checked_mask(mask, shape, name):
    # core 的 reshape_mask 會把任何形狀內插到 latent 尺寸，時間長度錯了也不會報錯——所以這裡
    # 只收「每維為 1 或等於 latent」（可少 channel 維），其餘一律拒絕，也不 clamp 掩蓋非法值
    m = mask.unsqueeze(1) if mask.ndim == len(shape) - 1 else mask
    if m.ndim != len(shape) or any(d not in (1, s) for d, s in zip(m.shape, shape)):
        _fail(f"既有 noise_mask（{name}）形狀 {tuple(mask.shape)} 需要內插才能對齊 latent {tuple(shape)}；"
              "每一維必須是 1 或與 latent 相同。")
    m = m.float()
    if not bool(torch.isfinite(m).all()) or m.min() < 0 or m.max() > 1:
        _fail(f"既有 noise_mask（{name}）含 NaN / Inf 或超出 [0, 1] 的值。請檢查上游設定 noise_mask 的節點。")
    return m


def _carry_mask(stream, time_dim, weights, existing, name):
    """→ (新 mask, 視窗內與既有 mask 衝突的 token 數)；視窗 = time 維的前 K 個 token。"""
    k = weights.numel()
    compact = [1] * stream.ndim
    compact[time_dim] = stream.shape[time_dim]
    if existing is None:
        # 精簡形狀即可：core 取樣前會用 reshape_mask 展開成完整 latent 形狀
        mask, conflicts = torch.ones(compact, dtype=torch.float32, device=stream.device), 0
    else:
        mask = _checked_mask(existing, stream.shape, name).to(stream.device)
        mask = mask.expand(torch.broadcast_shapes(mask.shape, tuple(compact))).clone()
        hit = mask.narrow(time_dim, 0, k) < 1
        conflicts = int(hit.movedim(time_dim, 0).reshape(k, -1).any(dim=1).sum())
    window = [1] * stream.ndim
    window[time_dim] = k
    mask.narrow(time_dim, 0, k).copy_(weights.view(window))
    return mask, conflicts


def _write_head(stream, time_dim, tail):
    out = stream.clone()
    out.narrow(time_dim, 0, tail.shape[time_dim]).copy_(tail)  # copy_ 會轉成 target stream 的 dtype / device
    return out


def _layout(space_id):
    # 只比第一段（plain / nestedN）：LTX-2 audio 的 time 軸不在最後一維、會被算進簽章，
    # 段長不同時整串必然不同；video 的 C/H/W 改由 tail 形狀逐維比對
    return space_id.split("|", 1)[0]


def _source_tails(spec, state, previous_latent, target_samples, carry_audio):
    """→ {stream: (tail view, 在來源中的起點)}；tail 形狀由呼叫端比對。"""
    if state is not None:
        src = _layout(state["carry"]["latent_space_id"])
    else:
        src = _layout(latent_space_id(previous_latent["samples"]))
    tgt = _layout(latent_space_id(target_samples))
    if src != tgt:
        _fail(f"續接來源的 stream 結構（{src}）與 target_latent（{tgt}）不同。請確認前後段使用同一模型。")
    if state is not None:
        return {"video": (state["carry"]["video"], 0), "audio": (state["carry"]["audio"], 0)}
    streams, _ = split_streams(previous_latent["samples"])
    t, k = streams[0].shape[2], spec["video_tokens"]
    tails = {"video": (streams[0][:, :, t - k:], t - k)}
    if carry_audio:
        tails["audio"] = (streams[1][..., spec["audio_k0"]:], spec["audio_k0"])
    return tails


def _provenance(segment, state, previous_latent):
    """一致性提示，不是來源證明：consistent / absent；任一欄不符直接 E_PROVENANCE。"""
    if state is not None:
        names = ("execution_id", "producer_index", "carry_id")
        got = (state["execution_id"], state["carry"]["producer_index"], state["carry"]["carry_id"])
    else:
        marker = previous_latent.get("mf_carry")
        if marker is None:
            return "absent"
        names = ("execution_id", "segment_index", "carry_spec_id")
        got = tuple(marker.get(n) for n in names)
    want = (segment["execution_id"], segment["index"] - 1, segment["carry_in_id"])
    for name, a, b in zip(names, got, want):
        if a != b:
            raise TimelineError(EXTEND_LABEL, "E_PROVENANCE", field=name)
    return "consistent"


def extend_latent(target_latent, segment, mask="hard", carry_audio=False, state=None, previous_latent=None,
                  video_taper_tokens=2, audio_taper_tokens=2, experimental_causal=False, on_conflict="error"):
    """MF_ExtendLatent 的引擎 → (新 LATENT dict, report dict)。不修改任何輸入。"""
    if not isinstance(segment, dict) or segment.get("type") != "MF_SEGMENT" or segment.get("version") != 1:
        _fail("segment 必須來自 Select Segment。")
    samples = target_latent["samples"]
    streams, nested = split_streams(samples)
    video = streams[0]
    if video.ndim != 5:
        _fail(f"target_latent 的 video 必須是 5D [B,C,T,H,W]，收到 {tuple(video.shape)}。")
    if segment["expected_tokens"] is not None and video.shape[2] != segment["expected_tokens"]:
        raise TimelineError(EXTEND_LABEL, "E_TOKENS", expected=segment["expected_tokens"], actual=video.shape[2])
    if any(s.shape[0] != 1 for s in streams):
        _fail("只支援 batch 1；請把上游 latent 的 batch_size 設為 1。")
    grid = segment["effective_grid"]
    if grid == H3_GRID and len(streams) > 1:
        audio_tokens = h3_audio_tokens(segment["length"])
        if streams[1].shape[-1] != audio_tokens:
            raise TimelineError(EXTEND_LABEL, "E_TOKENS", expected=audio_tokens, actual=streams[1].shape[-1])
    if carry_audio and not (grid == H3_GRID and len(streams) == 2):
        raise TimelineError(EXTEND_LABEL, "E_AUDIO_CARRY")
    if state is not None and previous_latent is not None:
        _fail("state 與 previous_latent 只能擇一，請移除其中一條連線。")

    spec = segment["carry_in"]
    out = dict(target_latent)
    carried, details, source, provenance = [], {}, None, None
    if spec is not None:
        source = "state" if state is not None else "previous_latent" if previous_latent is not None else None
        if source is None or (state is not None and state["carry"] is None):
            raise TimelineError(EXTEND_LABEL, "E_CARRY_SOURCE")
        if grid.startswith("causal") and not experimental_causal:
            raise TimelineError(EXTEND_LABEL, "E_CAUSAL")
        provenance = _provenance(segment, state, previous_latent)
        if provenance == "absent":
            print(format_message(EXTEND_LABEL, "W_PROVENANCE"))
        tails = _source_tails(spec, state, previous_latent, samples, carry_audio)

        jobs = [("video", 0, 2, spec["video_tokens"], video_taper_tokens)]
        if carry_audio:
            jobs.append(("audio", 1, streams[1].ndim - 1, spec["audio_tokens"], audio_taper_tokens))
        masks = _existing_masks(target_latent, nested, len(streams))
        new_streams, new_masks = list(streams), list(masks)
        for name, i, dim, k, taper in jobs:
            if mask == "taper" and not 0 <= taper <= k:
                _fail(f"{name}_taper_tokens 必須在 0…{k} 之間（本段 {name} 續接 {k} 個 token），收到 {taper}。")
            tail, offset = tails[name]
            stream = streams[i]
            want = tuple(stream.shape[:dim]) + (k,) + tuple(stream.shape[dim + 1:])
            got = None if tail is None else tuple(tail.shape)
            if got != want:
                _fail(f"續接來源的 {name} 形狀 {got} 與本段需要的 {want} 不符。請確認前後段使用同一模型、解析度與 grid。")
            weights = mask_weights(mask, k, taper)
            new_masks[i], conflicts = _carry_mask(stream, dim, weights, masks[i], name)
            if conflicts and on_conflict == "error":
                raise TimelineError(EXTEND_LABEL, "E_MASK_CONFLICT")
            new_streams[i] = _write_head(stream, dim, tail)
            # H3 先把 mask 量化成 ceil(m·256)/256 再換算逐 token timestep；其他模型直接用原值
            effective = torch.ceil(weights * 256.0) / 256.0 if grid == H3_GRID else weights
            details[name] = {"source_range": [offset, offset + k], "target_range": [0, k], "tokens": k,
                             "taper_tokens": taper if mask == "taper" else None,
                             "requested": weights.tolist(), "effective": effective.tolist(), "conflicts": conflicts}
            carried.append(name)
        if carry_audio:
            details["audio"].update(k0=spec["audio_k0"], phase=spec["audio_phase"])
        new_masks = [m if m is not None else torch.ones(s.shape, dtype=torch.float32, device=s.device)
                     for s, m in zip(streams, new_masks)]
        out["samples"] = type(samples)(new_streams) if nested else new_streams[0]
        out["noise_mask"] = type(samples)(new_masks) if nested else new_masks[0]

    out["mf_carry"] = {"execution_id": segment["execution_id"], "segment_index": segment["index"],
                       "carry_spec_id": segment["carry_out_id"], "streams_carried": list(carried)}
    changed = ("samples", "noise_mask", "mf_carry") if carried else ("mf_carry",)
    report = {
        "version": 1,
        "plan_id": segment["plan_id"], "execution_id": segment["execution_id"],
        "session": state["session"] if state is not None else None,
        "segment_index": segment["index"],
        "carry_source": source, "provenance": provenance,
        "latent_space_id": latent_space_id(samples),
        "streams": [{"shape": list(s.shape), "dtype": str(s.dtype).removeprefix("torch."), "device": str(s.device)}
                    for s in streams],
        "mask": mask, "on_conflict": on_conflict,
        "streams_carried": list(carried), "carry": details,
        "metadata": {"kept": sorted(key for key in target_latent if key not in changed),
                     "replaced": [key for key in changed if key in target_latent],
                     "added": [key for key in changed if key not in target_latent]},
        "experimental": bool(carried) and grid != H3_GRID,
        "conditioning_check": "unavailable",
        "warnings": ["W_PROVENANCE"] if provenance == "absent" else [],
    }
    return out, report
