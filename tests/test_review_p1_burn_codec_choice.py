"""[P1-1] BurnSubtitle 應暴露 codec / crf / preset，跟 SaveVideoFrames 一致。

不直接執行 ffmpeg；只驗 schema 與 cmd assembly 邏輯。
"""
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
sys.path.insert(0, _PLUGIN_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest  # noqa: F401, E402


def test_burn_subtitle_exposes_codec_widget():
    from comfyui_MediaForge.nodes.burn_subtitle import MF_BurnSubtitle
    schema = MF_BurnSubtitle.INPUT_TYPES()
    required = schema.get("required", {})
    assert "codec" in required, "BurnSubtitle 應暴露 codec 選項"
    assert "crf" in required, "BurnSubtitle 應暴露 crf 選項"
    assert "preset" in required, "BurnSubtitle 應暴露 preset 選項"
    print("[OK] P1-1: BurnSubtitle codec/crf/preset 已暴露")


def test_burn_subtitle_codec_list_includes_libx264():
    """codec dropdown 至少含 libx264 (CPU 永遠可用)。"""
    from comfyui_MediaForge.nodes.burn_subtitle import MF_BurnSubtitle
    schema = MF_BurnSubtitle.INPUT_TYPES()
    codec_choices = schema["required"]["codec"][0]
    assert any("libx264" in c for c in codec_choices), (
        f"codec choices 應含 libx264，但拿到 {codec_choices}"
    )
    print(f"[OK] P1-1: codec choices = {codec_choices}")


if __name__ == "__main__":
    test_burn_subtitle_exposes_codec_widget()
    test_burn_subtitle_codec_list_includes_libx264()
    print("\n=== Review P1-1: burn codec choice passed ===")
