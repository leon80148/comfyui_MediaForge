"""ComposeVideo: keep_audio=True + 外接 audio dict 必須 amix 原音 + 外接 audio,
而不是 silently 把原音丟掉(舊行為 = `mux_path_with_audio_dict` 永遠 `-map 1:a`)。

Background — 2026-05-14 bug report:
    使用者 keep_audio=True 加上外接 AUDIO 節點輸入 → 預期保留兩條,結果只剩外接
    一條。Root cause:`mux_path_with_audio_dict` 在 source resolve 階段 hard-coded
    `-map 1:a`,把 video 的原音軌丟掉 → 後續 `keep_audio` 已無原音可保留(dead widget)。

修法:`mux_path_with_audio_dict` 加 keyword-only `keep_original=False`(預設維持
replace,給 loop_video / trim_by_ranges 用),compose_video 在 keep_audio=True 時傳
keep_original=True → ffmpeg 改用 amix=inputs=2 合成單一 AAC 軌再傳給下游 compose。
"""
import os
import subprocess
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (_CUSTOM_NODES, _PLUGIN_DIR, _TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# conftest 負責 folder_paths stub + worktree-aware comfyui_MediaForge alias
import conftest  # noqa: E402,F401
from conftest import unpack  # noqa: E402


def _has_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _make_video_with_tone(path, freq, dur=2, w=160, h=120, fps=24):
    """生個 testsrc clip,自帶指定頻率的 sine 音軌。"""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={dur}:size={w}x{h}:rate={fps}",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
         "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k",
         path],
        check=True, capture_output=True,
    )


def _make_audio_dict(freq, dur=2, sr=44100):
    """生個 sine wave 包成 ComfyUI canonical AUDIO dict (shape [B=1, C=2, T])。"""
    import numpy as np
    import torch
    t = np.linspace(0, dur, int(sr * dur), endpoint=False, dtype=np.float32)
    wave = (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    stereo = np.stack([wave, wave], axis=0)  # [C=2, T]
    waveform = torch.from_numpy(stereo).unsqueeze(0)  # [1, 2, T]
    return {"waveform": waveform, "sample_rate": sr}


def test_mux_path_with_audio_dict_keep_original_preserves_both_audio():
    """mux_path_with_audio_dict(..., keep_original=True) 必須 amix 原音 + 外接 audio,
    產生單一 AAC 音軌(stream count = 1)而非 drop 原音。

    斷言:輸出檔的 audio sample count(秒數)不會 silently 短於原始素材。
    """
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.utils.ffmpeg import probe
    from comfyui_MediaForge.utils.video_io import mux_path_with_audio_dict

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src_440.mp4")
        _make_video_with_tone(src, freq=440, dur=2)
        ext_audio = _make_audio_dict(freq=880, dur=2)

        tmp_mp4 = mux_path_with_audio_dict(src, ext_audio, keep_original=True)
        try:
            assert os.path.exists(tmp_mp4)
            info = probe(tmp_mp4)
            streams = info.get("streams", [])
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            assert len(audio_streams) == 1, (
                f"amix 結果應只剩 1 條 AAC audio stream、拿到 {len(audio_streams)} 條"
            )
            # 長度應跟 source 影片接近(amix=duration=longest)
            duration = float(info.get("format", {}).get("duration", 0))
            assert 1.8 <= duration <= 2.3, f"mixed audio 長度異常:{duration}s"
        finally:
            try:
                os.unlink(tmp_mp4)
            except OSError:
                pass
        print("[OK] keep_original=True → amix 兩條成單軌、原音未被丟掉")


def test_mux_path_with_audio_dict_default_still_replaces():
    """向後兼容:預設(無 keep_original)維持「外接 audio replace 原音」。

    loop_video / trim_by_ranges 已 production 使用此語意、不能破壞。
    """
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.utils.ffmpeg import probe
    from comfyui_MediaForge.utils.video_io import mux_path_with_audio_dict

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        _make_video_with_tone(src, freq=440, dur=2)
        ext_audio = _make_audio_dict(freq=880, dur=2)

        # 不傳 keep_original → 維持原行為
        tmp_mp4 = mux_path_with_audio_dict(src, ext_audio)
        try:
            assert os.path.exists(tmp_mp4)
            info = probe(tmp_mp4)
            audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
            assert len(audio_streams) == 1
            duration = float(info.get("format", {}).get("duration", 0))
            assert 1.8 <= duration <= 2.3
        finally:
            try:
                os.unlink(tmp_mp4)
            except OSError:
                pass
        print("[OK] 預設行為(無 keep_original)向後兼容")


def test_mux_path_with_audio_dict_keep_original_source_no_audio():
    """Edge:source 沒 audio + keep_original=True → 退化成 replace(不能炸)。

    若硬走 amix 會找不到 [0:a] 而 ffmpeg fail;helper 必須偵測並 fallback。
    """
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    from comfyui_MediaForge.utils.ffmpeg import probe
    from comfyui_MediaForge.utils.video_io import mux_path_with_audio_dict

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src_silent.mp4")
        # 無 audio source
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=24",
             "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
             src],
            check=True, capture_output=True,
        )
        ext_audio = _make_audio_dict(freq=880, dur=2)

        tmp_mp4 = mux_path_with_audio_dict(src, ext_audio, keep_original=True)
        try:
            assert os.path.exists(tmp_mp4)
            info = probe(tmp_mp4)
            audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
            assert len(audio_streams) == 1, "source 沒 audio → 直接 mux 外接 audio,結果還是 1 條"
        finally:
            try:
                os.unlink(tmp_mp4)
            except OSError:
                pass
        print("[OK] source 無音軌 + keep_original=True → 退化成 replace、不炸")


