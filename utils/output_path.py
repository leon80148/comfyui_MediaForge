"""Output path resolution — counter-incremented filenames, no silent overwrite.

對齊 ComfyUI 核心 SaveImage 的 `filename_prefix` 慣例：每次跑 workflow 都產出
`output/<prefix>_<counter:05d>.<ext>`，不會覆蓋上次輸出。

為什麼自己掃 dir 而非用 `folder_paths.get_save_image_path()`：核心那個函式掃既存檔用
`<name>_<digits>_*` pattern（**末尾必須有底線**），對應 `SaveImage` 的 `_00001_.png`
格式。我們要 `_00001.mp4` 乾淨檔名，所以自己用 regex 掃 `<name>_<digits><ext>$`。
"""
import os
import re


# R8-2 [P2]：plugin 接受的影片副檔名唯一清單 —— MF_SelectVideo dropdown 掃描、
# 這裡的 _KNOWN_EXTS、下面 source_container_ext() 的保留清單都共用這個常數，避免
# 三處各自維護一份清單而 drift（過去 source_container_ext 自己維護一份只有 6 種
# 的清單，沒涵蓋 MF_SelectVideo 早就接受的 .ts/.mpg/.mpeg，.ts 來源走 concat copy
# / trim lossless 會被誤 fallback 成 .mp4）。
VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".ts")

# 使用者可能誤把副檔名打進 filename_prefix（如 "looped.mp4"）；
# 先剝掉這些已知 ext，下游再加自己想要的副檔名上去
# F6 [P3]：補 gif（MF_SaveVideoFrames 的 gif 偽 codec）與常見音訊副檔名
# （wav/mp3/m4a/ogg/flac，ExtractAudio 等音訊輸出節點會用到）。
_KNOWN_EXTS = (
    ".srt", ".txt", ".vtt", ".ass",
    *VIDEO_EXTENSIONS,
    ".gif", ".wav", ".mp3", ".m4a", ".ogg", ".flac",
)


