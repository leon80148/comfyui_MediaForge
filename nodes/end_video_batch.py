"""MF_EndVideoBatch — 收下每一批處理後的畫面，全部跑完後組成成片。和 🎞️ Start Video Batch 配對使用。

這個節點驅動整個工作：check_lazy_status 每批要一次畫面、落盤、清掉上一批的輸出，再要下一批；FUNCTION 只做最後的組裝。
frames / audio 用 rawLink 自己讀，這樣看得到 ExecutionBlocker，list 輸出也只算一批。
"""
from ..utils import video_batch
from ..utils.encoder import get_available_codecs, pick_default_codec
from ..utils.ffmpeg import ensure_ffmpeg
from ..utils.output_path import output_path_to_ui_entry


class MF_EndVideoBatch:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "frames": ("IMAGE", {"lazy": True, "rawLink": True, "tooltip": "處理後的畫面。"}),
                "frames_per_batch": ("INT", {
                    "default": video_batch.DEFAULT_FRAMES_PER_BATCH, "min": 1, "max": 100000, "socketless": True,
                    "tooltip": "每批送進處理節點的幀數；越小越省 RAM / VRAM。H3 需符合 17n+5（例：124）。",
                }),
                "fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 240.0, "step": 0.001, "socketless": True,
                                  "tooltip": "轉成這個固定 fps；0 使用來源報告的 fps。兩支影片 fps 不同時必填。"}),
                "max_frames": ("INT", {"default": 0, "min": 0, "max": 100000000, "socketless": True, "tooltip": "只處理前幾幀，試跑用；0 全部。"}),
                "codec": (list(get_available_codecs().keys()), {"default": pick_default_codec(), "socketless": True}),
                "keep_source_audio": ("BOOLEAN", {"default": True, "socketless": True, "tooltip": "成片保留來源音軌。接了 audio 時不使用。"}),
                "filename_prefix": ("STRING", {"default": "MediaForge/video_batch", "socketless": True, "advanced": True}),
                "overlap_frames": ("INT", {"default": 0, "min": 0, "max": 50000, "socketless": True, "advanced": True,
                                           "tooltip": "相鄰兩批重疊幾幀做淡接，上限是 frames_per_batch 的一半。"}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51, "socketless": True, "advanced": True}),
                "preset": (["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
                           {"default": "medium", "socketless": True, "advanced": True}),
            },
            "optional": {
                "audio": ("AUDIO", {"lazy": True, "rawLink": True, "tooltip": "取代成片音軌。接批次外（例如 Load Audio）整條取代；接批次內（例如 Start 的 audio）逐批取用。"}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID", "execution_list": "EXECUTION_LIST"},
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("final_video_path", "report")
    FUNCTION = "end"
    CATEGORY = "MediaForge/Video"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return float("NaN")

    def check_lazy_status(self, dynprompt=None, unique_id=None, execution_list=None, **kwargs):
        if not ensure_ffmpeg():
            raise RuntimeError(f"[{video_batch.END_LABEL}] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        video_batch.get_job(dynprompt).drive(unique_id, execution_list, dynprompt)
        return []

    def end(self, frames, frames_per_batch, fps, max_frames, codec, keep_source_audio, filename_prefix, overlap_frames, crf, preset,
            audio=None, dynprompt=None, unique_id=None, execution_list=None):
        path, report = video_batch.get_job(dynprompt).finish()
        print(f"[{video_batch.END_LABEL}] 輸出成功: {path}")
        entry = output_path_to_ui_entry(path)
        return {"ui": {"images": [entry] if entry else [], "animated": (True,)}, "result": (path, report)}


NODE_CLASS_MAPPINGS = {"MF_EndVideoBatch": MF_EndVideoBatch}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_EndVideoBatch": "🎬 End Video Batch"}
