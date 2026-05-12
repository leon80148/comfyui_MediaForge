"""MF_SaveVideoFrames — IMAGE batch (+ AUDIO dict) → broadcast-grade encoded video file.

v2.1 ROADMAP Phase 2 foundation 節點。
整合 v1 計畫中的 SaveVideo / Convert / Resize / ChangeFps / Compress / ReplaceAudio 為單一輸出節點。
"""
import os

from ..utils.encoder import get_available_codecs
from ..utils.ffmpeg import ensure_ffmpeg
from ..utils.output_path import resolve_output_path
from ..utils.video_io import encode_tensor_to_video


# CRF mode 預設值（範圍依各 codec 不同，UI 以單一旗標展示後內部 clamp）
# 注意 *_nvenc 走 -cq 但 range 跟 -crf 一樣 0-51；ProRes 不用
CRF_DEFAULTS = {
    "libx264": 18, "libx265": 22, "libsvtav1": 30, "prores_ks": 0,
    "h264_nvenc": 23, "hevc_nvenc": 25, "av1_nvenc": 28,
}


class MF_SaveVideoFrames:
    @classmethod
    def INPUT_TYPES(s):
        codec_map = get_available_codecs()
        return {
            "required": {
                "frames": ("IMAGE",),
                "filename_prefix": ("STRING", {"default": "MediaForge/video"}),
                "fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "codec": (list(codec_map.keys()), {"default": "h264 (libx264)"}),
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
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "save"
    CATEGORY = "MediaForge/Video"

    def save(self, frames, filename_prefix, fps, codec, encode_mode, crf, bitrate_kbps,
             target_size_mb, preset, pix_fmt_override, audio=None):
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

        codec_id, default_pix_fmt = get_available_codecs()[codec]
        pix_fmt = pix_fmt_override.strip() or default_pix_fmt

        # Codec-aware container：prores_ks 必須走 .mov；其餘 .mp4
        # (取代舊 _ensure_compatible_container 的 post-hoc 修正，現在 ext 從 prefix resolve 時就決定)
        ext = ".mov" if codec_id == "prores_ks" else ".mp4"
        output_path = resolve_output_path(filename_prefix, ext)

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


NODE_CLASS_MAPPINGS = {"MF_SaveVideoFrames": MF_SaveVideoFrames}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SaveVideoFrames": "📤 Save Video Frames"}
