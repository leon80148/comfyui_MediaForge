"""MF_ComposeStart — 啟動 Compose chain：吃 video_path，吐 MF_COMPOSE IR。

v2.1 ROADMAP Phase 4。Compose chain 起點，後續 ComposeOverlay* 節點吃此 IR。
"""
import os

from ..utils.compose_ir import ComposeIR
from ..utils.ffmpeg import ensure_ffmpeg, probe


class MF_ComposeStart:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                "target_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "target_width": ("INT", {"default": 1920, "min": 16, "max": 7680, "step": 2}),
                "target_height": ("INT", {"default": 1080, "min": 16, "max": 4320, "step": 2}),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE",)
    RETURN_NAMES = ("compose",)
    FUNCTION = "start"
    CATEGORY = "MediaForge/Compose"

    def start(self, video_path, target_fps, target_width, target_height):
        if not ensure_ffmpeg():
            raise RuntimeError("[Compose Start] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"[Compose Start] 找不到影片：{video_path}")

        info = probe(video_path)
        has_audio = bool(info and any(s.get("codec_type") == "audio" for s in info.get("streams", [])))

        ir = ComposeIR(
            target_fps=target_fps,
            target_width=target_width,
            target_height=target_height,
        )
        ir.add_video_input(video_path, is_main=True, has_audio=has_audio)
        print(f"[Compose Start] IR 初始化：{video_path} → {target_width}x{target_height}@{target_fps}fps, audio={has_audio}")
        return (ir,)


NODE_CLASS_MAPPINGS = {"MF_ComposeStart": MF_ComposeStart}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeStart": "🎬 Compose Start"}
