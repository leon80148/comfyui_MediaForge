import os

from ..utils.ffmpeg import ensure_ffmpeg, get_video_display_dims
from ..utils.ffmpeg import probe as ffprobe_data


class MF_ProbeMedia:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "media_path": ("STRING", {"default": "input/sample.mp4"}),
            },
        }

    RETURN_TYPES = ("FLOAT", "INT", "INT", "FLOAT", "STRING", "STRING")
    RETURN_NAMES = ("duration_sec", "width", "height", "fps", "video_codec", "audio_codec")
    FUNCTION = "probe"
    CATEGORY = "MediaForge/Analysis"

    def probe(self, media_path):
        if not ensure_ffmpeg():
            raise RuntimeError("[Probe Media] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(media_path):
            raise FileNotFoundError(f"[Probe Media] 找不到檔案：{media_path}")

        info = ffprobe_data(media_path)
        if not info:
            raise RuntimeError(f"[Probe Media] ffprobe 無法解析：{media_path}")

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

        fps = 0.0
        if v_stream and "r_frame_rate" in v_stream:
            try:
                num, den = v_stream["r_frame_rate"].split("/")
                fps = float(num) / float(den) if float(den) != 0 else 0.0
            except (ValueError, ZeroDivisionError):
                pass

        print(f"[Probe Media] {media_path}: {duration:.2f}s, {width}x{height}@{fps:.2f}fps, v={v_codec}, a={a_codec}")
        return (duration, width, height, fps, v_codec, a_codec)


NODE_CLASS_MAPPINGS = {"MF_ProbeMedia": MF_ProbeMedia}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ProbeMedia": "🔍 Probe Media"}
