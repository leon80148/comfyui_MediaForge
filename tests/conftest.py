"""pytest / standalone test bootstrap.

為什麼存在：MediaForge 的 `nodes/select_video.py` 在 module top-level
`import folder_paths`（ComfyUI 內建 module）。跑 unit test 時 ComfyUI 未啟動、
`folder_paths` 不在 sys.path，整個 package `__init__.py` 的 pkgutil walk 會炸。

這個 conftest 在任何 test collection / module import 之前就把一個極簡的
`folder_paths` stub 注入到 sys.modules。實際的 ComfyUI 行為由具體測試自己 mock /
patch；stub 只負責讓 import chain 不爆。

也設定 sys.path 包含 plugin 根目錄與 ComfyUI/custom_nodes 父目錄，讓
`from comfyui_MediaForge.nodes.foo import ...` 跟 `import utils.bar` 兩種風格都能 work。
"""
import os
import sys
import tempfile
import types


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_TESTS_DIR)
_CUSTOM_NODES_PARENT = os.path.dirname(_PLUGIN_DIR)

for p in (_CUSTOM_NODES_PARENT, _PLUGIN_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


def _install_folder_paths_stub():
    """Provide a minimal `folder_paths` module so `import folder_paths` succeeds.

    Real ComfyUI exposes a richer API; tests only need the surface used by
    MediaForge: `get_input_directory()`, `get_output_directory()`,
    `get_save_image_path()`. The stub points input/output to tempdirs.
    """
    if "folder_paths" in sys.modules:
        return  # 真實 ComfyUI 或 prior stub 已存在 — 不覆蓋

    mod = types.ModuleType("folder_paths")
    base = tempfile.gettempdir()
    in_dir = os.path.join(base, "mf_test_input")
    out_dir = os.path.join(base, "mf_test_output")
    os.makedirs(in_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    mod.get_input_directory = lambda: in_dir
    mod.get_output_directory = lambda: out_dir

    def get_save_image_path(prefix, output_dir):
        # 對齊 ComfyUI 真實 API 簽名；MediaForge 自己已不依賴此函式（用自家 output_path
        # helper），但保留 stub 以防其他 caller。
        folder, name = os.path.split(prefix)
        full = os.path.join(output_dir, folder) if folder else output_dir
        os.makedirs(full, exist_ok=True)
        return (full, name, 0, prefix, name)

    mod.get_save_image_path = get_save_image_path
    sys.modules["folder_paths"] = mod


_install_folder_paths_stub()
