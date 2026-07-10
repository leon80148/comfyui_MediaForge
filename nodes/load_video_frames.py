"""MF_LoadVideoFrames — FFmpeg → ComfyUI IMAGE batch + AUDIO dict + fps + metadata.

替代 VHS opencv decode 的廣 codec 涵蓋路徑 (AV1 / HEVC 10-bit / ProRes / VP9 等)。
v2.1 ROADMAP Phase 2 foundation 節點。
"""
import json
import os

from ..utils.cache_key import path_fingerprint
from ..utils.ffmpeg import ensure_ffmpeg
from ..utils.video_io import decode_audio_to_dict, decode_video_to_tensor


class MF_LoadVideoFrames:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                # 0 → 沿用原始 fps；>0 → 用 ffmpeg fps filter 重新採樣
                "target_fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 240.0, "step": 0.1}),
                # 0 → 不限制；>0 → 只解前 N frame，方便 preview 不爆記憶體
                "max_frames": ("INT", {"default": 0, "min": 0, "max": 100000, "step": 1}),
                "load_audio": ("BOOLEAN", {"default": True}),
                # 0 → 沿用原始 sample rate
                "audio_sr": ("INT", {"default": 0, "min": 0, "max": 192000, "step": 1000}),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "FLOAT", "INT", "INT", "INT", "STRING")
    RETURN_NAMES = ("frames", "audio", "fps", "width", "height", "frame_count", "metadata_json")
    FUNCTION = "load"
    CATEGORY = "MediaForge/Video"

    def load(self, video_path, target_fps, max_frames, load_audio, audio_sr):
        if not ensure_ffmpeg():
            raise RuntimeError("[Load Video Frames] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"[Load Video Frames] 找不到影片：{video_path}")

        frames, meta = decode_video_to_tensor(video_path, target_fps=target_fps, max_frames=max_frames)

        audio_dict = None
        if load_audio:
            audio_dict = decode_audio_to_dict(video_path, target_sr=audio_sr)
            if audio_dict is None:
                # 沒 audio stream → 輸出 None。
                # 為什麼不合成 1-sample 假音訊：MF_SaveVideoFrames 把任何 non-None AUDIO 視為真實，
                # 加 -shortest 後整段影片會被截到那 1 sample 的時長 (Codex Round 2 P2 finding)。
                print(f"[Load Video Frames] 注意：{video_path} 沒有 audio stream，audio 輸出為 None")

        print(
            f"[Load Video Frames] {video_path}: "
            f"{meta['n_frames']} frames @ {meta['effective_fps']:.2f}fps, "
            f"{meta['width']}x{meta['height']}, codec={meta['src_codec']}"
        )
        return (
            frames,
            audio_dict,
            float(meta["effective_fps"]),
            int(meta["width"]),
            int(meta["height"]),
            int(meta["n_frames"]),
            json.dumps(meta, ensure_ascii=False),
        )

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # 同路徑換內容(mtime 變)→ ComfyUI 需要 invalidate cache 重新 decode，
        # 對齊 core LoadImage 慣例(W1-13)。這個節點沒有 tensor 替代輸入，永遠讀 path。
        return path_fingerprint(kwargs.get("video_path", ""))


NODE_CLASS_MAPPINGS = {"MF_LoadVideoFrames": MF_LoadVideoFrames}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_LoadVideoFrames": "📥 Load Video Frames"}
