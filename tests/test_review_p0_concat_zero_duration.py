"""[P0-1] MF_ConcatVideos transcode 模式：probe 失敗（duration=0）應直接 raise，
而非送 `apad=whole_dur=0` 給 FFmpeg（會炸 filter graph、訊息晦澀）。

執行：python tests/test_review_p0_concat_zero_duration.py
"""
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
sys.path.insert(0, _PLUGIN_DIR)
# 跑 standalone（非 pytest）時 conftest.py 不會自動載入 — 顯式 import 它觸發
# folder_paths stub 註冊到 sys.modules。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest  # noqa: F401, E402


def test_concat_transcode_raises_on_zero_duration():
    import comfyui_MediaForge.nodes.concat_videos as cv

    # Monkey-patch probe_video_duration → 回 0，模擬 probe 失敗
    saved_pd = cv.encode_tensor_to_tempfile  # noqa: F841 (sanity reference)

    # 我們不能 mock module-level import — 改 mock 它從 ..utils.ffmpeg 取的 helper
    import comfyui_MediaForge.utils.ffmpeg as ff
    saved_probe = ff.probe
    saved_dur = ff.probe_video_duration

    ff.probe = lambda p: {"streams": [{"codec_type": "audio"}]}
    ff.probe_video_duration = lambda p: 0.0  # 觸發 zero-duration branch

    try:
        node = cv.MF_ConcatVideos()
        # 兩個假檔 — 用 tempfile，確保 os.path.exists 過
        import tempfile
        f1 = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        f1.close()
        f2 = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        f2.close()
        try:
            try:
                node.concat(
                    video_paths=f"{f1.name}\n{f2.name}",
                    filename_prefix="MediaForge/_test_p0_1",
                    mode="transcode",
                    transition_sec=0.0,
                    transition_type="fade",
                    fps=30.0, width=320, height=180, crf=18,
                )
            except RuntimeError as e:
                # 預期：點名 duration 為 0 / probe 失敗
                msg = str(e)
                assert "duration" in msg.lower() or "probe" in msg.lower() or "時長" in msg, (
                    f"expected probe-related raise, got: {msg}"
                )
                print(f"[OK] P0-1: zero duration raise — {msg[:120]}")
                return
            raise AssertionError("expected RuntimeError when probe duration is 0, but call returned")
        finally:
            for p in (f1.name, f2.name):
                try:
                    os.unlink(p)
                except OSError:
                    pass
    finally:
        ff.probe = saved_probe
        ff.probe_video_duration = saved_dur


if __name__ == "__main__":
    test_concat_transcode_raises_on_zero_duration()
    print("\n=== Review P0-1: concat zero-duration guard passed ===")
