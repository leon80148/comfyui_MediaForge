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
# F6 [P3]：補 gif（MF_SaveVideoFrames 的 gif 偽 codec）與常見音訊副檔名
# （wav/mp3/m4a/ogg/flac，ExtractAudio 等音訊輸出節點會用到）。
_KNOWN_EXTS = (
    ".srt", ".txt", ".vtt", ".ass",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".gif", ".wav", ".mp3", ".m4a", ".ogg", ".flac",
)


def resolve_output_path(filename_prefix, ext):
    """filename_prefix + ext → counter-incremented full path.

    Args:
        filename_prefix: 使用者填的 prefix，可含子目錄如 "MediaForge/looped"。
                         誤填副檔名（如 "looped.mp4"）會被剝掉，避免雙副檔名。
                         `/` 或 `\\` 皆可分子目錄（兩平台語意一致）；不允許用 `..`
                         跳出 output_dir。
        ext: 副檔名（含 leading dot），例 ".mp4" / ".srt" / ".mov"。

    Returns:
        絕對路徑：`<output_dir>/<subfolder>/<filename>_<counter:05d><ext>`。
        Counter = 該資料夾內既有同 prefix+ext 檔的 max 序號 + 1（從 1 起跳）。

    Raises:
        ValueError: filename_prefix 解析後的資料夾落在 output_dir 之外（路徑穿越）。
    """
    import folder_paths

    # 剝掉誤加的副檔名
    for known in _KNOWN_EXTS:
        if filename_prefix.lower().endswith(known):
            filename_prefix = filename_prefix[: -len(known)]
            break

    # Windows 反斜線正規化成 `/`：POSIX 上 os.path.split 認不得 `\` 當分隔符，
    # 沒這步 Linux/macOS 部署填 "MediaForge\looped" 會整串被當成檔名、分不出子目錄。
    filename_prefix = filename_prefix.replace("\\", "/")

    # 切子目錄 + 確保目錄存在
    output_dir = folder_paths.get_output_directory()
    subfolder, filename = os.path.split(filename_prefix)
    full_output_folder = os.path.join(output_dir, subfolder) if subfolder else output_dir

    # 防路徑穿越：`..` 或絕對路徑 subfolder 可能讓 full_output_folder 落在 output_dir
    # 之外；用 realpath 解掉 `..` / symlink 後跟 output_dir 比對 commonpath。
    # ComfyUI core 的 get_save_image_path 有做這層防護，我們自製版原本沒有（W1-7）。
    real_output_dir = os.path.realpath(output_dir)
    real_full_output_folder = os.path.realpath(full_output_folder)
    try:
        inside_output_dir = (
            os.path.commonpath([real_output_dir, real_full_output_folder]) == real_output_dir
        )
    except ValueError:
        inside_output_dir = False  # 不同磁碟機代號（Windows）→ 一定不在 output_dir 內
    if not inside_output_dir:
        raise ValueError(
            f"[resolve_output_path] filename_prefix={filename_prefix!r} 解析後的路徑"
            f"（{real_full_output_folder}）落在 output 目錄之外。允許使用子目錄，"
            "但不允許用 `..` 或絕對路徑跳出 output/ —— 請改用不含 `..` 的相對子目錄路徑。"
        )

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


# F3 [P2]：stream-copy 語意的輸出（lossless trim / concat copy 模式）不 re-encode，
# 輸出容器必須跟來源一致，否則可能 mux 進不支援來源 codec tag 的容器（例：ProRes
# 塞進 .mp4 muxer 直接 mux fail，mp4 muxer 沒有 prores tag）。
_VIDEO_CONTAINER_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def source_container_ext(path):
    """來源檔案副檔名（lower-case，含 leading dot）；未知或缺副檔名退 `.mp4`。

    給 stream-copy 輸出用：`nodes/trim_by_ranges.py` 的 lossless 模式、
    `nodes/concat_videos.py` 的 copy 模式都靠這個決定輸出容器（沿用第一個/來源
    輸入的副檔名，不看 codec widget）。
    """
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in _VIDEO_CONTAINER_EXTS else ".mp4"
