"""MF_ExtendLatent — 把前一段 latent 的尾端寫進本段開頭，並用 noise_mask 指定保留程度。

只做 tensor 運算（utils/latent_carry.py）：不載入模型、不取樣，sampler 由使用者自己接。
續接來源二擇一：loop 用 ➕ Append Segment 的 state，線性鏈用前一段取樣後的 previous_latent。
"""
import json

from ..utils import segment_spill
from ..utils.latent_carry import EXTEND_LABEL, extend_latent


class MF_ExtendLatent:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "target_latent": ("LATENT", {"tooltip": "本段的空 latent；形狀需已是最終尺寸，時間長度等於本段 token 數。"}),
                "segment": ("MF_SEGMENT",),
                "mask": (["hard", "taper", "linear"], {
                    "default": "hard",
                    "tooltip": "hard 保留全部；taper 放寬尾端；linear 逐步放寬（最後一個 carried token 可完全去噪；只有 1 個 token 時例外保留）。",
                }),
                "carry_audio": ("BOOLEAN", {"default": False, "tooltip": "僅 H3 音訊續接；關閉時不改音訊 latent／遮罩。"}),
            },
            "optional": {
                "state": ("MF_SEGMENT_STATE", {"tooltip": "前一段 ➕ Append Segment 的 state（loop 用）；與 previous_latent 擇一。"}),
                "previous_latent": ("LATENT", {"tooltip": "前一段取樣後的 latent（線性鏈用）；與 state 擇一。"}),
                "video_taper_tokens": ("INT", {
                    "default": 2, "min": 0, "max": 1000000, "step": 1,
                    "tooltip": "taper 時 video 續接區尾端放寬的 token 數，不得超過續接 token 數。",
                }),
                "audio_taper_tokens": ("INT", {
                    "default": 2, "min": 0, "max": 1000000, "step": 1,
                    "tooltip": "taper 時 audio 續接區尾端放寬的 token 數，不得超過續接 token 數。",
                }),
                "experimental_causal": ("BOOLEAN", {"default": False, "tooltip": "啟用 Wan／LTX 等因果 grid 續接；接縫可能不連續。"}),
                "on_conflict": (["error", "carry_wins"], {
                    "default": "error",
                    "tooltip": "error 停止；carry_wins 覆寫續接區的 samples 與 mask。",
                }),
            },
        }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "report")
    FUNCTION = "extend"
    CATEGORY = "MediaForge/AI"

    def extend(self, target_latent, segment, mask, carry_audio, state=None, previous_latent=None,
               video_taper_tokens=2, audio_taper_tokens=2, experimental_causal=False, on_conflict="error"):
        if state is not None:
            segment_spill.check_state(state, EXTEND_LABEL)  # an old saved state gets the re-run message, not E_PROVENANCE
        latent, report = extend_latent(target_latent, segment, mask, carry_audio, state, previous_latent,
                                       video_taper_tokens, audio_taper_tokens, experimental_causal, on_conflict)
        return (latent, json.dumps(report, ensure_ascii=False))


NODE_CLASS_MAPPINGS = {"MF_ExtendLatent": MF_ExtendLatent}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ExtendLatent": "🧬 Extend Latent"}
