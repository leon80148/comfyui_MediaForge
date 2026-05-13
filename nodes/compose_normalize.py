"""MF_ComposeNormalize — append loudnorm op into MF_COMPOSE_AUDIO_OPS chain。

EBU R128 / streaming-grade 響度標準化(單 pass)。

Streaming 平台目標 LUFS 慣例:
- YouTube / Spotify (音樂): -14 LUFS
- Apple Podcasts / Spotify spoken-word: -16 LUFS (預設)
- 廣電 broadcast EBU R128: -23 LUFS
- TikTok / Instagram Reels: -14 LUFS

注意:單 pass loudnorm 對非典藏級用途夠;要 EBU R128 嚴格認證才需要 two-pass
(跑兩遍 ffmpeg、第一遍 measure、第二遍套 measured 值)。MediaForge 預設場景
不需要那麼準。
"""


class MF_ComposeNormalize:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                # Integrated loudness target,LUFS。streaming 慣例 -14 ~ -16,廣電 -23。
                "target_i": ("FLOAT", {"default": -16.0, "min": -70.0, "max": -5.0, "step": 0.5}),
                # True Peak ceiling,dBTP。-1 是避免 clip 的最寬鬆設置。
                "target_tp": ("FLOAT", {"default": -1.0, "min": -9.0, "max": 0.0, "step": 0.1}),
                # Loudness Range,LU。較大值保留更多動態、較小值更壓平。預設 11 對 podcast OK。
                "target_lra": ("FLOAT", {"default": 11.0, "min": 1.0, "max": 50.0, "step": 0.5}),
                # linear=true 避免 dynamic range compression、保留原作 mix 的動態起伏。
                # 設 False 會強制壓平到 target_lra、犧牲動態換到嚴格符合 LUFS 目標。
                "linear": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "audio_ops": ("MF_COMPOSE_AUDIO_OPS",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE_AUDIO_OPS",)
    RETURN_NAMES = ("audio_ops",)
    FUNCTION = "add"
    CATEGORY = "MediaForge/Compose"

    def add(self, target_i, target_tp, target_lra, linear, audio_ops=None):
        params = {
            "target_i": float(target_i),
            "target_tp": float(target_tp),
            "target_lra": float(target_lra),
            "linear": bool(linear),
        }
        ops = list(audio_ops) if audio_ops else []
        ops.append({"type": "loudnorm", "params": params})
        return (ops,)


NODE_CLASS_MAPPINGS = {"MF_ComposeNormalize": MF_ComposeNormalize}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeNormalize": "📏 Compose Normalize"}
