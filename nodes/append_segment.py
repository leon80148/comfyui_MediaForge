"""MF_AppendSegment — 逐段解碼畫面 / 音訊，去重疊、做 crossfade 後落盤成 spill part。

畫面由 latent + vae 在這裡解碼；有接 audio_vae 時，音訊解自同一個 AV latent 的音訊 stream。
latent 同時是下一段 carry tail 的來源。輸出的 MF_SEGMENT_STATE 是新 dict，輸入的 state 不會被修改。
"""
from ..utils import segment_spill
from ..utils.ffmpeg import ensure_ffmpeg
from ..utils.latent_carry import carry_drift, extract_carry_tail, latent_space_id, lock_drift, split_streams
from ..utils.timeline_messages import TimelineError, format_message
from ..utils.timeline_plan import require_segment

LABEL = segment_spill.APPEND_LABEL
# 正確保留時差在 1e-6 級（float32 累積誤差）；兩段式取樣把雜訊留在續接區時是數十。鎖音同一標準
CARRY_DRIFT_ATOL = 1e-3


def _check_interrupt():
    import comfy.model_management
    comfy.model_management.throw_exception_if_processing_interrupted()


def _provenance(latent, segment):
    """§4.8 三態：consistent / absent（警告後繼續）/ mismatch（raise）。這是一致性提示，不是來源證明。"""
    marker = latent.get("mf_carry")
    if marker is None:
        print(format_message(LABEL, "W_PROVENANCE"))
        return "absent", None
    expected = {"sampling_id": segment["sampling_id"], "segment_index": segment["index"]}
    bad = [k for k, v in expected.items() if not isinstance(marker, dict) or marker.get(k) != v]
    if bad:
        raise TimelineError(LABEL, "E_PROVENANCE", field=", ".join(bad))
    return "consistent", marker


def _decode_video(vae, latent, segment):
    video = split_streams(latent["samples"])[0][0]
    if video.ndim != 5 or video.shape[0] != 1:
        raise ValueError(f"[{LABEL}] video latent 必須是 batch=1 的 [1,C,T,H,W]，收到 {tuple(video.shape)}。請將 batch 設為 1。")
    ratio = vae.upscale_ratio
    up_h, up_w = (ratio[1], ratio[2]) if isinstance(ratio, (tuple, list)) else (ratio, ratio)
    # 整段 RGB float32 的估算；causal VAE 不能按時間切塊 decode，所以峰值至少是這麼多
    segment_spill.check_ram(segment["length"] * video.shape[-2] * up_h * video.shape[-1] * up_w * 3 * 4)
    images = vae.decode(video)
    if images.ndim == 5:
        if images.shape[0] != 1:
            raise ValueError(f"[{LABEL}] 解碼結果的 batch 必須是 1，收到 {images.shape[0]}。請將 batch 設為 1。")
        images = images[0]
    if images.ndim != 4 or images.shape[-1] != 3 or images.shape[0] != segment["length"]:
        raise ValueError(f"[{LABEL}] 解碼得到 {tuple(images.shape)}，預期 {segment['length']} 幀 RGB。請確認 vae 與 latent 屬於同一模型。")
    return images


def _decode_audio(audio_vae, latent):
    from comfy_extras.nodes_audio import vae_decode_audio
    streams, nested = split_streams(latent["samples"])
    if not nested or len(streams) != 2:
        raise ValueError(f"[{LABEL}] audio_vae 需要 video + audio 兩個 stream 的 AV latent。請確認模型是 MiniMax H3，或拔掉 audio_vae。")
    return segment_spill.audio_input(vae_decode_audio(audio_vae, {"samples": streams[1]}), LABEL)


def _audio_curve(seam, from_audio_vae, provenance, marker):
    if seam["kind"] != "crossfade":
        return None
    if seam["audio_curve"] != "auto":
        return seam["audio_curve"]
    # 同一個 latent 經 audio_vae 解碼、且 carry 了音訊：兩邊是同一段聲音的延續，用 linear 不會疊出 +3 dB
    if from_audio_vae and provenance == "consistent" and "audio" in marker.get("streams_carried", ()):
        return "linear"
    return "equal_power"


class MF_AppendSegment:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "segment": ("MF_SEGMENT",),
            },
            "optional": {
                "state": ("MF_SEGMENT_STATE", {"tooltip": "前一段的 state；第一段留空。"}),
                "latent": ("LATENT", {"tooltip": "本段取樣結果；也是下一段續接的來源。"}),
                "vae": ("VAE", {"tooltip": "解碼 latent 的畫面。"}),
                "audio_vae": ("VAE", {"tooltip": "解碼同一個 AV latent 的音訊；不接就沒有音訊。"}),
            },
        }

    RETURN_TYPES = ("MF_SEGMENT_STATE",)
    RETURN_NAMES = ("state",)
    FUNCTION = "append"
    CATEGORY = "MediaForge/Video"

    def append(self, segment, state=None, latent=None, vae=None, audio_vae=None):
        require_segment(segment, ("assemble",), LABEL)
        if latent is None or vae is None:
            raise ValueError(f"[{LABEL}] 請同時連接 latent 與 vae。")
        if not ensure_ffmpeg():
            raise RuntimeError(f"[{LABEL}] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        segment_spill.check_sequence(state, segment)
        provenance, marker = _provenance(latent, segment)
        if provenance == "consistent" and segment["carry_in"] is not None:
            drift = carry_drift(latent, state["carry"], marker, LABEL)
            if drift is not None and drift > CARRY_DRIFT_ATOL:
                raise TimelineError(LABEL, "E_CARRY_DRIFT", drift=f"{drift:.3g}")
        locked = lock_drift(latent, segment, LABEL)
        if locked is not None and locked > CARRY_DRIFT_ATOL:
            raise TimelineError(LABEL, "E_LOCK_DRIFT", reason=f"取樣後最大差 {locked:.3g}")
        images = _decode_video(vae, latent, segment)
        wave = _decode_audio(audio_vae, latent) if audio_vae is not None else None
        carry = None
        if segment["carry_out"] is not None:
            samples = latent["samples"]
            carry = {**extract_carry_tail(samples, segment), "producer_index": segment["index"],
                     "carry_id": segment["carry_out_id"], "sampling_id": segment["sampling_id"],
                     "latent_space_id": latent_space_id(samples)}
        new_state = segment_spill.append_segment(
            state, segment, images, wave,
            curve=_audio_curve(segment["seam_in"], audio_vae is not None, provenance, marker),
            carry=carry, check_interrupt=_check_interrupt, lock_drift=locked)
        import comfy.model_management
        comfy.model_management.soft_empty_cache()
        return (new_state,)


NODE_CLASS_MAPPINGS = {"MF_AppendSegment": MF_AppendSegment}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_AppendSegment": "➕ Append Segment"}
