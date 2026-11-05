"""MF_StartVideoBatch — 把一支影片一批一批送進處理節點。和 🎬 End Video Batch 配對使用。

批次大小、fps 等工作設定都在 End，這裡只管來源。每批由 End 驅動重新執行一次；同一個 prompt、同一批 node ID。
"""
from ..utils import video_batch
from .select_video import _list_input_videos


class MF_StartVideoBatch:
    @classmethod
    def INPUT_TYPES(s):
        # An empty input/ gives an empty list, like core Load Video: any placeholder entry would be loaded as a preview by
        # Nodes 2.0 and show an error on every new node. web/dual_input_lock.js then sends "" so this required input isn't
        # missing from the prompt, and prepare() reports a real miss.
        return {
            "required": {
                "video": (_list_input_videos(), {"video_upload": True, "tooltip": "要處理的影片（ComfyUI input/ 目錄）。接了 video_path 時不使用。"}),
                "resize": (list(video_batch.RESIZE_MODES), {"default": "none", "tooltip": "none 原尺寸；fit 等比留邊；fill 等比裁切；stretch 拉伸。"}),
                "width": ("INT", {"default": 1280, "min": 2, "max": 7680, "step": 2}),
                "height": ("INT", {"default": 720, "min": 2, "max": 4320, "step": 2}),
            },
            "optional": {
                "video_path": ("STRING", {"forceInput": True, "tooltip": "影片路徑，優先於 video。上游不能依賴批次內的節點。"}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID", "execution_list": "EXECUTION_LIST"},
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "FLOAT", "INT")
    RETURN_NAMES = ("frames", "audio", "fps", "frame_count")
    OUTPUT_TOOLTIPS = ("本批畫面，永遠是 frames_per_batch 幀（最後一批以最後一幀補足，End 會裁回）。",
                       "本批音訊。只有接線才解碼；來源沒有音軌時接線會報錯。",
                       "實際 fps。",
                       "本批送進處理節點的幀數，含補幀（不是全片總長）。")
    FUNCTION = "start"
    CATEGORY = "MediaForge/Video"

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # Same-prompt reruns evict by hand, and a result from an earlier prompt would be some other batch. Never reuse.
        return float("NaN")

    @classmethod
    def VALIDATE_INPUTS(s, video):
        # With video_path linked, a dropdown value naming a deleted file must not block the run. prepare() reports a real miss.
        return True

    def check_lazy_status(self, dynprompt=None, unique_id=None, execution_list=None, **kwargs):
        video_batch.get_job(dynprompt).prepare(unique_id, execution_list, dynprompt)
        return []

    def start(self, video, resize, width, height, video_path=None, dynprompt=None, unique_id=None, execution_list=None):
        return video_batch.get_job(dynprompt).read_batch(unique_id)


NODE_CLASS_MAPPINGS = {"MF_StartVideoBatch": MF_StartVideoBatch}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_StartVideoBatch": "🎞️ Start Video Batch"}