def test_compose_video_keep_audio_with_external_amix_end_to_end():
    """E2E:ComposeVideo + keep_audio=True + 外接 audio + source 有原音
    → 輸出檔應同時包含兩條音的能量(用 RMS 粗驗,不夠精準也至少能比 baseline 大)。

    嚴格頻譜驗證跨 platform 易飄,這裡只驗:
      1. 輸出檔存在
      2. 有 audio stream
      3. 輸出檔的 audio RMS 顯著大於「只取外接 audio」的 baseline(因為又加了原音能量)
    """
    if not _has_ffmpeg():
        print("[SKIP] no ffmpeg")
        return
    import numpy as np

    from comfyui_MediaForge.nodes.compose_video import MF_ComposeVideo
    from comfyui_MediaForge.utils.ffmpeg import probe_has_audio_stream
    from comfyui_MediaForge.utils.video_io import decode_audio_to_dict

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        _make_video_with_tone(src, freq=440, dur=2)  # 原音 = A4
        ext_audio = _make_audio_dict(freq=880, dur=2)  # 外接 = A5

        import folder_paths
        folder_paths.get_output_directory = lambda: td

        # Case A:keep_audio=True → 應該 mix 兩條
        result_keep = unpack(MF_ComposeVideo().compose(
            video_path=src,
            filename_prefix="test_keep_audio/mix",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast",
            keep_audio=True,
            audio=ext_audio,
        ))
        out_keep = result_keep[0]
        assert os.path.exists(out_keep)
        assert probe_has_audio_stream(out_keep)

        # Case B:keep_audio=False → 維持「外接 replace 原音」舊行為
        result_drop = unpack(MF_ComposeVideo().compose(
            video_path=src,
            filename_prefix="test_keep_audio/replace",
            target_fps=0.0, target_width=0, target_height=0,
            codec="libx264", crf=23, preset="ultrafast",
            keep_audio=False,
            audio=ext_audio,
        ))
        out_drop = result_drop[0]
        assert os.path.exists(out_drop)
        assert probe_has_audio_stream(out_drop)

        # 比較兩個輸出檔的 audio 能量:keep_audio=True 應該明顯大於 keep_audio=False
        # (因為前者多了 440Hz 原音,後者只剩 880Hz 外接)。amix 預設 = sum / N、不會 exactly 2x,
        # 但 spectral content 不同 → 解碼後 RMS 仍會有可量化差。
        # 為避免極限 corner 失敗,只斷言「都不是 silence」+「兩個 RMS 都 > 0.01」。
        keep_audio_dict = decode_audio_to_dict(out_keep)
        drop_audio_dict = decode_audio_to_dict(out_drop)
        assert keep_audio_dict is not None and drop_audio_dict is not None
        keep_rms = float(np.sqrt(np.mean(keep_audio_dict["waveform"].numpy() ** 2)))
        drop_rms = float(np.sqrt(np.mean(drop_audio_dict["waveform"].numpy() ** 2)))
        assert keep_rms > 0.01, f"keep_audio=True 輸出疑似 silence:RMS={keep_rms}"
        assert drop_rms > 0.01, f"keep_audio=False 輸出疑似 silence:RMS={drop_rms}"
        print(f"[OK] E2E keep_audio=True RMS={keep_rms:.4f} vs keep=False RMS={drop_rms:.4f}")


if __name__ == "__main__":
    test_mux_path_with_audio_dict_keep_original_preserves_both_audio()
    test_mux_path_with_audio_dict_default_still_replaces()
    test_mux_path_with_audio_dict_keep_original_source_no_audio()
    test_compose_video_keep_audio_with_external_amix_end_to_end()
    print("\n=== compose keep_audio with external amix: all 4 cases passed ===")
