"""P3-9: Unit tests for previously-untested nodes — SelectVideo / ConvertChinese / AIConfig.

跑法：python tests/test_review_p3_unit_untested_nodes.py

本檔不打外部 API、不跑 ffmpeg；純 schema + business-logic test。
覆蓋三個之前 zero-test-coverage 的節點，後續 schema drift / 邏輯改動會被立刻擋下。
"""
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _CUSTOM_NODES not in sys.path:
    sys.path.insert(0, _CUSTOM_NODES)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
import conftest  # noqa: E402,F401
from conftest import unpack  # noqa: E402


# =============================================================================
# MF_SelectVideo
# =============================================================================

def test_select_video_schema_includes_video_dropdown():
    from comfyui_MediaForge.nodes.select_video import MF_SelectVideo

    types = MF_SelectVideo.INPUT_TYPES()
    assert "video" in types["required"], "MF_SelectVideo 缺 video widget"
    spec = types["required"]["video"]
    assert isinstance(spec, tuple) and isinstance(spec[0], list), (
        f"video 應為 dropdown (list of choices)：{spec}"
    )
    assert len(spec[0]) >= 1, "video dropdown 應至少有 1 個選項（placeholder 也算）"
    print("[OK] P3-9: MF_SelectVideo schema 含 video dropdown")


def test_select_video_returns_full_path():
    """Sanity：dropdown 選一個檔，select() 回 absolute path。"""
    from comfyui_MediaForge.nodes import select_video as sv

    # 先在 stub 的 input_dir 放個假檔
    import folder_paths
    in_dir = folder_paths.get_input_directory()
    fake = os.path.join(in_dir, "test_p39_select.mp4")
    open(fake, "wb").close()

    try:
        node = sv.MF_SelectVideo()
        (path,) = node.select("test_p39_select.mp4")
        assert path.endswith("test_p39_select.mp4")
        assert os.path.isabs(path), f"應回 absolute path：{path}"
        print(f"[OK] P3-9: MF_SelectVideo.select() → {os.path.basename(path)}")
    finally:
        try:
            os.unlink(fake)
        except OSError:
            pass


def test_select_video_empty_placeholder_raises():
    """選到 placeholder（input/ 沒檔）應 raise FileNotFoundError 帶友善訊息。"""
    from comfyui_MediaForge.nodes import select_video as sv

    node = sv.MF_SelectVideo()
    try:
        node.select(sv._EMPTY_PLACEHOLDER)
    except FileNotFoundError as e:
        assert "input/" in str(e) or "目錄沒有" in str(e)
        print("[OK] P3-9: MF_SelectVideo placeholder → FileNotFoundError")
        return
    raise AssertionError("placeholder 應 raise，但沒有")


def test_select_video_is_changed_uses_mtime():
    """IS_CHANGED 應回 file mtime（讓 ComfyUI 在檔案內容改動時 invalidate cache）。"""
    from comfyui_MediaForge.nodes import select_video as sv

    import folder_paths
    in_dir = folder_paths.get_input_directory()
    fake = os.path.join(in_dir, "test_p39_ischanged.mp4")
    open(fake, "wb").close()
    try:
        m1 = sv.MF_SelectVideo.IS_CHANGED("test_p39_ischanged.mp4")
        # touch the file to bump mtime
        import time
        time.sleep(0.05)
        os.utime(fake, None)
        m2 = sv.MF_SelectVideo.IS_CHANGED("test_p39_ischanged.mp4")
        assert m2 >= m1, f"mtime 應遞增：m1={m1} m2={m2}"
        print(f"[OK] P3-9: IS_CHANGED 用 mtime ({m1:.3f} → {m2:.3f})")
    finally:
        try:
            os.unlink(fake)
        except OSError:
            pass


def test_select_video_is_changed_placeholder_returns_zero():
    from comfyui_MediaForge.nodes import select_video as sv
    assert sv.MF_SelectVideo.IS_CHANGED(sv._EMPTY_PLACEHOLDER) == 0.0
    print("[OK] P3-9: IS_CHANGED placeholder → 0.0")


# =============================================================================
# MF_AIConfig
# =============================================================================

def test_ai_config_schema_required_widgets():
    from comfyui_MediaForge.nodes.ai_config import MF_AIConfig
    types = MF_AIConfig.INPUT_TYPES()
    req = types["required"]
    for k in ("provider", "base_url", "api_key", "model", "device"):
        assert k in req, f"MF_AIConfig 缺 {k}"
    # provider 必須是兩個 backend 的 dropdown
    providers = req["provider"][0]
    assert "openai_compatible" in providers and "faster_whisper_local" in providers
    print("[OK] P3-9: MF_AIConfig schema 完整")


