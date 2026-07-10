"""ComfyUI Media Template — FFmpeg 驅動的影音工具節點集。

每個節點獨立寫在 ``nodes/<name>.py`` 並各自匯出 ``NODE_CLASS_MAPPINGS`` /
``NODE_DISPLAY_NAME_MAPPINGS``；這個 aggregator 會自動掃描並匯總。
要加新節點 → 在 ``nodes/`` 放新檔即可，不必改本檔。
"""

import importlib
import pkgutil

from . import nodes

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

for _, _module_name, _ in pkgutil.iter_modules(nodes.__path__):
    _module = importlib.import_module(f".nodes.{_module_name}", __package__)
    NODE_CLASS_MAPPINGS.update(getattr(_module, "NODE_CLASS_MAPPINGS", {}))
    NODE_DISPLAY_NAME_MAPPINGS.update(getattr(_module, "NODE_DISPLAY_NAME_MAPPINGS", {}))

# C5：plugin 載入時掃一次 `.mf_tmp/`，回收跨 ComfyUI 執行存活、已超過時效（預設
# 24h）的殘留檔案（例如 MF_ComposeAudioMix AUDIO dict materialize 出的 BGM WAV
# ——不再由 ComposeVideo 主動 unlink，見 utils/compose_ops.py 的說明）。
# try/except 包住：掃描失敗（權限問題等）不該擋 plugin 載入。
try:
    from .utils.plugin_tmp import sweep_stale_plugin_tmp
    _swept = sweep_stale_plugin_tmp()
    if _swept:
        print(f"[MediaForge] 已清除 {_swept} 個過期暫存檔（.mf_tmp/）")
except Exception as e:
    print(f"[MediaForge] .mf_tmp/ 掃描失敗（不影響載入）：{e}")

# 告訴 ComfyUI 我們有 frontend extension 要 serve。
# 沒這行的話 ComfyUI 不會 serve plugin 的 web/ 目錄、JS extension 完全不會 load。
# 路徑相對於本 __init__.py 所在的 plugin 根目錄。
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
