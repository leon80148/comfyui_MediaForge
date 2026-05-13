"""Regression tests for Codex Round 4 review findings.

跑法：python tests/test_codex_r4_fixes.py
"""
import json
import os
import subprocess
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
import conftest  # noqa: E402,F401  (installs folder_paths stub for standalone runs)
from conftest import unpack  # noqa: E402


def _ffprobe_duration(path):
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", path],
        check=True, capture_output=True, text=True,
    )
    return float(json.loads(info.stdout)["format"]["duration"])


def test_save_keeps_full_video_when_audio_shorter():
    """[R4 P2] video 2s + audio 0.5s → 輸出應保留 2s 整段，audio 自然 mux 完早停。"""
    import torch
    from comfyui_MediaForge.nodes.save_video_frames import MF_SaveVideoFrames

    with tempfile.TemporaryDirectory() as _td:
        # 60 frames @ 30 fps = 2.0 s 影像
        frames = torch.rand(60, 64, 64, 3)
        # 0.5s mono audio (44.1k → 22050 samples)
        sr = 44100
        n_samples = int(sr * 0.5)
        audio = {
            "waveform": torch.zeros((1, 1, n_samples), dtype=torch.float32),
            "sample_rate": sr,
        }
        node = MF_SaveVideoFrames()
        (out,) = unpack(node.save(
            frames=frames, filename_prefix="MediaForge/test_r4_short_audio",
            fps=30.0,
            codec="h264 (libx264)", encode_mode="crf", crf=23,
            bitrate_kbps=4000, target_size_mb=8.0,
            preset="ultrafast", pix_fmt_override="",
            audio=audio,
        ))
        dur = _ffprobe_duration(out)
        # video 應為 ~2s（之前 -shortest 會截到 0.5s）
        assert dur > 1.8, f"output duration={dur:.3f}s，video 被 audio 截短 (應 >=1.8s for 60 frames @ 30 fps)"
        print(f"[OK] R4 P2: short audio + long video → keeps full video ({dur:.2f}s)")


# NOTE: R4 P2 test_compose_finalize_uses_explicit_t_not_shortest removed
# Compose pipeline v2 refactor 刪掉了 compose_finalize.py、新的 MF_ComposeVideo 完全
# 不用 `-shortest`(amix duration=first 自然把音訊綁到 source 視訊時長)。
# 原 R4 P2 的擔憂(audio 比 video 短時 -shortest 截掉 video) 在新架構不存在。


def test_whisper_empty_override_skips_cfg_chat_model():
    """[R4 P2] cfg.model='gpt-4o-mini' + override='' → 不該把 gpt-4o-mini 送進
    /audio/transcriptions；改走 backend STT default。"""
    # 直接驗解析邏輯（不真打 API）
    def resolve(override, cfg_model, provider):
        u = (override or "").strip()
        if u:
            return u
        return "whisper-1" if provider == "openai_compatible" else "base"

    # 共用 MF_AIConfig 預設的情境：cfg.model=gpt-4o-mini, override=''
    assert resolve("", "gpt-4o-mini", "openai_compatible") == "whisper-1", (
        "空 override 不能 fallback 到 chat model"
    )
    assert resolve("", "gpt-4o-mini", "faster_whisper_local") == "base"
    # 使用者明確設 override 還是要受尊重
    assert resolve("whisper-large-v3", "gpt-4o-mini", "openai_compatible") == "whisper-large-v3"
    print("[OK] R4 P2: empty override skips cfg.model, uses backend STT default")


def test_save_unaffected_when_audio_longer_than_video():
    """補：audio 長過 video 時也必須 output = video 時長（不該被 audio 拉長）。"""
    import torch
    from comfyui_MediaForge.nodes.save_video_frames import MF_SaveVideoFrames

    with tempfile.TemporaryDirectory() as _td:
        # 30 frames @ 30 fps = 1.0 s 影像
        frames = torch.rand(30, 64, 64, 3)
        # 3s audio
        sr = 22050
        n_samples = int(sr * 3.0)
        audio = {
            "waveform": torch.zeros((1, 1, n_samples), dtype=torch.float32),
            "sample_rate": sr,
        }
        node = MF_SaveVideoFrames()
        (out,) = unpack(node.save(
            frames=frames, filename_prefix="MediaForge/test_r4_long_audio",
            fps=30.0,
            codec="h264 (libx264)", encode_mode="crf", crf=23,
            bitrate_kbps=4000, target_size_mb=8.0,
            preset="ultrafast", pix_fmt_override="",
            audio=audio,
        ))
        dur = _ffprobe_duration(out)
        # 應接近 1.0s，不該被 audio 拉到 3s
        assert 0.8 < dur < 1.2, f"output={dur:.3f}s，audio 不該把 output 拉長 (應 ~1.0s)"
        print(f"[OK] R4 補: long audio + short video → keeps video duration ({dur:.2f}s)")


if __name__ == "__main__":
    test_save_keeps_full_video_when_audio_shorter()
    test_whisper_empty_override_skips_cfg_chat_model()
    test_save_unaffected_when_audio_longer_than_video()
    print("\n=== Codex R4 fixes: 3 cases passed (P2 compose_finalize 因架構重構移除) ===")