def test_ai_config_strips_trailing_slash_from_base_url():
    from comfyui_MediaForge.nodes.ai_config import MF_AIConfig
    node = MF_AIConfig()
    (cfg,) = node.config(
        provider="openai_compatible",
        base_url="https://api.openai.com/v1/",   # trailing slash
        api_key="sk-test", model="gpt-4o-mini",
        device="auto",
    )
    assert cfg["base_url"] == "https://api.openai.com/v1", (
        f"trailing slash 沒被 strip: {cfg['base_url']!r}"
    )
    print("[OK] P3-9: MF_AIConfig 自動 strip base_url trailing slash")


def test_ai_config_api_key_logged_masked():
    """[security] log 不該明文吐出 api_key — 只留前 4 字 + ***。

    透過 capture stdout 驗 log 訊息只含 mask 而非完整 key。
    """
    import io
    import contextlib
    from comfyui_MediaForge.nodes.ai_config import MF_AIConfig
    node = MF_AIConfig()

    long_key = "sk-supersecret-fake-key-1234567890"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        node.config(
            provider="openai_compatible", base_url="https://x.y",
            api_key=long_key, model="m", device="auto",
        )
    output = buf.getvalue()
    assert long_key not in output, (
        f"[SECURITY] api_key 完整字串出現在 log:\n{output}"
    )
    # 應該只露前 4 字
    assert "sk-s***" in output, f"log 應含 mask 'sk-s***'：{output}"
    print("[OK] P3-9 SECURITY: MF_AIConfig log 只露 4 字 mask")


# =============================================================================
# MF_ConvertChinese
# =============================================================================

def test_convert_chinese_schema_required_widgets():
    from comfyui_MediaForge.nodes.convert_chinese import MF_ConvertChinese
    types = MF_ConvertChinese.INPUT_TYPES()
    assert "profile" in types["required"]
    assert "text" in types["required"]
    profiles = types["required"]["profile"][0]
    assert any("s2twp" in p for p in profiles), "預設應含 s2twp 簡→繁台灣"
    print("[OK] P3-9: MF_ConvertChinese schema 含 profile / text")


def test_convert_chinese_text_empty_and_no_input_path_raises():
    from comfyui_MediaForge.nodes.convert_chinese import MF_ConvertChinese
    node = MF_ConvertChinese()
    try:
        node.convert(profile="s2twp (簡→繁台灣詞庫)", text="", input_path="")
    except ValueError as e:
        assert "text 為空" in str(e) or "input_path" in str(e)
        print("[OK] P3-9: ConvertChinese 空 input → ValueError")
        return
    raise AssertionError("空 input 應 raise ValueError")


def test_convert_chinese_input_path_not_found_raises():
    from comfyui_MediaForge.nodes.convert_chinese import MF_ConvertChinese
    node = MF_ConvertChinese()
    try:
        node.convert(
            profile="s2twp (簡→繁台灣詞庫)", text="",
            input_path="/nonexistent/path.srt",
        )
    except FileNotFoundError as e:
        assert "找不到" in str(e) or "not found" in str(e).lower()
        print("[OK] P3-9: ConvertChinese 缺檔 → FileNotFoundError")
        return
    raise AssertionError("缺檔應 raise FileNotFoundError")


def test_convert_chinese_basic_simplified_to_traditional():
    """需要 opencc 套件；沒裝就 SKIP。

    注意：這裡用 codepoint 比對而非 print 比對，避免 Windows cp950 console
    UnicodeEncodeError。s2t 行為：「计」(U+8BA1) → 「計」(U+8A08)。
    """
    try:
        import opencc  # noqa: F401
    except ImportError:
        print("[SKIP] opencc 未安裝，略過實際轉換驗證")
        return
    from comfyui_MediaForge.nodes.convert_chinese import MF_ConvertChinese
    node = MF_ConvertChinese()
    (converted, saved) = unpack(node.convert(
        profile="s2t   (簡→繁通用)",  # 通用版避免詞庫差異
        text="计算机软件",  # 计算机软件 (simplified)
    ))
    # 「計」(U+8A08) 應出現在 converted；「计」(U+8BA1) 不該出現
    assert "計" in converted, (
        f"s2t 沒把「计」轉成「計」: codepoints={[hex(ord(c)) for c in converted]}"
    )
    assert "计" not in converted, (
        f"s2t 結果仍含 simplified 「计」: codepoints={[hex(ord(c)) for c in converted]}"
    )
    assert saved == ""  # 沒設 filename_prefix → saved 空字串
    print(f"[OK] P3-9: ConvertChinese s2t 5 chars → {len(converted)} chars (codepoints verified)")


