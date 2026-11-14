"""MF_ExtendLatent — 把前一段 latent 的尾端寫進本段開頭，並用 noise_mask 指定保留程度。

只做 tensor 運算（utils/latent_carry.py）：不載入模型、不取樣。續接來源是前一段取樣後的 previous_latent。

內部節點：由 MF_SampleSegments 展開（graph expansion）時建立，不出現在節點選單。
"""
import json

from ..utils.latent_carry import extend_latent


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
                "previous_latent": ("LATENT", {"tooltip": "前一段取樣後的 latent；本段與前一段有重疊時必接。"}),
                "video_taper_tokens": ("INT", {
                    "default": 2, "min": 0, "max": 1000000, "step": 1,
                    "tooltip": "taper 時 video 續接區尾端放寬的 token 數，不得超過續接 token 數。",
                }),
                "audio_taper_tokens": ("INT", {
                    "default": 2, "min": 0, "max": 1000000, "step": 1,
                    "tooltip": "taper 時 audio 續接區尾端放寬的 token 數，不得超過續接 token 數。",
                }),
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
    DEPRECATED = True  # 不是要淘汰：core 只有這個旗標能讓節點不進搜尋與節點庫（另見 web/internal_nodes.js）

    def extend(self, target_latent, segment, mask, carry_audio, previous_latent=None,
               video_taper_tokens=2, audio_taper_tokens=2, on_conflict="error"):
        latent, report = extend_latent(
            target_latent, segment, mask=mask, carry_audio=carry_audio, previous_latent=previous_latent,
            video_taper_tokens=video_taper_tokens, audio_taper_tokens=audio_taper_tokens, on_conflict=on_conflict)
        return (latent, json.dumps(report, ensure_ascii=False))


NODE_CLASS_MAPPINGS = {"MF_ExtendLatent": MF_ExtendLatent}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ExtendLatent": "🧬 Extend Latent"}
