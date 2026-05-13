"""MF_ComposeStart — 啟動 Compose chain：吃 video_path，吐 MF_COMPOSE IR。

v2.1 ROADMAP Phase 4。Compose chain 起點，後續 ComposeOverlay* 節點吃此 IR。
"""
import os

from ..utils.compose_ir import ComposeIR
from ..utils.ffmpeg import ensure_ffmpeg, probe
from ..utils.video_io import encode_tensor_to_tempfile, mux_path_with_audio_dict


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
            "optional": {
                # In-memory chain: 連 frames 後改走 tensor → temp mp4 → Compose 流程
                # 注意：temp mp4 是 ComposeFinalize 之前的中介；ComposeFinalize 跑完才清掉
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("MF_COMPOSE",)
    RETURN_NAMES = ("compose",)
    FUNCTION = "start"
    CATEGORY = "MediaForge/Compose"

    def start(self, video_path, target_fps, target_width, target_height,
              frames=None, tensor_fps=30.0, audio=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Compose Start] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        # Dual-input: 連 frames 時寫 temp mp4，路徑掛進 IR.tmp_paths_to_cleanup —
        # ComposeFinalize 在 cleanup_paths loop 裡會自動 unlink（不需要本節點 try/finally）。
        # 為什麼不能本地 finally：temp 檔要活到下游 Finalize 跑完才能刪。
        tmp_source = None
        # Dual-input dispatch（與 BurnSubtitle / LoopVideo / TrimByRanges 對稱）：
        # path mode + audio dict 同時接時必須 pre-mux，否則 audio 在 ComposeFinalize 端 silent drop
        if frames is not None:
            source_path = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
            tmp_source = source_path
        else:
            if not os.path.exists(video_path):
                raise FileNotFoundError(f"[Compose Start] 找不到影片：{video_path}")
            if audio is not None:
                source_path = mux_path_with_audio_dict(video_path, audio)
                tmp_source = source_path
            else:
                source_path = video_path

        info = probe(source_path)
        has_audio = bool(info and any(s.get("codec_type") == "audio" for s in info.get("streams", [])))

        ir = ComposeIR(
            target_fps=target_fps,
            target_width=target_width,
            target_height=target_height,
        )
        ir.add_video_input(source_path, is_main=True, has_audio=has_audio)
        if tmp_source:
            ir.tmp_paths_to_cleanup.append(tmp_source)
        print(f"[Compose Start] IR 初始化：{source_path} → {target_width}x{target_height}@{target_fps}fps, audio={has_audio}")
        return (ir,)


NODE_CLASS_MAPPINGS = {"MF_ComposeStart": MF_ComposeStart}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeStart": "🎬 Compose Start"}
