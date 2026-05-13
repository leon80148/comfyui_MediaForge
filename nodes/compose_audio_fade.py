"""MF_ComposeAudioFade — append afade op (in/out) into MF_COMPOSE_AUDIO_OPS chain。

`direction`:'in' (從靜音淡入到全音) 或 'out' (從全音淡出到靜音)
`start_sec`:fade 開始的時間點 — in 通常 0、out 通常 video_duration - duration_sec
`duration_sec`:fade 持續秒數
`curve`:fade 曲線形狀 (FADE_CURVES) — 預設 'tri' 線性、`qsin` 自然 S-curve
"""
from ..utils.audio_mix import FADE_CURVES


class MF_ComposeAudioFade:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "direction": (["in", "out"], {"default": "in"}),
                "start_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.1}),
                "duration_sec": ("FLOAT", {"default": 2.0, "min": 0.1, "max": 60.0, "step": 0.1}),
                "curve": (FADE_CURVES, {"default": "tri"}),
            },
            "optional": {
                "audio_ops": ("MF_COMPOSE_AUDIO_OPS",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE_AUDIO_OPS",)
    RETURN_NAMES = ("audio_ops",)
    FUNCTION = "add"
    CATEGORY = "MediaForge/Compose"

    def add(self, direction, start_sec, duration_sec, curve, audio_ops=None):
        params = {
            "direction": direction,
            "start_sec": float(start_sec),
            "duration_sec": float(duration_sec),
            "curve": curve,
        }
        ops = list(audio_ops) if audio_ops else []
        ops.append({"type": "afade", "params": params})
        return (ops,)


NODE_CLASS_MAPPINGS = {"MF_ComposeAudioFade": MF_ComposeAudioFade}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeAudioFade": "🌅 Compose Audio Fade"}
