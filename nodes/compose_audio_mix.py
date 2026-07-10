"""MF_ComposeAudioMix — append amix op (BGM mixing) into MF_COMPOSE_AUDIO_OPS chain。

兩種模式 (`keep_source`):
- True (預設):source audio + BGM 混音、適合「影片配 BGM」場景
- False:純粹用 BGM、source audio 完全捨棄、適合「素材無聲、加全部音軌」

BGM 來源:dual-input pattern
- `audio_path`:檔案路徑 STRING (e.g., "input/bgm.mp3")
- `audio` AUDIO dict (optional):上游 tensor 鏈接、會 materialize 成 plugin tmp
  （`.mf_tmp/`）WAV。兩個同時接 → AUDIO dict 優先。C5 fix：這個 WAV **不會**在
  ComposeVideo 跑完自動清理——它的 path 是本節點輸出值的一部分，會被 ComfyUI
  cache 住並可能跨執行重複引用；改由 utils/plugin_tmp.py 的
  sweep_stale_plugin_tmp()（plugin 載入時執行一次）按時效（預設 >24h）回收。

`bgm_volume`:混音前先對 BGM 衰減。預設 0.3 讓 source voice 蓋過、podcast/vlog 慣例。
"""
import os

from ..utils.audio_mix import AMIX_DURATIONS
from ..utils.cache_key import path_fingerprint
from ..utils.plugin_tmp import mkstemp_in_plugin_tmp
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
        # C5 fix：寫進 plugin tmp（不是系統 temp）——這個 path 會被存進本節點的
        # 輸出值（op spec），ComfyUI cache-hit 重跑下游 ComposeVideo 時還會再讀到
        # 同一個 path，需要跨執行存活；系統 temp 沒有這個保證，plugin tmp 由
        # utils/plugin_tmp.py 的 sweep_stale_plugin_tmp() 按時效回收即可（見
        # utils/compose_ops.py apply_audio_ops_to_ir 的對應說明）。
        is_temp = False
        if audio is not None:
            resolved_path = write_audio_dict_to_wav(audio, mkstemp=mkstemp_in_plugin_tmp)
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

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # C1：ComfyUI IS_CHANGED 收到的 linked 輸入（上游 audio_ops chain、AUDIO
        # dict）一律是 None，檔案變更偵測必須由持有 path widget 的節點自己負責——
        # audio_path 是這個節點自己的 widget，IS_CHANGED 看得到。tensor 模式下這
        # 個 widget 事實上沒被讀（AUDIO dict 優先），fingerprint 它只是無害的多餘
        # 敏感度。
        return path_fingerprint(kwargs.get("audio_path", ""))


NODE_CLASS_MAPPINGS = {"MF_ComposeAudioMix": MF_ComposeAudioMix}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeAudioMix": "🎵 Compose Audio Mix"}
