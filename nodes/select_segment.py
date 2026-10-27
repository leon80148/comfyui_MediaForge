"""MF_SelectSegment — 從 MF_SEGMENT_PLAN 取出第 i 段的 MF_SEGMENT 規格。

segment_index 從 0 起算，可直接接 core Start Loop 的 iteration_index。輸出的 MF_SEGMENT
是 Extend / Append / LoadSegmentFrames 共用的同一份快照——fps、重疊、carry 只在這裡算一次。
"""
from ..utils.timeline_plan import segment_spec


class MF_SelectSegment:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "segment_plan": ("MF_SEGMENT_PLAN",),
                "segment_index": ("INT", {
                    "default": 0, "min": 0, "max": 255, "step": 1,
                    "tooltip": "0 起算；loop 時接 Start Loop 的 iteration_index。",
                }),
            },
        }

    RETURN_TYPES = ("MF_SEGMENT", "STRING", "INT", "FLOAT", "INT")
    RETURN_NAMES = ("segment", "prompt", "length", "start_sec", "segment_count")
    FUNCTION = "select"
    CATEGORY = "MediaForge/Video"

    def select(self, segment_plan, segment_index):
        if not isinstance(segment_plan, dict) or segment_plan.get("type") != "MF_SEGMENT_PLAN":
            raise ValueError("[🎯 Select Segment] segment_plan 必須來自 ⏱️ Edit Timeline")
        spec = segment_spec(segment_plan, segment_index)
        num, den = spec["fps"]
        return (spec, spec["prompt"], spec["length"], spec["start"] * den / num, spec["count"])


NODE_CLASS_MAPPINGS = {"MF_SelectSegment": MF_SelectSegment}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SelectSegment": "🎯 Select Segment"}
