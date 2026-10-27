"""Latent tail-carry 引擎（純 tensor 運算，不載入模型、不取樣）。

LATENT["samples"] 可能是一般 5D tensor，或 core 的 NestedTensor（H3 / LTX-2 的 video + audio）。
兩者都有 `.is_nested`（torch.Tensor 原生屬性 / core wrapper 的旗標），所以不需要 import comfy
就能分辨——這也讓 Windows 測試機能用假容器跑。
"""


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
