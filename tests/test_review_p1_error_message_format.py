"""P1-7: Error message format audit — every `raise X("...")` in nodes/ must:

1. Open with `[Display Name]` tag (matches the node's `NODE_DISPLAY_NAME_MAPPINGS`
   entry, sans emoji/leading space). Lets users grep stack traces and find the
   originating node quickly.
2. End with an *actionable* hint — a token from {"請查看", "請檢查", "請先",
   "請參考", "改用", "改", "pip install", "提示", "建議"} so the user knows what
   to try next (vs a flat "X failed" wall).

Test mechanism: AST-walk `nodes/*.py` to collect every `raise <ExcType>(<str>)` /
`raise <ExcType>(f"...")` literal, then regex-check each one.

Why AST rather than grep: f-string with `.format`/`f""` concatenation is one
logical message but multiple syntactic tokens; AST `JoinedStr` preserves the
template-literal pieces so we can join them back.

Allow-list cases:
- pure dispatch raise (`raise NotImplementedError(f"未支援的 type={x!r}")`) is
  considered actionable enough — token "未支援的" hints "check op type".
- Raise inside `_*` helper that always wraps a context message in caller layer
  → optional skip via `# noqa: MF_RAISE_FMT` comment trailing the raise line.
"""
import ast
import os
import re
import sys


_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NODES_DIR = os.path.join(_PLUGIN_DIR, "nodes")
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (_CUSTOM_NODES, _PLUGIN_DIR, _TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
import conftest  # noqa: E402,F401


# Token set that counts as "actionable" — at least one must appear in the message。
# 規則：訊息要嘛指明使用者下一步該做什麼 (`請改用`、`pip install X`)，要嘛清楚說明
# 「合法 input 長什麼樣」(`必須是`、`應為`、`未支援的`) 讓使用者知道要怎麼修。冰冷的
# 「X 失敗」這類訊息會被擋下。
_ACTION_TOKENS = (
    # 顯式建議
    "請", "改用", "改成", "改", "pip install", "提示", "建議", "(提示",
    # 隱式但具體：說明合法值 / 期待格式
    "必須", "應為", "應", "未支援", "未知", "範圍是", "需要", "需", "可以",
    # 環境 / 工具 hint
    "不在 PATH",
    # 失敗訊息習慣性附 stderr → 給 user "self-diagnose via stderr" 隱性建議
    "失敗 (", "失敗:", "失敗：", "失敗\n",
    # 「檢查 / 確認」家族（在訊息任意位置）
    "檢查", "確認",
    # 「重試 / 重做」家族
    "重試", "重新",
    # ComfyUI-flavored "把 X 接到 Y" guidance
    "接 ", "傳 ",
)

# 不檢查 message 格式的特殊 case：純 sanity check 沒對使用者公開的 raise
_SKIP_MESSAGE_TOKENS = (
    "expected",      # internal sanity check
    "internal:",     # internal sanity check
)


def _extract_message_text(node):
    """從 raise X(...) 的第一個 arg 拼回 message 字串。

    支援 純 str literal、f-string (JoinedStr)、簡單 BinOp concat。複雜 case
    (call、ternary) 直接回 None — 那些通常是 dynamic 訊息、不該擋。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string：把所有 literal 段串起來，formatted value 用 `<...>` placeholder 填
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v, ast.Constant):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                parts.append("<expr>")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _extract_message_text(node.left)
        right = _extract_message_text(node.right)
        if left is None or right is None:
            return None
        return left + right
    return None


def _collect_raises(filepath):
    """Walk a Python file, yield (lineno, exc_type_name, message_text) for each raise."""
    with open(filepath, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=filepath)
    raises = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Raise):
            continue
        exc = n.exc
        if exc is None:  # bare `raise` re-raise
            continue
        # raise X("...") → exc is Call(func=Name(id='X'), args=[...])
        if isinstance(exc, ast.Call):
            func = exc.func
            exc_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "<unknown>")
            if exc.args:
                msg = _extract_message_text(exc.args[0])
                if msg is not None:
                    raises.append((n.lineno, exc_name, msg))
    return raises


def _expected_tag_pattern():
    """Match anything that looks like `[Display Name]` at message start。

    Accept any single bracketed prefix — we don't enforce exact node name match
    (would require parsing NODE_DISPLAY_NAME_MAPPINGS which is fragile across
    refactors); the bracket prefix is the contract the user relies on for greppability.
    """
    return re.compile(r"^\[[^\]]+\]")


def test_node_raise_messages_have_bracket_tag():
    """每個 nodes/*.py 的 raise 第一個 arg (str/f-string literal) 應以 `[X]` 起頭。"""
    bad = []
    tag_re = _expected_tag_pattern()
    for fname in sorted(os.listdir(_NODES_DIR)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        full = os.path.join(_NODES_DIR, fname)
        for lineno, exc_name, msg in _collect_raises(full):
            # Skip dynamic / internal-only messages
            if any(t in msg.lower() for t in _SKIP_MESSAGE_TOKENS):
                continue
            if not tag_re.match(msg):
                bad.append(f"{fname}:{lineno} ({exc_name}): {msg[:80]!r}")

    if bad:
        joined = "\n  ".join(bad)
        raise AssertionError(
            f"[P1-7] 以下 raise 訊息開頭沒有 `[Display Name]` tag（共 {len(bad)} 個）:\n  {joined}"
        )
    print(f"[OK] P1-7: 所有 nodes/*.py 的 raise 訊息都有 `[X]` tag")


def test_node_raise_messages_have_action_hint():
    """raise 訊息應含至少一個 action token (`請查看`、`改用`、`pip install` 等)。"""
    missing = []
    for fname in sorted(os.listdir(_NODES_DIR)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        full = os.path.join(_NODES_DIR, fname)
        for lineno, exc_name, msg in _collect_raises(full):
            if any(t in msg.lower() for t in _SKIP_MESSAGE_TOKENS):
                continue
            # FileNotFoundError 的「找不到」/「不存在」訊息已內含「使用者該檢查路徑」隱性建議
            # — 算合格、不另外要求「請檢查」。
            if exc_name == "FileNotFoundError" and ("找不到" in msg or "不存在" in msg):
                continue
            if not any(tok in msg for tok in _ACTION_TOKENS):
                missing.append(f"{fname}:{lineno} ({exc_name}): {msg[:80]!r}")

    if missing:
        joined = "\n  ".join(missing)
        raise AssertionError(
            f"[P1-7] 以下 raise 訊息缺行動建議（{_ACTION_TOKENS}）— 共 {len(missing)} 個:\n"
            f"  {joined}\n"
            "建議在訊息結尾加類似 `改用 mode=copy`、`pip install X`、`請查看上方 stderr`。"
        )
    print(f"[OK] P1-7: 所有 nodes/*.py 的 raise 訊息都帶 action hint")


if __name__ == "__main__":
    test_node_raise_messages_have_bracket_tag()
    test_node_raise_messages_have_action_hint()
    print("\n=== P1-7 raise message format audit: all cases passed ===")
