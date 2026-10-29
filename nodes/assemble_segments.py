"""MF_AssembleSegments — 把 MF_SEGMENT_STATE 的 spill parts 編成最終影片（ProRes 為 .mov）。

parts 是無損 FFV1 + float32 WAV；這裡才做唯一一次有損編碼。音訊三條路：內部音軌、外部 audio
整條取代、兩者皆無（輸出不含音軌）。spill parts 不在這裡刪——它們屬於仍可能被快取重用的 state。
"""
import json

from ..utils import segment_spill
from ..utils.encoder import build_encoder_args, get_available_codecs, pick_default_codec
from ..utils.ffmpeg import ensure_ffmpeg
from ..utils.output_path import output_path_to_ui_entry
from .append_segment import _check_interrupt

LABEL = segment_spill.ASSEMBLE_LABEL


class MF_AssembleSegments:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "state": ("MF_SEGMENT_STATE",),
                "filename_prefix": ("STRING", {"default": "MediaForge/timeline"}),
                "codec": (list(get_available_codecs().keys()), {"default": pick_default_codec()}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
                "preset": (
                    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"],
                    {"default": "medium"},
                ),
            },
            "optional": {
                "audio": ("AUDIO", {"tooltip": "整條取代最終音軌；長度自動補靜音 / 截斷到影片長度。"}),
                "lock_audio": (["original", "generated"], {
                    "default": "original",
                    "tooltip": "original：鎖定原音的區間在母帶換回原聲（48 kHz）；generated：全部用生成音軌。沒有鎖音時無作用。",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("final_video_path", "report")
    FUNCTION = "assemble"
    CATEGORY = "MediaForge/Video"
    OUTPUT_NODE = True

    def assemble(self, state, filename_prefix, codec, crf, preset, audio=None, lock_audio="original"):
        segment_spill.check_state(state, LABEL)
        if not ensure_ffmpeg():
            raise RuntimeError(f"[{LABEL}] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        codec_id, pix_fmt = get_available_codecs()[codec]
        if codec_id == "prores_ks":
            # ProRes 沒有 crf / preset；profile 3 = 422 HQ，與 MF_SaveVideoFrames 一致
            ext, encoder_args = ".mov", build_encoder_args(codec_id) + ["-profile:v", "3"]
        else:
            ext, encoder_args = ".mp4", build_encoder_args(codec_id, crf=crf, preset=preset)
        override = None if audio is None else segment_spill.audio_input(audio, LABEL)
        path, report = segment_spill.assemble(state, filename_prefix, ext, pix_fmt, encoder_args, override,
                                              check_interrupt=_check_interrupt, lock_audio=lock_audio)
        report["codec"] = codec_id
        print(f"[{LABEL}] 輸出成功: {path}")
        entry = output_path_to_ui_entry(path)
        return {"ui": {"images": [entry] if entry else []},
                "result": (path, json.dumps(report, ensure_ascii=False))}


NODE_CLASS_MAPPINGS = {"MF_AssembleSegments": MF_AssembleSegments}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_AssembleSegments": "🧵 Assemble Segments"}
