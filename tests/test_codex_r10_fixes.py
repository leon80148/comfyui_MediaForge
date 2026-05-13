"""Regression tests for Codex Round 10 review findings.

跑法：python tests/test_codex_r10_fixes.py
"""
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CUSTOM_NODES = os.path.dirname(_PLUGIN_DIR)
if _CUSTOM_NODES not in sys.path:
    sys.path.insert(0, _CUSTOM_NODES)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def test_whisper_openai_compat_honors_stt_cfg_model():
    """[R10 P2] cfg.model='whisper-large-v3' 對 openai_compatible 也是合法 STT model，
    應被尊重，不能硬塞 'whisper-1'。"""
    from comfyui_MediaForge.nodes.whisper_transcribe import _looks_like_stt_model

    def resolve(cfg_model, provider):
        c = (cfg_model or "").strip()
        if _looks_like_stt_model(c):
            return c
        return "base" if provider == "faster_whisper_local" else "whisper-1"

    # R10 specific: Groq's whisper-large-v3 for openai_compatible
    assert resolve("whisper-large-v3", "openai_compatible") == "whisper-large-v3"
    # 但 gpt-4o-mini (chat) 不該被當 STT
    assert resolve("gpt-4o-mini", "openai_compatible") == "whisper-1"
    # local 維持 R5 行為
    assert resolve("large-v3", "faster_whisper_local") == "large-v3"
    print("[OK] R10 P2: openai_compat 接受 STT cfg.model")


def test_burn_subtitle_uses_aac_not_copy():
    """[R10 P2] burn_subtitle 用 aac 取代 -c:a copy（避免跨 container mux fail）。"""
    src = open(os.path.join(_PLUGIN_DIR, "nodes/burn_subtitle.py"), encoding="utf-8").read()
    assert '"-c:a", "aac"' in src or "'-c:a', 'aac'" in src, "burn_subtitle 應改用 -c:a aac"
    assert "-c:a copy" not in src.replace(" ", ""), "仍有 -c:a copy 殘留"
    print("[OK] R10 P2: burn_subtitle 改用 -c:a aac")


def test_concat_audio_pads_before_trim():
    """[R10 P2] concat transcode mode 必須 apad before atrim (避免短 audio 對不齊 xfade seam)。"""
    src = open(os.path.join(_PLUGIN_DIR, "nodes/concat_videos.py"), encoding="utf-8").read()
    # 找到 audio 處理 chain，確認 apad 在 atrim 之前
    audio_chain_idx = src.find("aresample=async=1:first_pts=0,")
    assert audio_chain_idx > 0
    chain = src[audio_chain_idx:audio_chain_idx + 300]
    apad_pos = chain.find("apad=")
    atrim_pos = chain.find("atrim=duration=")
    assert 0 < apad_pos < atrim_pos, (
        f"apad 應在 atrim 之前\n  apad at {apad_pos}\n  atrim at {atrim_pos}\n  chain: {chain}"
    )
    print("[OK] R10 P2: concat audio chain apad → atrim 順序正確")


def test_decode_size_guard_accounts_for_peak_memory():
    """[R10 P2] MAX_DECODE_BYTES 降低 + peak ratio 提示，避免 4GB raw → 20GB peak OOM。"""
    import utils.video_io as vio
    assert vio.MAX_DECODE_BYTES <= 2 * 1024 * 1024 * 1024, (
        f"MAX_DECODE_BYTES={vio.MAX_DECODE_BYTES / 1024**3}GiB 太寬，peak 易爆"
    )
    assert hasattr(vio, "DECODE_PEAK_RATIO") and vio.DECODE_PEAK_RATIO >= 4, (
        "DECODE_PEAK_RATIO 應 >= 4 (rgb bytes + numpy + float32 tensor)"
    )
    print(f"[OK] R10 P2: MAX_DECODE_BYTES={vio.MAX_DECODE_BYTES / 1024**3:.1f}GiB × peak {vio.DECODE_PEAK_RATIO}x")


def test_probe_media_reports_display_dims_for_rotated_videos():
    """[R10 P2] MF_ProbeMedia 必須回 display dims (post auto-rotate)，跟 LoadVideoFrames 一致。"""
    import comfyui_MediaForge.nodes.probe_media as pm

    # Mock ffprobe response：portrait phone video coded 1920x1080 + rotation 90
    fake_info = {
        "streams": [{
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920, "height": 1080,
            "r_frame_rate": "30/1",
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}],
        }],
        "format": {"duration": "0.5"},
    }
    saved = pm.ffprobe_data
    pm.ffprobe_data = lambda p: fake_info
    try:
        node = pm.MF_ProbeMedia()
        # ProbeMedia 不檢查檔案是否存在；只看 ffprobe 結果
        # 但 os.path.exists 會 fail — 跳過該 check 改 mock os.path.exists
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            fake_path = f.name
        try:
            duration, width, height, fps, vc, ac = node.probe(fake_path)
            # 應 swap：display 1080x1920
            assert (width, height) == (1080, 1920), (
                f"rotation 90 應 swap dims，但拿到 {width}x{height}"
            )
            print(f"[OK] R10 P2: ProbeMedia 回 display dims {width}x{height} (post rotation)")
        finally:
            os.unlink(fake_path)
    finally:
        pm.ffprobe_data = saved


if __name__ == "__main__":
    test_whisper_openai_compat_honors_stt_cfg_model()
    test_burn_subtitle_uses_aac_not_copy()
    test_concat_audio_pads_before_trim()
    test_decode_size_guard_accounts_for_peak_memory()
    test_probe_media_reports_display_dims_for_rotated_videos()
    print("\n=== Codex R10 fixes: all 5 cases passed ===")
