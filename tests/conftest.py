"""pytest / standalone test bootstrap.

為什麼存在：MediaForge 的 `nodes/select_video.py` 在 module top-level
`import folder_paths`（ComfyUI 內建 module）。跑 unit test 時 ComfyUI 未啟動、
`folder_paths` 不在 sys.path，整個 package `__init__.py` 的 pkgutil walk 會炸。

這個 conftest 在任何 test collection / module import 之前就把一個極簡的
`folder_paths` stub 注入到 sys.modules。實際的 ComfyUI 行為由具體測試自己 mock /
patch；stub 只負責讓 import chain 不爆。

也設定 sys.path 包含 plugin 根目錄與 ComfyUI/custom_nodes 父目錄，讓
`from comfyui_MediaForge.nodes.foo import ...` 跟 `import utils.bar` 兩種風格都能 work。

P3-10 (2026-05-14)：補 autouse fixture `_reset_folder_paths_stub` 強制每個 test 開始時
folder_paths.get_output_directory / get_input_directory 都回到 stable stub。背景：
test_compose_audio_chain 等檔案會 `folder_paths.get_output_directory = lambda: td`
（td 是 TemporaryDirectory），離開 with 後 TemporaryDirectory 的 weakref.finalize 隨時
會被 GC 觸發 rmtree，但 lambda 還活著、後續測試讀到死路徑、檔案剛寫完就被 finalize 掃光
— `os.path.exists()` 偽 False。autouse fixture 在每個 test 啟動前 reset、隔離污染。

P3-10 補強：autouse fixture 同時 chdir 到 plugin root，避免 cwd-相依測試 (例如 conftest
import 路徑、`input/sample.mp4` 等 relative default) 因 pytest 啟動位置不同而炸。
"""
import os
import sys
import tempfile
import types

import pytest


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_TESTS_DIR)
_CUSTOM_NODES_PARENT = os.path.dirname(_PLUGIN_DIR)

