"""IS_CHANGED cache-key helpers — mtime/size fingerprint for path-consumer nodes.

為什麼需要：ComfyUI 預設用「輸入值本身」算節點快取 key —— STRING path 沒變、輸出
就直接沿用快取，即使該路徑指向的檔案內容已經被覆寫（同路徑換內容）。對齊 ComfyUI
core LoadImage 的作法：IS_CHANGED 回傳跟檔案 mtime/size 掛鉤的字串，檔案一變就讓
ComfyUI 判定「輸入變了」重新執行，避免下游拿到 stale 但看起來合法的舊輸出。
"""
import os


def path_fingerprint(*paths):
    """把多個 path 轉成一個 fingerprint 字串，用 `|` join。

    每個 path：存在 → f"{abspath}:{mtime_ns}:{size}"；不存在（含空字串 / None）→
    穩定常數 "missing"（IS_CHANGED 不必為了「路徑目前不存在」動態變化 — 節點執行時
    仍會走自己的 FileNotFoundError 檢查）。
    """
    parts = []
    for p in paths:
        if p and os.path.exists(p):
            st = os.stat(p)
            parts.append(f"{os.path.abspath(p)}:{st.st_mtime_ns}:{st.st_size}")
        else:
            parts.append("missing")
    return "|".join(parts)
