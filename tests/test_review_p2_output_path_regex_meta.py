"""P2-4: `resolve_output_path` counter behavior with regex-meta-char prefixes.

`resolve_output_path(prefix, ext)` scans the target dir with
`re.compile(rf"^{re.escape(filename)}_(\d+){re.escape(ext)}$")`. We already
`re.escape` so meta chars in `filename` should be literal — this test rigs that
contract:

- `foo.bar` (dot — regex any-char)
- `clip+v2` (plus — regex one-or-more)
- `(paren)` (parens — regex group)
- `[bracket]` (brackets — regex char class)
- `名稱中文` (unicode — counter logic must be Unicode-safe)

Each case writes 3 fake counter files, asks `resolve_output_path` for the next,
expects `_00004`.

也順便 cover P2-4 spec 的「subfolder + special chars」混搭。
"""
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (_CUSTOM_NODES, _PLUGIN_DIR, _TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
import conftest  # noqa: E402,F401


def _seed_existing(out_dir, base, ext, count=3):
    """Create N fake `base_<counter:05d><ext>` files under out_dir。"""
    os.makedirs(out_dir, exist_ok=True)
    for i in range(1, count + 1):
        p = os.path.join(out_dir, f"{base}_{i:05d}{ext}")
        open(p, "wb").close()


def _resolve_in_isolated_dir(tmp_path, monkeypatch_get_output, prefix, ext):
    """Patch folder_paths.get_output_directory to tmp_path and resolve。

    回傳 (resolved_path, counter_int)。Caller 應該 cleanup（用 pytest 的
    `tmp_path` fixture 已自動掃）。
    """
    from comfyui_MediaForge.utils.output_path import resolve_output_path
    monkeypatch_get_output(tmp_path)
    p = resolve_output_path(prefix, ext)
    # parse counter back from path (basename like `<filename>_00004.mp4`)
    import re
    m = re.search(r"_(\d{5})" + re.escape(ext) + r"$", p)
    assert m, f"沒抓到 counter pattern: {p}"
    return p, int(m.group(1))


def _patch_output_dir(tmp_path):
    """Helper: 把 folder_paths.get_output_directory 暫指到 tmp_path。

    P3-10 conftest fixture 會在 test teardown 還原 — 所以 test 結束自動清掉。
    """
    import folder_paths
    folder_paths.get_output_directory = lambda: str(tmp_path)


def test_dot_in_prefix_does_not_match_any_char(tmp_path):
    """`foo.bar` 的 dot 必須當 literal。同 dir 內有 `fooXbar_00099.mp4` 不該被當成
    counter 99（regex `.` 會誤 match X）。"""
    base = "foo.bar"
    out_dir = str(tmp_path)
    # 同名假檔（counter 1-3）
    _seed_existing(out_dir, base, ".mp4", count=3)
    # 干擾項：`fooXbar_00099.mp4` — 若 dot 被當 wildcard 會被認成 counter 99
    open(os.path.join(out_dir, "fooXbar_00099.mp4"), "wb").close()

    _patch_output_dir(tmp_path)
    from comfyui_MediaForge.utils.output_path import resolve_output_path
    p = resolve_output_path(base, ".mp4")
    assert p.endswith("foo.bar_00004.mp4"), (
        f"dot 被當 wildcard 了：counter 應為 00004（不該被 fooXbar_00099 干擾），實際 {p}"
    )
    print(f"[OK] P2-4: dot literal → {os.path.basename(p)}")


def test_plus_in_prefix_does_not_repeat(tmp_path):
    """`clip+v2` 的 + 必須當 literal。否則 regex 會把它解讀成「至少一個」量詞。"""
    base = "clip+v2"
    _seed_existing(str(tmp_path), base, ".mp4", count=3)
    _patch_output_dir(tmp_path)
    from comfyui_MediaForge.utils.output_path import resolve_output_path
    p = resolve_output_path(base, ".mp4")
    assert p.endswith("clip+v2_00004.mp4"), f"+ literal 失敗: {p}"
    print(f"[OK] P2-4: plus literal → {os.path.basename(p)}")


def test_parens_in_prefix_do_not_group(tmp_path):
    """`(paren)` 不可被 regex 當成 capture group。"""
    base = "(paren)"
    _seed_existing(str(tmp_path), base, ".mp4", count=3)
    _patch_output_dir(tmp_path)
    from comfyui_MediaForge.utils.output_path import resolve_output_path
    p = resolve_output_path(base, ".mp4")
    assert p.endswith("(paren)_00004.mp4"), f"parens literal 失敗: {p}"
    print(f"[OK] P2-4: parens literal → {os.path.basename(p)}")


def test_brackets_in_prefix_do_not_make_charclass(tmp_path):
    """`[bracket]` 不可被 regex 當成 character class。"""
    base = "[bracket]"
    _seed_existing(str(tmp_path), base, ".mp4", count=3)
    _patch_output_dir(tmp_path)
    from comfyui_MediaForge.utils.output_path import resolve_output_path
    p = resolve_output_path(base, ".mp4")
    assert p.endswith("[bracket]_00004.mp4"), f"brackets literal 失敗: {p}"
    print(f"[OK] P2-4: brackets literal → {os.path.basename(p)}")


def test_unicode_in_prefix(tmp_path):
    """Unicode (CJK) prefix 計數正常。"""
    base = "中文檔名"
    _seed_existing(str(tmp_path), base, ".mp4", count=3)
    _patch_output_dir(tmp_path)
    from comfyui_MediaForge.utils.output_path import resolve_output_path
    p = resolve_output_path(base, ".mp4")
    assert p.endswith("中文檔名_00004.mp4"), f"CJK literal 失敗: {p}"
    print(f"[OK] P2-4: CJK literal → {os.path.basename(p)}")


def test_subfolder_with_meta_chars(tmp_path):
    """`MediaForge/(meta).out` 既有 slash 切 subfolder、又有 meta chars。"""
    prefix = "subdir/foo.bar"
    sub = os.path.join(str(tmp_path), "subdir")
    _seed_existing(sub, "foo.bar", ".mp4", count=2)
    _patch_output_dir(tmp_path)
    from comfyui_MediaForge.utils.output_path import resolve_output_path
    p = resolve_output_path(prefix, ".mp4")
    assert p.endswith(os.path.join("subdir", "foo.bar_00003.mp4")), (
        f"subdir + meta: {p}"
    )
    print(f"[OK] P2-4: subfolder + meta → {os.path.basename(p)}")


def test_ext_independence_for_same_prefix(tmp_path):
    """同 prefix 不同 ext 互不影響 counter — `.mp4` 跟 `.mov` 各自獨立累。"""
    base = "audio.test"
    _seed_existing(str(tmp_path), base, ".mp4", count=5)  # mp4 to 5
    _seed_existing(str(tmp_path), base, ".mov", count=2)  # mov to 2
    _patch_output_dir(tmp_path)
    from comfyui_MediaForge.utils.output_path import resolve_output_path
    mp4 = resolve_output_path(base, ".mp4")
    mov = resolve_output_path(base, ".mov")
    assert mp4.endswith("audio.test_00006.mp4"), f"mp4: {mp4}"
    assert mov.endswith("audio.test_00003.mov"), f"mov: {mov}"
    print(f"[OK] P2-4: ext-independent counters mp4={os.path.basename(mp4)} mov={os.path.basename(mov)}")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        tp = Path(td)
        for case in [
            test_dot_in_prefix_does_not_match_any_char,
            test_plus_in_prefix_does_not_repeat,
            test_parens_in_prefix_do_not_group,
            test_brackets_in_prefix_do_not_make_charclass,
            test_unicode_in_prefix,
            test_subfolder_with_meta_chars,
            test_ext_independence_for_same_prefix,
        ]:
            sub = tp / case.__name__
            sub.mkdir()
            case(sub)
    print("\n=== P2-4 output_path regex meta safety: all cases passed ===")
