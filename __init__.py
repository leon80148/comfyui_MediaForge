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

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