def test_convert_chinese_srt_heuristic_picks_srt_ext():
    """寫檔時：text 含 `-->` 應選 .srt 副檔名；否則 .txt。"""
    try:
        import opencc  # noqa: F401
    except ImportError:
        print("[SKIP] opencc 未安裝，略過 ext heuristic 驗證")
        return
    from comfyui_MediaForge.nodes.convert_chinese import MF_ConvertChinese
    node = MF_ConvertChinese()

    srt_text = "1\n00:00:01,000 --> 00:00:02,000\n你好\n"
    (_, saved_srt) = unpack(node.convert(
        profile="t2s   (繁通用→簡)",  # 順向、不會改變 ASCII timestamp
        text=srt_text,
        filename_prefix="MediaForge/test_p39_srt_heuristic",
    ))
    assert saved_srt.endswith(".srt"), f"含 --> 應選 .srt，但拿到 {saved_srt}"

    plain_text = "你好世界"
    (_, saved_txt) = unpack(node.convert(
        profile="t2s   (繁通用→簡)",
        text=plain_text,
        filename_prefix="MediaForge/test_p39_txt_heuristic",
    ))
    assert saved_txt.endswith(".txt"), f"無 --> 應選 .txt，但拿到 {saved_txt}"

    # 清理避免 counter 污染後續 run
    for p in (saved_srt, saved_txt):
        try:
            os.unlink(p)
        except OSError:
            pass
    print("[OK] P3-9: ConvertChinese SRT/TXT 副檔名 heuristic 正確")


def test_convert_chinese_input_path_with_explicit_text_takes_priority():
    """text 非空 → 即使 input_path 也填，仍以 text 為準（避免歧義）。

    避免 Windows cp950 console 的 UnicodeEncodeError — 用 codepoint 比對 + 不 print 中文字。
    """
    try:
        import opencc  # noqa: F401
    except ImportError:
        print("[SKIP] opencc 未安裝")
        return
    from comfyui_MediaForge.nodes.convert_chinese import MF_ConvertChinese
    node = MF_ConvertChinese()

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "should_be_ignored.txt")
        # 檔案內含「歲月」(traditional)；如果被讀到 → t2s converted 應含「岁」(simplified)
        with open(path, "w", encoding="utf-8") as f:
            f.write("歲月不饒人")

        # text 給 traditional「優先」(U+512A)；正確時 t2s 應吐 simplified「优先」(U+4F18)
        # 而不該出現「岁」(U+5C81)
        (converted, _) = unpack(node.convert(
            profile="t2s   (繁通用→簡)",
            text="優先用這個",
            input_path=path,
        ))
        # 「优」(U+4F18) 應出現（對應 text 的「優」），「岁」(U+5C81) 不該出現
        assert "优" in converted, (
            "text 應被使用：「優」應轉成「优」(U+4F18)；"
            f"converted codepoints={[hex(ord(c)) for c in converted]}"
        )
        assert "岁" not in converted, (
            "input_path 不該被讀（檔案含「歲」應轉成「岁」U+5C81，但出現了 → 表示讀檔了）"
        )
        print("[OK] P3-9: ConvertChinese text 優先於 input_path (codepoint verified)")


if __name__ == "__main__":
    test_select_video_schema_includes_video_dropdown()
    test_select_video_returns_full_path()
    test_select_video_empty_placeholder_raises()
    test_select_video_is_changed_uses_mtime()
    test_select_video_is_changed_placeholder_returns_zero()
    test_ai_config_schema_required_widgets()
    test_ai_config_strips_trailing_slash_from_base_url()
    test_ai_config_api_key_logged_masked()
    test_convert_chinese_schema_required_widgets()
    test_convert_chinese_text_empty_and_no_input_path_raises()
    test_convert_chinese_input_path_not_found_raises()
    test_convert_chinese_basic_simplified_to_traditional()
    test_convert_chinese_srt_heuristic_picks_srt_ext()
    test_convert_chinese_input_path_with_explicit_text_takes_priority()
    print("\n=== P3-9 unit tests for untested nodes: all cases passed ===")
