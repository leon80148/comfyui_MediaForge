"""Plugin-internal temp dir (`.mf_tmp/`) — shared mkstemp helper + stale-file sweep.

為什麼獨立成一個 module（原本 `_mkstemp_in_plugin_tmp` 是 utils/compose_ir.py 的私有
函式，W1-9）：C5 fix 需要在 nodes/compose_audio_mix.py 也用同一份「寫進 plugin
tmp」邏輯（AUDIO dict materialize 出的 BGM WAV，見 sweep_stale_plugin_tmp()
docstring），compose_ir.py 專注 IR compile、不該是這種跨 node 共用 helper 的家。
utils/compose_ir.py 仍 re-export `_mkstemp_in_plugin_tmp` / `_PLUGIN_TMP_DIR`
維持既有 import path 相容（見該檔案）。
"""
import os
import tempfile
import time


_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_TMP_DIR = os.path.join(_PLUGIN_DIR, ".mf_tmp")


def mkstemp_in_plugin_tmp(suffix="", prefix="mf_"):
    """跟 tempfile.mkstemp 同介面 (fd, path)，優先寫進 plugin 內 `.mf_tmp/`。

    為什麼不用系統 temp：Windows 中文使用者名稱（例如
    `C:\\Users\\游永亮\\AppData\\Local\\Temp\\`）的路徑含非 ASCII 字元，
    libavfilter 開 textfile= / -filter_complex_script 讀檔偶爾失敗（P0-9）。
    Plugin 內路徑固定、ASCII，由我們自己管控。
    目錄建立或寫入失敗（唯讀安裝 / 權限問題）時退回 tempfile.mkstemp 原行為 ——
    caller 的 cleanup（unlink 回傳的 path）語意兩種來源一致，不受影響。
    """
    try:
        os.makedirs(PLUGIN_TMP_DIR, exist_ok=True)
        return tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=PLUGIN_TMP_DIR)
    except OSError:
        return tempfile.mkstemp(suffix=suffix, prefix=prefix)


def sweep_stale_plugin_tmp(max_age_hours=24.0):
    """刪除 `.mf_tmp/` 內 mtime 超過 max_age_hours 的檔案；回傳刪除數。

    為什麼需要（C5 [嚴重] fix）：MF_ComposeAudioMix 的 AUDIO dict 模式會把
    materialize 出的 BGM WAV path 寫進節點「輸出值」的 op spec 裡（`_is_temp:
    True`），這個輸出值會被 ComfyUI 按 upstream 節點的 input hash cache 住。
    以前 ComposeVideo 執行完會在 finally 把這個 WAV unlink 掉——但如果之後只改
    ComposeVideo 自己的 widget（例如 crf）重跑，AudioMix 沒有任何輸入變化、
    ComfyUI cache-hit 直接回傳舊的 op spec，仍然指著同一個（已被刪除的）WAV
    path，導致 ffmpeg 每次都對著不存在的檔案報錯，直到使用者手動強制 AudioMix
    重新執行。

    修法：這個 WAV 不再由 ComposeVideo 主動 unlink（見
    utils/compose_ops.py apply_audio_ops_to_ir 的說明），生命週期改成「寫進
    plugin tmp、跨 ComfyUI 執行存活、由時效回收」——plugin 載入時（__init__.py）
    呼叫本函式一次，把太舊（預設 >24h，早已不可能被任何後續 cache-hit 引用）的
    殘留檔案清掉，避免 `.mf_tmp/` 無限長大。

    Best-effort：單一檔案的 stat/unlink 失敗（競態刪除、權限問題）不中斷整個
    掃描，也不 raise——這只是背景維護、不該擋 plugin 載入或影響使用者工作流。
    """
    if not os.path.isdir(PLUGIN_TMP_DIR):
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for name in os.listdir(PLUGIN_TMP_DIR):
        path = os.path.join(PLUGIN_TMP_DIR, name)
        try:
            if os.path.isfile(path) and os.stat(path).st_mtime < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed
