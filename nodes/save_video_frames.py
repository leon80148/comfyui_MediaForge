"""MF_SaveVideoFrames — IMAGE batch (+ AUDIO dict) → broadcast-grade encoded video file.

v2.1 ROADMAP Phase 2 foundation 節點。
整合 v1 計畫中的 SaveVideo / Convert / Resize / ChangeFps / Compress / ReplaceAudio 為單一輸出節點。
"""
import os

from ..utils.ffmpeg import ensure_ffmpeg
from ..utils.video_io import encode_tensor_to_video


# 預設 codec → (ffmpeg codec id, pix_fmt) mapping
CODEC_MAP = {
    "h264 (libx264)": ("libx264", "yuv420p"),
    "hevc (libx265)": ("libx265", "yuv420p"),
    "av1 (libsvtav1)": ("libsvtav1", "yuv420p"),
    "prores (prores_ks)": ("prores_ks", "yuv422p10le"),
}

# CRF mode 預設值（範圍依各 codec 不同，UI 以單一旗標展示後內部 clamp）
CRF_DEFAULTS = {"libx264": 18, "libx265": 22, "libsvtav1": 30, "prores_ks": 0}


class MF_SaveVideoFrames:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "frames": ("IMAGE",),
                "output_path": ("STRING", {"default": "/輸出/影片.mp4"}),
                "fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "codec": (list(CODEC_MAP.keys()), {"default": "h264 (libx264)"}),
                # CRF mode 為 v2.1 預設；bitrate>0 切到 bitrate mode；target_size_mb>0 切到 two-pass
                "encode_mode": (["crf", "bitrate", "target_size"], {"default": "crf"}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
                "bitrate_kbps": ("INT", {"default": 4000, "min": 100, "max": 200000, "step": 100}),
                "target_size_mb": ("FLOAT", {"default": 8.0, "min": 0.5, "max": 4000.0, "step": 0.5}),
                "preset": (
                    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"],
                    {"default": "medium"},
                ),
                # libx264/libx265 yuv420p 是相容性最高的；可選 yuv444p / yuv422p10le 給 ProRes
                "pix_fmt_override": ("STRING", {"default": ""}),
            },
            "optional": {
                "trigger": ("*", {}),
                "ai_config": ("AI_CONFIG", {}),
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "save"
    CATEGORY = "MediaForge/Video"

    def save(self, frames, output_path, fps, codec, encode_mode, crf, bitrate_kbps,
             target_size_mb, preset, pix_fmt_override,
             trigger=None, ai_config=None, audio=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Save Video Frames] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        if frames is None or frames.ndim != 4 or frames.shape[0] == 0:
            raise ValueError(
                f"[Save Video Frames] frames 必須是 [B,H,W,C] 非空 tensor，但拿到 "
                f"{tuple(frames.shape) if frames is not None else None}"
            )

        # 防呆：丟棄退化的 AUDIO dict（length<=1 sample 視為「上游無 audio」的假合成）。
        # 不這樣做的話 ffmpeg 加 -shortest 後整段 frames 被截到 1-sample 的長度。
        # 對應 Codex Round 2 P2 finding：silent-video roundtrip truncation。
        if audio is not None:
            w = audio.get("waveform") if isinstance(audio, dict) else None
            if w is None or (hasattr(w, "shape") and len(w.shape) >= 3 and w.shape[-1] <= 1):
                print("[Save Video Frames] 注意：audio dict 退化 (waveform 缺或 T<=1)，視為無音訊處理")
                audio = None

        codec_id, default_pix_fmt = CODEC_MAP[codec]
        pix_fmt = pix_fmt_override.strip() or default_pix_fmt

        # R8 P2 fix：ProRes 不支援 MP4 容器，FFmpeg 在 encode 才會炸。事前驗證 + 自動修正 .mp4 → .mov
        output_path = _ensure_compatible_container(output_path, codec_id, tag="Save Video Frames")

        # 確保輸出目錄存在
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        if encode_mode == "target_size":
            # target_size 模式 = 從目標大小反推 bitrate。
            # 公式：bitrate_bps = target_bytes * 8 / duration_sec - audio_bitrate
            duration = frames.shape[0] / fps
            target_bits = target_size_mb * 1024 * 1024 * 8
            audio_bps = 192_000 if audio is not None else 0
            video_bps = max(100_000, int(target_bits / duration) - audio_bps)
            self._encode(frames, output_path, fps, codec_id, pix_fmt, preset,
                         bitrate=f"{video_bps}", audio=audio)

        elif encode_mode == "bitrate":
            self._encode(frames, output_path, fps, codec_id, pix_fmt, preset,
                         bitrate=f"{bitrate_kbps}k", audio=audio)

        else:  # crf
            effective_crf = crf if codec_id != "prores_ks" else CRF_DEFAULTS[codec_id]
            self._encode(frames, output_path, fps, codec_id, pix_fmt, preset,
                         crf=effective_crf, audio=audio)

        print(f"[Save Video Frames] 輸出成功: {output_path}")
        return (output_path,)

    @staticmethod
    def _encode(frames, output_path, fps, codec_id, pix_fmt, preset,
                crf=None, bitrate=None, audio=None):
        extra = []
        # ProRes 用 -profile:v 而非 -crf；預設 422 HQ (profile=3)
        if codec_id == "prores_ks":
            extra.extend(["-profile:v", "3"])
        encode_tensor_to_video(
            frames,
            output_path,
            fps=fps,
            audio=audio,
            codec=codec_id,
            pix_fmt=pix_fmt,
            crf=crf if crf is not None else 18,
            bitrate=bitrate,
            preset=preset,
            extra_args=extra,
        )


def _ensure_compatible_container(output_path, codec_id, tag="Save Video Frames"):
    """Codec/container 相容性 guard。

    某些 codec 不能進某些 container：
    - prores_ks → MP4 不支援、應走 MOV
    （R8 P2 finding；FFmpeg 在 encode time 才報錯、使用者體驗差）
    """
    incompatible_containers = {
        "prores_ks": {".mp4", ".m4v"},
    }
    bad = incompatible_containers.get(codec_id)
    if not bad:
        return output_path
    ext = os.path.splitext(output_path)[1].lower()
    if ext in bad:
        new_path = os.path.splitext(output_path)[0] + ".mov"
        print(
            f"[{tag}] 注意：{codec_id} codec 不支援 {ext} 容器，自動改為 .mov："
            f"{output_path} → {new_path}"
        )
        return new_path
    return output_path


NODE_CLASS_MAPPINGS = {"MF_SaveVideoFrames": MF_SaveVideoFrames}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SaveVideoFrames": "📤 Save Video Frames"}
