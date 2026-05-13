"""MF_ComposeAudioMix — append amix op (BGM mixing) into MF_COMPOSE_AUDIO_OPS chain。

兩種模式 (`keep_source`):
- True (預設):source audio + BGM 混音、適合「影片配 BGM」場景
- False:純粹用 BGM、source audio 完全捨棄、適合「素材無聲、加全部音軌」

BGM 來源:dual-input pattern
- `audio_path`:檔案路徑 STRING (e.g., "input/bgm.mp3")
- `audio` AUDIO dict (optional):上游 tensor 鏈接、會 materialize 成 temp WAV、
  ComposeVideo 跑完自動清理。兩個同時接 → AUDIO dict 優先。

`bgm_volume`:混音前先對 BGM 衰減。預設 0.3 讓 source voice 蓋過、podcast/vlog 慣例。
"""
import os

from ..utils.audio_mix import AMIX_DURATIONS
from ..utils.video_io import write_audio_dict_to_wav


class MF_ComposeAudioMix:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "audio_path": ("STRING", {"default": "input/bgm.mp3"}),
                "keep_source": ("BOOLEAN", {"default": True}),
                "bgm_volume": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 2.0, "step": 0.05}),
                "duration": (AMIX_DURATIONS, {"default": "first"}),
            },
            "optional": {
                "audio_ops": ("MF_COMPOSE_AUDIO_OPS",),
                # Dual-input:接 AUDIO dict 蓋過 audio_path
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE_AUDIO_OPS",)
    RETURN_NAMES = ("audio_ops",)
    FUNCTION = "add"
    CATEGORY = "MediaForge/Compose"

    def add(self, audio_path, keep_source, bgm_volume, duration,
            audio_ops=None, audio=None):
        # Dual-input dispatch — AUDIO dict 優先,materialize 成 temp WAV
        is_temp = False
        if audio is not None:
            resolved_path = write_audio_dict_to_wav(audio)
            is_temp = True
        else:
            if not os.path.exists(audio_path):
                raise FileNotFoundError(f"[Compose Audio Mix] 找不到 BGM 檔:{audio_path}")
            resolved_path = audio_path

        params = {
            "keep_source": bool(keep_source),
            "bgm_volume": float(bgm_volume),
            "duration": duration,
        }

        ops = list(audio_ops) if audio_ops else []
        ops.append({
            "type": "amix",
            "audio_path": resolved_path,
            "_is_temp": is_temp,
            "params": params,
        })
        return (ops,)


NODE_CLASS_MAPPINGS = {"MF_ComposeAudioMix": MF_ComposeAudioMix}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeAudioMix": "🎵 Compose Audio Mix"}
