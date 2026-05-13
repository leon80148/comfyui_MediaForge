"""Output path resolution — counter-incremented filenames, no silent overwrite.

對齊 ComfyUI 核心 SaveImage 的 `filename_prefix` 慣例：每次跑 workflow 都產出
`output/<prefix>_<counter:05d>.<ext>`，不會覆蓋上次輸出。

為什麼自己掃 dir 而非用 `folder_paths.get_save_image_path()`：核心那個函式掃既存檔用
`<name>_<digits>_*` pattern（**末尾必須有底線**），對應 `SaveImage` 的 `_00001_.png`
格式。我們要 `_00001.mp4` 乾淨檔名，所以自己用 regex 掃 `<name>_<digits><ext>$`。
"""
import os
import re


# 使用者可能誤把副檔名打進 filename_prefix（如 "looped.mp4"）；
# 先剝掉這些已知 ext，下游再加自己想要的副檔名上去
_KNOWN_EXTS = (
    ".srt", ".txt", ".vtt", ".ass",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
)


def resolve_output_path(filename_prefix, ext):
    """filename_prefix + ext → counter-incremented full path.

    Args:
        filename_prefix: 使用者填的 prefix，可含子目錄如 "MediaForge/looped"。
                         誤填副檔名（如 "looped.mp4"）會被剝掉，避免雙副檔名。
        ext: 副檔名（含 leading dot），例 ".mp4" / ".srt" / ".mov"。

    Returns:
        絕對路徑：`<output_dir>/<subfolder>/<filename>_<counter:05d><ext>`。
        Counter = 該資料夾內既有同 prefix+ext 檔的 max 序號 + 1（從 1 起跳）。
    """
    import folder_paths

    # 剝掉誤加的副檔名
    for known in _KNOWN_EXTS:
        if filename_prefix.lower().endswith(known):
            filename_prefix = filename_prefix[: -len(known)]
            break

    # 切子目錄 + 確保目錄存在
    output_dir = folder_paths.get_output_directory()
    subfolder, filename = os.path.split(filename_prefix)
    full_output_folder = os.path.join(output_dir, subfolder) if subfolder else output_dir
    os.makedirs(full_output_folder, exist_ok=True)

    # 掃既存檔取 max counter
    pattern = re.compile(rf"^{re.escape(filename)}_(\d+){re.escape(ext)}$", re.IGNORECASE)
    max_n = 0
    for f in os.listdir(full_output_folder):
        m = pattern.match(f)
        if m:
            max_n = max(max_n, int(m.group(1)))
    counter = max_n + 1

    return os.path.join(full_output_folder, f"{filename}_{counter:05d}{ext}")


def output_path_to_ui_entry(output_path, type="output"):
    """Absolute output path → ComfyUI UI dict entry for /history exposure.

    搭配 resolve_output_path()。寫完檔後在 node 的 return 用：
        return {"ui": {"images": [<entry>]}, "result": (output_path,)}

    `images` 是 ComfyUI 通用 UI key（SaveImage canonical）— 影片 / 音訊 / 任意檔
    都走這個 key，frontend 跟 API 客戶端用 /view?filename=X&subfolder=Y&type=output
    下載。Windows 上 os.path.relpath 會吐 `\\` separator，所以 normalize 成 `/`
    （ComfyUI /view URL parser 走 POSIX 風）。
    """
    import folder_paths
    output_dir = folder_paths.get_output_directory()
    rel = os.path.relpath(output_path, output_dir)
    subfolder, filename = os.path.split(rel)
    return {
        "filename": filename,
        "subfolder": subfolder.replace("\\", "/"),
        "type": type,
    }
