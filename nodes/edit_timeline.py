"""MF_EditTimeline — 時間軸分段計畫（MF_SEGMENT_PLAN）。

timeline_data 是唯一持久化載體：GUI（web/timeline_editor.js）只負責編輯這個 JSON，
API 使用者也可以直接送字串。每次執行都在這裡重新嚴格驗證，不信任前端算出的值。
"""
import json

from ..utils.cache_key import path_fingerprint
from ..utils.timeline_media import describe_material
from ..utils.timeline_messages import format_message
from ..utils.timeline_plan import FPS_PRESETS, GRIDS, MODES, compile_plan, cut_ranges_seconds, parse_timeline_data
from ..utils.timeline_source import describe_source, resolve_timeline_source
from .select_video import _list_input_videos

NO_SOURCE = "(none)"
# 新節點預設即合法：H3 單段 107 幀（≈4.46 s @24fps）
DEFAULT_TIMELINE = json.dumps({"version": 1, "source": {"origin_frame": 0},
                               "segments": [{"start": 0, "end": 107, "prompt": ""}]})


class MF_EditTimeline:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "video": ([NO_SOURCE] + _list_input_videos(), {
                    "default": NO_SOURCE,
                    "tooltip": "參考來源影片（input/）。generate 模式可不選；cut 模式必選。",
                }),
                "fps": (list(FPS_PRESETS), {"default": "24", "tooltip": "時間軸幀率（有理數）。H3 必須 24。"}),
                "grid": (list(GRIDS), {
                    "default": "h3_5+17n",
                    "tooltip": "決定段長與重疊格點；不選擇或載入模型。cut 模式不使用。",
                }),
                "mode": (list(MODES), {
                    "default": "generate",
                    "tooltip": "generate 規劃生成段；cut 選取來源區間，不續接 latent。",
                }),
                "timeline_data": ("STRING", {"default": DEFAULT_TIMELINE, "multiline": True, "dynamicPrompts": False}),
            },
        }

    RETURN_TYPES = ("MF_SEGMENT_PLAN", "SILENCE_RANGES", "STRING", "INT", "INT")
    RETURN_NAMES = ("segment_plan", "ranges", "plan_json", "segment_count", "total_frames")
    FUNCTION = "edit"
    CATEGORY = "MediaForge/Video"

    def edit(self, video, fps, grid, mode, timeline_data):
        data = parse_timeline_data(timeline_data)
        source = None if video == NO_SOURCE else describe_source(video)
        rate = FPS_PRESETS.get(fps, (24, 1))
        snapshots = {m["id"]: describe_material(m, rate) for m in data["materials"]}
        plan = compile_plan(data, mode, fps, grid, source, snapshots)
        if source is not None and source["source_start_us"] is None:
            print(format_message("Edit Timeline", "W_SOURCE_TIME_UNKNOWN"))
        return (plan, cut_ranges_seconds(plan), json.dumps(plan, ensure_ascii=False),
                len(plan["segments"]), plan["total_frames"])

    @classmethod
    def IS_CHANGED(s, **kwargs):
        text = kwargs.get("timeline_data")
        if text is None or kwargs.get("video") is None:
            # 接成 linked 輸入時這裡看不到內容（C1）：每次重跑、重建素材快照
            return float("nan")
        refs = [kwargs.get("video", NO_SOURCE)]
        try:
            refs += [m["ref"] for m in json.loads(text).get("materials", []) if isinstance(m, dict)]
        except (ValueError, AttributeError, TypeError):
            pass  # 壞掉的 JSON 交給 edit() 報清楚的錯
        out = []
        for ref in refs:
            if ref == NO_SOURCE or not isinstance(ref, str):
                continue
            try:
                out.append(path_fingerprint(resolve_timeline_source(ref)))
            except (ValueError, OSError):
                # 非法 / 找不到的路徑交給 edit() 報清楚的錯；這裡只需要穩定值
                out.append(f"invalid:{ref}")
        return "|".join(out)


NODE_CLASS_MAPPINGS = {"MF_EditTimeline": MF_EditTimeline}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_EditTimeline": "⏱️ Edit Timeline"}