for p in (_CUSTOM_NODES_PARENT, _PLUGIN_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


# Worktree-aware package alias:當 plugin 被 git worktree 簽到 .claude/worktrees/<name>/
# 時 dir name ≠ "comfyui_MediaForge",一般 `from comfyui_MediaForge.* import ...` 會
# 回去找 main worktree 的(舊)code、跳過正在驗的改動。這裡強制把 _PLUGIN_DIR 自己
# register 成 `comfyui_MediaForge` package、確保 test 跑的是本 plugin dir 的 source。
# Main worktree (dir 已叫 comfyui_MediaForge) 跑時行為一致、無害。
def _alias_plugin_as_comfyui_mediaforge():
    import importlib.util

    existing = sys.modules.get("comfyui_MediaForge")
    existing_path = os.path.dirname(getattr(existing, "__file__", "") or "") if existing else ""
    # 已是同一個 dir 載入 → 跳過,避免重複 exec_module
    if existing_path == _PLUGIN_DIR:
        return

    spec = importlib.util.spec_from_file_location(
        "comfyui_MediaForge",
        os.path.join(_PLUGIN_DIR, "__init__.py"),
        submodule_search_locations=[_PLUGIN_DIR],
    )
    if spec is None or spec.loader is None:
        return  # __init__.py 不在 — 不是 plugin layout、放棄 alias
    mod = importlib.util.module_from_spec(spec)
    sys.modules["comfyui_MediaForge"] = mod
    # 清掉前任 partial state (e.g. 已 register 過 main worktree 的 submodule)
    for k in list(sys.modules):
        if k.startswith("comfyui_MediaForge."):
            del sys.modules[k]
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # exec 失敗(typically folder_paths stub 還沒裝)留給 _install_folder_paths_stub 之後再試
        sys.modules.pop("comfyui_MediaForge", None)


# Stable directories for the default stub。Module-level constants 不會隨 fixture lifecycle 變、
# 也不會像 TemporaryDirectory 那樣被 weakref.finalize 偷掃。
_STUB_BASE = tempfile.gettempdir()
_STUB_INPUT = os.path.join(_STUB_BASE, "mf_test_input")
_STUB_OUTPUT = os.path.join(_STUB_BASE, "mf_test_output")
os.makedirs(_STUB_INPUT, exist_ok=True)
os.makedirs(_STUB_OUTPUT, exist_ok=True)


def _default_get_input_directory():
    return _STUB_INPUT


def _default_get_output_directory():
    return _STUB_OUTPUT


def _default_get_save_image_path(prefix, output_dir):
    # 對齊 ComfyUI 真實 API 簽名；MediaForge 自己已不依賴此函式（用自家 output_path
    # helper），但保留 stub 以防其他 caller。
    folder, name = os.path.split(prefix)
    full = os.path.join(output_dir, folder) if folder else output_dir
    os.makedirs(full, exist_ok=True)
    return (full, name, 0, prefix, name)


def _install_folder_paths_stub():
    """Provide a minimal `folder_paths` module so `import folder_paths` succeeds.

    Real ComfyUI exposes a richer API; tests only need the surface used by
    MediaForge: `get_input_directory()`, `get_output_directory()`,
    `get_save_image_path()`. The stub points input/output to module-level
    stable tempdirs (NOT to test-local TemporaryDirectory which can be
    finalize-killed mid-run).
    """
    if "folder_paths" in sys.modules:
        return  # 真實 ComfyUI 或 prior stub 已存在 — 不覆蓋

    mod = types.ModuleType("folder_paths")
    mod.get_input_directory = _default_get_input_directory
    mod.get_output_directory = _default_get_output_directory
    mod.get_save_image_path = _default_get_save_image_path
    sys.modules["folder_paths"] = mod


_install_folder_paths_stub()
_alias_plugin_as_comfyui_mediaforge()


@pytest.fixture(autouse=True)
def _reset_folder_paths_stub():
    """Each test starts with the canonical stub。

    Why autouse: test_compose_audio_chain / test_compose_video_v2 等檔內部用
    `folder_paths.get_output_directory = lambda: td` 模式做 per-test 隔離，但 td 是
    TemporaryDirectory — 離開 with block 後其 weakref.finalize 隨機 GC 期間會 rmtree、
    導致後續測試讀到的 path string 對應到已被掃掉的目錄。下一個寫檔流程是：
      1. resolve_output_path() → 死 path string
      2. concat() 內 makedirs 重建 → OK
      3. ffmpeg 寫檔成功 → OK
      4. GC 跑 (任何時機) → rmtree 掃掉剛建的整支樹 → 檔案蒸發
      5. assert os.path.exists(out) → False
    teardown 後把這三個 attribute 還原成 module-level stable function 切斷污染。
    """
    import folder_paths  # type: ignore  # noqa: F401

    saved = (
        getattr(folder_paths, "get_input_directory", None),
        getattr(folder_paths, "get_output_directory", None),
        getattr(folder_paths, "get_save_image_path", None),
    )
    folder_paths.get_input_directory = _default_get_input_directory
    folder_paths.get_output_directory = _default_get_output_directory
    folder_paths.get_save_image_path = _default_get_save_image_path
    try:
        yield
    finally:
        # 還原到 fixture 進入時的狀態（通常會是 default function，但若 prior test 設了
        # 永久 stub，這裡也能保留它的「進入此 test 前的樣子」）。
        folder_paths.get_input_directory = saved[0] or _default_get_input_directory
        folder_paths.get_output_directory = saved[1] or _default_get_output_directory
        folder_paths.get_save_image_path = saved[2] or _default_get_save_image_path


@pytest.fixture(autouse=True)
def _chdir_to_plugin_root():
    """P3-10：每個 test 跑在 plugin root cwd 下。

    避免 `input/sample.mp4` 之類 relative default 因 pytest 啟動位置不同而出現 cwd
    依賴炸點；測試結束還原。同時穩定 sys.path-based module resolution（comfyui_MediaForge
    package 在哪取，受 cwd 影響）。
    """
    prev = os.getcwd()
    try:
        os.chdir(_PLUGIN_DIR)
    except OSError:
        # 例如 plugin 在 wsl-mount 而 native Windows cwd 不允許 chdir — silent skip
        yield
        return
    try:
        yield
    finally:
        try:
            os.chdir(prev)
        except OSError:
            pass


def unpack(r):
    """Unpack ComfyUI node return — tuple OR dict-with-result → plain tuple.

    File-producing nodes（BurnSubtitle / LoopVideo / etc）改成 ComfyUI canonical
    `{"ui": {...}, "result": (path,)}` return 後，test 端 unpack 需要兩邊都吃。
    Tuple-only 節點（如 ProbeMedia / AIConfig）走 r 不是 dict 那條路、不受影響。
    """
    return r["result"] if isinstance(r, dict) else r
