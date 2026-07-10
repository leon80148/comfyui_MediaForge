import os

from ..utils.cache_key import path_fingerprint
from ..utils.ffmpeg import ensure_ffmpeg, get_video_display_dims
from ..utils.ffmpeg import probe as ffprobe_data
from ..utils.video_io import encode_tensor_to_tempfile


class MF_ProbeMedia:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "media_path": ("STRING", {"default": "input/sample.mp4"}),
            },
            "optional": {
                # In-memory chain: 連 frames 後改走 tensor → temp mp4 → probe 流程
                # (tensor 進來 probe 算冗餘 — 但保持跟其他 file-consumer node 一致 UX)
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("FLOAT", "INT", "INT", "FLOAT", "STRING", "STRING")
    RETURN_NAMES = ("duration_sec", "width", "height", "fps", "video_codec", "audio_codec")
    FUNCTION = "probe"
    CATEGORY = "MediaForge/Analysis"

    def probe(self, media_path, frames=None, tensor_fps=30.0, audio=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Probe Media] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        cleanup_tmp = None
        try:
            if frames is not None:
                source_path = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
                cleanup_tmp = source_path
            else:
                if not os.path.exists(media_path):
                    raise FileNotFoundError(f"[Probe Media] 找不到檔案：{media_path}")
                source_path = media_path

            info = ffprobe_data(source_path)
            if not info:
                raise RuntimeError(
                    f"[Probe Media] ffprobe 無法解析：{source_path}。"
                    "請檢查檔案是否損毀、或容器格式 ffprobe 是否認得"
                )

            duration = 0.0
            try:
                duration = float(info["format"]["duration"])
            except (KeyError, ValueError, TypeError):
                pass

            v_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
            a_stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)

            # R10 P2 fix：用 display dims（過 auto-rotate）回報，避免 portrait phone 影片
            # workflow 用 ProbeMedia 結果去算 Compose / Save 解析度時拿到錯的方向。
            if v_stream:
                width, height = get_video_display_dims(v_stream)
                v_codec = v_stream.get("codec_name", "")
            else:
                width, height, v_codec = 0, 0, ""
            a_codec = a_stream.get("codec_name", "") if a_stream else ""

            out_fps = 0.0
            if v_stream and "r_frame_rate" in v_stream:
                try:
                    num, den = v_stream["r_frame_rate"].split("/")
                    out_fps = float(num) / float(den) if float(den) != 0 else 0.0
                except (ValueError, ZeroDivisionError):
                    pass

            print(f"[Probe Media] {source_path}: {duration:.2f}s, {width}x{height}@{out_fps:.2f}fps, v={v_codec}, a={a_codec}")
            return (duration, width, height, out_fps, v_codec, a_codec)
        finally:
            if cleanup_tmp:
                try:
                    os.unlink(cleanup_tmp)
                except OSError:
                    pass

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # frames 接了 -> media_path 不會被讀，tensor 本身已參與 ComfyUI 原生 input
        # hash；否則同路徑換內容(mtime 變)要 invalidate cache(W1-13)。
        if kwargs.get("frames") is not None:
            return ""
        return path_fingerprint(kwargs.get("media_path", ""))


NODE_CLASS_MAPPINGS = {"MF_ProbeMedia": MF_ProbeMedia}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ProbeMedia": "🔍 Probe Media"}
