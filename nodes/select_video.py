"""MF_SelectVideo — dropdown file picker for input/ video files.

為什麼分離出來：MediaForge 有 6+ 個吃 video_path 的 node (BurnSubtitle / LoopVideo /
TrimByRanges / ProbeMedia / ConcatVideos / ComposeStart)。每個都自己內建 dropdown
= 重複 UI 程式碼 + 各自 cache invalidation。集中為一個 selector node，feed 給多個
下游 = single source of truth。

對映 ComfyUI core LoadImage 的 dropdown UX，但只吐 STRING path 不 decode；想要
decode 走 MF_LoadVideoFrames（重量級、tensor pipeline）。
"""
import os

# `folder_paths` 是 ComfyUI runtime 才有的 module；放 function-level lazy-import
# 才能在 unit test / non-ComfyUI 環境也安全 load 整個 package（plugin auto-discovery
# 透過 __init__.py 的 pkgutil.iter_modules walk 每個 nodes/*.py，module-level import
# folder_paths 會讓整個 package 在 ComfyUI 沒啟動時直接炸）。
# CLAUDE.md 約定：optional / runtime-only dependency 一律 function-level import。


VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".ts")

# ComfyUI dropdown 不能是空 list — 沒檔案時放一個 placeholder string，使用者看到就知道要去 input/ 放檔
_EMPTY_PLACEHOLDER = "(input/ 目錄沒有影片檔)"


def _list_input_videos():
    """Walk ComfyUI input/ recursively, return relative paths to video files."""
    import folder_paths
    input_dir = folder_paths.get_input_directory()
    if not os.path.isdir(input_dir):
        return []
    found = []
    for root, _, names in os.walk(input_dir):
        for n in names:
            if n.lower().endswith(VIDEO_EXTENSIONS):
                rel = os.path.relpath(os.path.join(root, n), input_dir)
                # Windows path sep 統一成 / — ComfyUI workflow JSON 跨平台保險
                found.append(rel.replace(os.sep, "/"))
    return sorted(found)


class MF_SelectVideo:
    @classmethod
    def INPUT_TYPES(s):
        files = _list_input_videos()
        return {
            "required": {
                "video": (files or [_EMPTY_PLACEHOLDER],),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("video_path",)
    FUNCTION = "select"
    CATEGORY = "MediaForge/Video"

    def select(self, video):
        import folder_paths
        if video == _EMPTY_PLACEHOLDER:
            raise FileNotFoundError(
                "[Select Video] ComfyUI input/ 目錄沒有影片檔。"
                "請把影片放進 ComfyUI/input/ 然後重新整理瀏覽器讓 dropdown 重新掃描。"
            )
        input_dir = folder_paths.get_input_directory()
        path = os.path.join(input_dir, video)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"[Select Video] 找不到 {video}（重新整理瀏覽器讓 dropdown 重新掃描 input/ 目錄）"
            )
        return (path,)

    @classmethod
    def IS_CHANGED(s, video):
        # 用檔案 mtime 當 cache key：使用者換了 input/foo.mp4 的內容（即使同檔名），
        # mtime 變動 → ComfyUI 自動 invalidate 下游 node 的 output cache。
        # 對應 ComfyUI core LoadImage 的同款設計。
        import folder_paths
        if video == _EMPTY_PLACEHOLDER:
            return 0.0
        input_dir = folder_paths.get_input_directory()
        path = os.path.join(input_dir, video)
        return os.path.getmtime(path) if os.path.exists(path) else 0.0


NODE_CLASS_MAPPINGS = {"MF_SelectVideo": MF_SelectVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SelectVideo": "📂 Select Video"}