def resolve_output_path(filename_prefix, ext):
    """filename_prefix + ext → counter-incremented full path.

    Args:
        filename_prefix: 使用者填的 prefix，可含子目錄如 "MediaForge/looped"。
                         誤填副檔名（如 "looped.mp4"）會被剝掉，避免雙副檔名。
                         `/` 或 `\\` 皆可分子目錄（兩平台語意一致）。可以用 `..`
                         或絕對路徑跳出 output_dir——仍在 ComfyUI 目錄樹內只印警告
                         照常寫入，跳出 ComfyUI 目錄樹則 raise ValueError（見下方
                         R7-1 說明）。

    Returns:
        絕對路徑：`<output_dir>/<subfolder>/<filename>_<counter:05d><ext>`。
        Counter = 該資料夾內既有同 prefix+ext 檔的 max 序號 + 1（從 1 起跳）。

    C9 fix：`..` / 絕對路徑跳出 output_dir 曾經在這裡 raise ValueError（W1-7），但
    這破壞了升級前就存在的合法用法——`filename_prefix="../input/cleaned"` 把輸出
    鏈回 input/ 是常見手法、絕對路徑（如 `D:/exports/clip`）也是使用者的明確意圖，
    兩者升級後直接壞掉不成比例。C9 當時把處置改成「印警告後照常寫入」，不論穿越
    到哪裡都不 raise。

    R7-1 fix（2026-07-11）：C9 的「警告全放行」被 Codex R7 round review 認為形同
    允許任意路徑寫入（例如 `filename_prefix="../../../../Users/user/Desktop/export"`
    這種完全脫離 ComfyUI 安裝目錄的目標——分享出去的 workflow JSON 被別人打開
    執行就會被動寫到對方磁碟任意位置）。裁決折衷成「ComfyUI 根目錄內圍堵」：
      - 落在 output_dir 內：一如既往，無警告。
      - 落在 output_dir 外、但仍在 ComfyUI base root 內（保住 C9 想保護的
        `../input/cleaned` 相容用法）：印警告後照常寫入。
      - 落在 ComfyUI base root 之外（含任意絕對路徑）：raise ValueError，擋掉
        Codex 關切的任意寫入。
    Base root 優先讀 `folder_paths.base_path`（正式 ComfyUI 環境一定有這個屬性，
    見 ComfyUI/folder_paths.py），拿不到時 fallback 成 output_dir 的上一層目錄。
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

    # 偵測路徑穿越：`..` 或絕對路徑 subfolder 可能讓 full_output_folder 落在
    # output_dir 之外；用 realpath 解掉 `..` / symlink 後跟 output_dir 比對
    # commonpath。R7-1 fix（見上方 docstring）：三段式處置——output_dir 內無警告；
    # output_dir 外但仍在 ComfyUI base root 內印警告放行（保住 C9 想保護的
    # `../input/cleaned` 相容用法）；base root 之外 raise（擋 Codex 關切的任意
    # 路徑寫入）。
    real_output_dir = os.path.realpath(output_dir)
    real_full_output_folder = os.path.realpath(full_output_folder)
    try:
        inside_output_dir = (
            os.path.commonpath([real_output_dir, real_full_output_folder]) == real_output_dir
        )
    except ValueError:
        inside_output_dir = False  # 不同磁碟機代號（Windows）→ 一定不在 output_dir 內

    if not inside_output_dir:
        # base root 優先讀正式 ComfyUI 環境一定會設的 folder_paths.base_path；
        # 測試用的 stub 沒有這個屬性時 fallback 成 output_dir 的上一層目錄。
        base_root = getattr(folder_paths, "base_path", None)
        real_base_root = (
            os.path.realpath(base_root) if base_root
            else os.path.dirname(real_output_dir)
        )
        try:
            inside_base_root = (
                os.path.commonpath([real_base_root, real_full_output_folder]) == real_base_root
            )
        except ValueError:
            inside_base_root = False  # 不同磁碟機代號 → 一定不在 base root 內

        if not inside_base_root:
            raise ValueError(
                f"[MediaForge] filename_prefix={filename_prefix!r} 解析到 ComfyUI 目錄樹"
                f"之外（{real_full_output_folder}），僅允許輸出到 ComfyUI 目錄樹內的路徑"
                "（防止分享出去的 workflow JSON 被別人打開執行時，任意寫入對方磁碟的其他"
                "位置）。如需輸出到外部目錄，請先輸出到 output/ 底下，再自行搬移。"
            )

        print(
            f"[MediaForge] 注意：filename_prefix={filename_prefix!r} 解析到 output 目錄"
            f"之外、但仍在 ComfyUI 目錄樹內（{real_full_output_folder}）——依舊版相容行為"
            "寫入；auto-counter 檔名不會覆蓋既有檔案。"
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
        entry = output_path_to_ui_entry(output_path)
        return {"ui": {"images": [entry] if entry else []}, "result": (output_path,)}

    `images` 是 ComfyUI 通用 UI key（SaveImage canonical）— 影片 / 音訊 / 任意檔
    都走這個 key，frontend 跟 API 客戶端用 /view?filename=X&subfolder=Y&type=output
    下載。Windows 上 os.path.relpath 會吐 `\\` separator，所以 normalize 成 `/`
    （ComfyUI /view URL parser 走 POSIX 風）。

    R9-1 fix（2026-07-11）：resolve_output_path() 的舊相容模式（見該函式 R7-1
    docstring）允許 filename_prefix 解析到 output_dir 之外、只要仍在 ComfyUI base
    root 內（例如 `../input/cleaned`）。這種情況算出的 subfolder 會帶 `..`
    （如 `../input`），但 ComfyUI 內建 /view endpoint 有自己的 path-traversal
    防護、只服務 output_dir 內的檔案——這種 entry 送給 API 客戶端照 README 打
    /view 只會 404。與其吐一個保證用壞的 dict，這裡在算 relpath 前先確認
    output_path 的 realpath 是否落在 output_dir 內；不在 → 印一行繁中說明後回
    None，呼叫端把 ui.images 留空陣列（node 仍正常寫檔、result 仍回傳正確的
    絕對路徑，只是不會出現在 /history 的 ui 檔案清單）。跨磁碟機代號（Windows）
    算 commonpath 會 ValueError，同樣視為「不在 output_dir 內」。
    """
    import folder_paths
    output_dir = folder_paths.get_output_directory()
    real_output_dir = os.path.realpath(output_dir)
    real_output_path = os.path.realpath(output_path)
    try:
        inside_output_dir = (
            os.path.commonpath([real_output_dir, real_output_path]) == real_output_dir
        )
    except ValueError:
        inside_output_dir = False  # 不同磁碟機代號（Windows）→ 一定不在 output_dir 內

    if not inside_output_dir:
        print(
            f"[MediaForge] 注意：輸出位於 output/ 之外（{real_output_path}），"
            "不會出現在 /history 的 ui 檔案清單（/view 僅能存取 output/ 內檔案）。"
        )
        return None

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
# R8-2 [P2]：改用 VIDEO_EXTENSIONS（單一事實來源）而非自己維護一份清單 ——
# 原本的 6 種沒涵蓋 MF_SelectVideo 早就接受的 .ts/.mpg/.mpeg，這三種來源會被誤
# fallback 成 .mp4（mpegts muxer 對 stream copy 沒問題，不需要退階）。
_VIDEO_CONTAINER_EXTS = set(VIDEO_EXTENSIONS)


def source_container_ext(path):
    """來源檔案副檔名（lower-case，含 leading dot）；未知或缺副檔名退 `.mp4`。

    給 stream-copy 輸出用：`nodes/trim_by_ranges.py` 的 lossless 模式、
    `nodes/concat_videos.py` 的 copy 模式都靠這個決定輸出容器（沿用第一個/來源
    輸入的副檔名，不看 codec widget）。已知副檔名清單見 VIDEO_EXTENSIONS。
    """
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in _VIDEO_CONTAINER_EXTS else ".mp4"
