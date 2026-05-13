"""MF_ComposeVolume — append volume op into MF_COMPOSE_AUDIO_OPS chain。

Scale 語意:
  0.0 = mute
  0.5 = 半音量
  1.0 = 原音 (passthrough — 跳過此 op 也一樣)
  2.0 = 2× boost (注意 clipping 風險)
"""


class MF_ComposeVolume:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "scale": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05}),
            },
            "optional": {
                "audio_ops": ("MF_COMPOSE_AUDIO_OPS",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE_AUDIO_OPS",)
    RETURN_NAMES = ("audio_ops",)
    FUNCTION = "add"
    CATEGORY = "MediaForge/Compose"

    def add(self, scale, audio_ops=None):
        ops = list(audio_ops) if audio_ops else []
        ops.append({
            "type": "volume",
            "params": {"scale": float(scale)},
        })
        return (ops,)


NODE_CLASS_MAPPINGS = {"MF_ComposeVolume": MF_ComposeVolume}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeVolume": "🔊 Compose Volume"}
