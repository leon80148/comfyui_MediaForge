"""MF_AppendSegment — 逐段取得畫面 / 音訊，去重疊、做 crossfade 後落盤成 spill part。

畫面二擇一：frames（IMAGE），或 latent + vae 在這裡解碼；音訊二擇一：audio（AUDIO），或
audio_vae 解同一個 AV latent 的音訊 stream。latent 同時是下一段 carry tail 的來源。輸出的
MF_SEGMENT_STATE 是新 dict，輸入的 state 不會被修改。
"""
from ..utils import segment_spill
from ..utils.ffmpeg import ensure_ffmpeg
from ..utils.latent_carry import carry_drift, extract_carry_tail, latent_space_id, split_streams
from ..utils.timeline_messages import TimelineError, format_message

LABEL = segment_spill.APPEND_LABEL
# 正確保留時差在 1e-6 級（float32 累積誤差）；兩段式取樣把雜訊留在續接區時是數十
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
    expected = {"execution_id": segment["execution_id"], "segment_index": segment["index"],
                "carry_spec_id": segment["carry_out_id"]}
    bad = [k for k, v in expected.items() if not isinstance(marker, dict) or marker.get(k) != v]
    if bad:
        raise TimelineError(LABEL, "E_PROVENANCE", field=", ".join(bad))
    return "consistent", marker


def _frames_input(frames, segment):
    if frames.ndim == 5:
        if frames.shape[0] != 1:
            raise ValueError(f"[{LABEL}] frames 的 batch 必須是 1，收到 {frames.shape[0]}。請一次只接一段的畫面。")
        frames = frames[0]
    if frames.ndim != 4 or frames.shape[-1] != 3:
        raise ValueError(f"[{LABEL}] frames 必須是 [T,H,W,3] RGB，收到 {tuple(frames.shape)}。請接 RGB 的 IMAGE。")
    if frames.shape[0] != segment["length"]:
        raise ValueError(f"[{LABEL}] 第 {segment['index'] + 1} 段需要 {segment['length']} 幀，frames 有 {frames.shape[0]} 幀。"
                         "請確認畫面來源與時間軸段長一致。")
    return frames


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
        raise ValueError(f"[{LABEL}] audio_vae 需要 video + audio 兩個 stream 的 AV latent。請改接 audio（AUDIO）。")
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
                "vae": ("VAE", {"tooltip": "解碼 latent 畫面（沒接 frames 時必接）。"}),
                "audio_vae": ("VAE", {"tooltip": "解碼同一個 AV latent 的音訊；與 audio 擇一。"}),
                "frames": ("IMAGE", {"tooltip": "直接提供本段畫面（不經 VAE）；與 vae / audio_vae 互斥。"}),
                "audio": ("AUDIO", {"tooltip": "本段音訊；與 audio_vae 擇一。"}),
            },
        }

    RETURN_TYPES = ("MF_SEGMENT_STATE",)
    RETURN_NAMES = ("state",)
    FUNCTION = "append"
    CATEGORY = "MediaForge/Video"

    def append(self, segment, state=None, latent=None, vae=None, audio_vae=None, frames=None, audio=None):
        if not isinstance(segment, dict) or segment.get("type") != "MF_SEGMENT":
            raise ValueError(f"[{LABEL}] segment 必須來自 Select Segment，請連接它的 segment 輸出。")
        if frames is not None:
            if vae is not None or audio_vae is not None:
                raise ValueError(f"[{LABEL}] 已接 frames 時畫面不經 VAE；請拔掉 vae / audio_vae（latent 可保留作續接來源）。")
        elif latent is None or vae is None:
            raise ValueError(f"[{LABEL}] 沒接 frames 時，請同時連接 latent 與 vae。")
        if audio is not None and audio_vae is not None:
            raise ValueError(f"[{LABEL}] audio 與 audio_vae 只能擇一，請拔掉其中一個。")
        if segment["carry_out"] is not None and latent is None:
            raise ValueError(f"[{LABEL}] 下一段需要續接資料，請連接本段的 latent。")
        if not ensure_ffmpeg():
            raise RuntimeError(f"[{LABEL}] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        segment_spill.check_sequence(state, segment)
        provenance, marker = (None, None) if latent is None else _provenance(latent, segment)
        if provenance == "consistent" and segment["carry_in"] is not None:
            drift = carry_drift(latent, state["carry"], marker, LABEL)
            if drift is not None and drift > CARRY_DRIFT_ATOL:
                raise TimelineError(LABEL, "E_CARRY_DRIFT", drift=f"{drift:.3g}")
        images = _frames_input(frames, segment) if frames is not None else _decode_video(vae, latent, segment)
        wave = None
        if audio is not None:
            wave = segment_spill.audio_input(audio, LABEL)
        elif audio_vae is not None:
            wave = _decode_audio(audio_vae, latent)
        carry = None
        if segment["carry_out"] is not None:
            samples = latent["samples"]
            carry = {**extract_carry_tail(samples, segment), "producer_index": segment["index"],
                     "carry_id": segment["carry_out_id"], "latent_space_id": latent_space_id(samples)}
        new_state = segment_spill.append_segment(
            state, segment, images, wave,
            curve=_audio_curve(segment["seam_in"], audio_vae is not None, provenance, marker),
            carry=carry, check_interrupt=_check_interrupt)
        if frames is None:
            import comfy.model_management
            comfy.model_management.soft_empty_cache()
        return (new_state,)


NODE_CLASS_MAPPINGS = {"MF_AppendSegment": MF_AppendSegment}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_AppendSegment": "➕ Append Segment"}
