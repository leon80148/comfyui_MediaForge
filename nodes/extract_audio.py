"""MF_ExtractAudio — 從影片 / AUDIO dict 抽出音軌成獨立音訊檔。

Phase 6 首個 standalone Audio 節點：LoadVideoFrames(tensor) 只吐 AUDIO dict、沒有
「落地成檔案」的手段；本節點補上這塊，也是目前唯一能把 AUDIO dict 寫成實體音訊檔
的節點（write_audio_dict_to_wav 之前只在別的節點內部當 temp file 用）。
"""
import os

from ..utils.cache_key import path_fingerprint
from ..utils.ffmpeg import ensure_ffmpeg, probe, run_ffmpeg
from ..utils.output_path import output_path_to_ui_entry, resolve_output_path
from ..utils.video_io import write_audio_dict_to_wav


# copy 模式：來源 audio codec_name → 副檔名。pcm_* 系列另外用 startswith 判斷
# （pcm_s16le / pcm_f32le / ... 都是合法 wav payload，不必窮舉）。
# 找不到對映的 codec → raise，建議改走 transcode format。
_COPY_EXT_MAP = {
    "aac": ".m4a",
    "mp3": ".mp3",
    "opus": ".ogg",
    "vorbis": ".ogg",
    "flac": ".flac",
}

# transcode 模式：format dropdown display name → (ffmpeg -c:a codec, ext)
_TRANSCODE_MAP = {
    "mp3": ("libmp3lame", ".mp3"),
    "aac (m4a)": ("aac", ".m4a"),
    "wav (pcm_s16le)": ("pcm_s16le", ".wav"),
    "flac": ("flac", ".flac"),
}


class MF_ExtractAudio:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "audio_source": ("STRING", {"default": "input/sample.mp4"}),
                "format": (
                    ["copy", "mp3", "aac (m4a)", "wav (pcm_s16le)", "flac"],
                    {"default": "copy"},
                ),
                "filename_prefix": ("STRING", {"default": "MediaForge/audio"}),
            },
            "optional": {
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("audio_file_path",)
    FUNCTION = "extract"
    CATEGORY = "MediaForge/Audio"

    def extract(self, audio_source, format, filename_prefix, audio=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Extract Audio] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        cleanup_tmp = None
        try:
            if audio is not None:
                wav_path = write_audio_dict_to_wav(audio)
                cleanup_tmp = wav_path
                output_path = self._extract_from_audio_dict(wav_path, format, filename_prefix)
            else:
                if not os.path.exists(audio_source):
                    raise FileNotFoundError(f"[Extract Audio] 找不到輸入檔：{audio_source}")
                output_path = self._extract_from_path(audio_source, format, filename_prefix)
        finally:
            if cleanup_tmp:
                try:
                    os.unlink(cleanup_tmp)
                except OSError:
                    pass

        print(f"[Extract Audio] 輸出成功: {output_path}")
        # UI emit：跟 BurnSubtitle / TrimByRanges 同款 canonical 回傳，讓 /history 暴露檔案路徑。
        return {"ui": {"images": [output_path_to_ui_entry(output_path)]}, "result": (output_path,)}

    def _extract_from_path(self, audio_source, format, filename_prefix):
        audio_stream = _first_audio_stream(probe(audio_source))
        if audio_stream is None:
            raise RuntimeError(
                f"[Extract Audio] {audio_source} 沒有 audio stream，無法抽取音訊。"
                "請確認來源是含音軌的影片或音訊檔。"
            )

        if format == "copy":
            ext = self._copy_ext_for_codec(audio_stream.get("codec_name", ""))
            output_path = resolve_output_path(filename_prefix, ext)
            cmd = ["ffmpeg", "-y", "-i", audio_source, "-vn", "-c:a", "copy", output_path]
        else:
            codec, ext = _TRANSCODE_MAP[format]
            output_path = resolve_output_path(filename_prefix, ext)
            cmd = ["ffmpeg", "-y", "-i", audio_source, "-vn", "-c:a", codec, output_path]

        if not run_ffmpeg(cmd, tag="Extract Audio"):
            raise RuntimeError("[Extract Audio] FFmpeg 抽取音訊失敗，請查看上方 stderr 輸出。")
        return output_path

    @staticmethod
    def _copy_ext_for_codec(codec_name):
        if codec_name.startswith("pcm_"):
            return ".wav"
        ext = _COPY_EXT_MAP.get(codec_name)
        if ext is None:
            raise ValueError(
                f"[Extract Audio] format=copy 但來源 audio codec={codec_name!r} 不在支援清單"
                f"（{sorted(_COPY_EXT_MAP)} + pcm_*）。請改選 mp3 / aac / wav / flac 走 transcode。"
            )
        return ext

    def _extract_from_audio_dict(self, wav_path, format, filename_prefix):
        # AUDIO dict 沒有原生 codec 概念（write_audio_dict_to_wav 已固定吐 pcm_s16le
        # wav）；format=copy 對它而言沒有字面意義，視為 wav 輸出並告知使用者。
        if format == "copy":
            print("[Extract Audio] AUDIO dict 沒有原生 codec，format=copy 視為 wav 輸出。")
            codec, ext = _TRANSCODE_MAP["wav (pcm_s16le)"]
        else:
            codec, ext = _TRANSCODE_MAP[format]

        output_path = resolve_output_path(filename_prefix, ext)
        cmd = ["ffmpeg", "-y", "-i", wav_path, "-c:a", codec, output_path]
        if not run_ffmpeg(cmd, tag="Extract Audio"):
            raise RuntimeError("[Extract Audio] FFmpeg 抽取音訊失敗，請查看上方 stderr 輸出。")
        return output_path

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # audio 接了 -> audio_source 不會被讀，tensor 本身已參與 ComfyUI 原生 input
        # hash；否則同路徑換內容(mtime 變)要 invalidate cache(W1-13 慣例)。
        if kwargs.get("audio") is not None:
            return ""
        return path_fingerprint(kwargs.get("audio_source", ""))


def _first_audio_stream(info):
    if not info:
        return None
    return next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)


NODE_CLASS_MAPPINGS = {"MF_ExtractAudio": MF_ExtractAudio}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ExtractAudio": "🎧 Extract Audio"}
