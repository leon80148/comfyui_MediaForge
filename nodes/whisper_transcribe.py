"""MF_WhisperTranscribe — AUDIO dict 或 path + AI_CONFIG → SRT_TEXT.

兩種 provider:
- openai_compatible：POST <base_url>/audio/transcriptions，要 verbose_json、自己組 SRT
- faster_whisper_local：lazy import faster_whisper、本機推論

雙模式選擇來自 AI_CONFIG.provider，使用者切 provider 就換 backend，不必動下游節點。
"""
import json
import os
import tempfile
from urllib.parse import urlparse

from ..utils.cache_key import path_fingerprint
from ..utils.ffmpeg import ensure_ffmpeg, resolve_ffmpeg_cmd


class MF_WhisperTranscribe:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "ai_config": ("AI_CONFIG",),
                "audio_path": ("STRING", {"default": "input/sample.mp4"}),
                "language": ("STRING", {"default": "zh"}),
            },
            "optional": {
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("srt_text",)
    FUNCTION = "transcribe"
    CATEGORY = "MediaForge/AI"

    def transcribe(self, ai_config, audio_path, language, audio=None):
        if not isinstance(ai_config, dict):
            raise ValueError(
                f"[Whisper Transcribe] ai_config 必須是 AI_CONFIG dict，"
                f"但拿到 {type(ai_config).__name__}（請接 MF_AIConfig 節點）"
            )
        provider = ai_config.get("provider")
        # 先解析模型：不支援語音辨識的服務要在抽音訊前就報錯
        effective_model = _resolve_stt_model(ai_config)
        print(f"[Whisper Transcribe] 使用模型：{effective_model}")

        cleanup = None
        try:
            if audio is not None:
                source = _audio_dict_to_tmp_wav(audio)
                cleanup = source
            else:
                if not os.path.exists(audio_path):
                    raise FileNotFoundError(f"[Whisper Transcribe] 找不到音訊：{audio_path}")
                # 抽純 wav 給 STT 用 — 避免大檔影片上傳整個 mp4
                if not ensure_ffmpeg():
                    raise RuntimeError("[Whisper Transcribe] FFmpeg 不在 PATH。")
                source = _extract_wav(audio_path)
                cleanup = source

            if provider == "openai_compatible":
                srt = _transcribe_openai_compatible(source, ai_config, language, effective_model)
            elif provider == "faster_whisper_local":
                srt = _transcribe_faster_whisper(source, ai_config, language, effective_model)
            else:
                raise ValueError(
                    f"[Whisper Transcribe] 未知 provider={provider!r}，"
                    "AI_CONFIG.provider 必須是 'openai_compatible' 或 'faster_whisper_local'"
                )
        finally:
            if cleanup:
                try:
                    os.unlink(cleanup)
                except OSError:
                    pass

        line_count = srt.count("\n\n")
        print(f"[Whisper Transcribe] 完成（{line_count} 段）")
        return (srt,)

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # C1 fix：ComfyUI IS_CHANGED 收到的 linked 輸入（audio）永遠是 None，用它
        # 判斷 tensor 模式是 dead code。無條件 fingerprint audio_path——tensor
        # 模式下沒被讀，fingerprint 它只是無害的多餘敏感度。
        return path_fingerprint(kwargs.get("audio_path", ""))


# faster-whisper 認得的 model 名稱 prefix (官方 huggingface tag)。其他字串如 'gpt-4o-mini' 視為非 STT。
_STT_MODEL_PREFIXES = (
    "tiny", "base", "small", "medium", "large",  # OpenAI Whisper sizes
    "whisper",                                    # 'whisper-1', 'whisper-large-v3', etc.
    "faster-whisper-",                            # community-built
    "distil-",                                    # distil-whisper variants
)
# 明確排除：chat / image / embed model 字首
_NON_STT_MODEL_PREFIXES = (
    "gpt-", "claude-", "gemini-", "llama-", "qwen-", "mistral-", "phi-",
    "text-embedding", "dall-e", "tts-",
)


def _looks_like_stt_model(name: str) -> bool:
    """Heuristic：cfg.model 是否像 faster-whisper / OpenAI STT 模型名。"""
    if not name:
        return False
    low = name.lower()
    if any(low.startswith(p) for p in _NON_STT_MODEL_PREFIXES):
        return False
    return any(low.startswith(p) for p in _STT_MODEL_PREFIXES)


# web/ai_config_services.js 標成僅翻譯的雲端服務：沒有可用的語音辨識端點。
_HOSTS_WITHOUT_STT = {"openrouter.ai", "generativelanguage.googleapis.com"}
_STT_FALLBACK_BY_HOST = {"api.groq.com": "whisper-large-v3-turbo"}


def _resolve_stt_model(cfg):
    """決定實際送出的語音模型。

    STT 跟 chat completion 用不同 model id (Codex R2 P2 finding)：同一個 AIConfig 若要
    餵 Whisper + Translate，cfg.model 不能兩端通吃。不像 STT id 就退回該服務的語音模型。
    """
    provider = cfg.get("provider")
    host = (urlparse(cfg.get("base_url") or "").hostname or "").lower()
    if provider == "openai_compatible" and host in _HOSTS_WITHOUT_STT:
        raise ValueError(
            f"[Whisper Transcribe] {host} 不支援語音辨識，只能用來翻譯。"
            "請在 AI Config 改選 OpenAI、Groq 或 faster-whisper。"
        )
    cfg_model = (cfg.get("model") or "").strip()
    if _looks_like_stt_model(cfg_model):
        return cfg_model
    if provider == "faster_whisper_local":
        return "base"
    return _STT_FALLBACK_BY_HOST.get(host, "whisper-1")


def _input_has_audio_stream(path):
    """ffprobe 確認 input 至少有一個 audio stream。

    為什麼：抽 WAV 前先 probe 比直接讓 ffmpeg 失敗給更好的 UX —— 當 input 是純 video
    或 SRT 之類沒 audio 的檔，ffmpeg 只會吐 generic "Output file does not contain
    any stream"，不像「input 缺 audio stream」那麼直白。
    """
    from ..utils.ffmpeg import probe
    info = probe(path)
    if info is None:
        return False
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


# WAV header (RIFF/fmt/data) 通常 44 bytes；任何低於這個量就一定不是有效音訊。
_MIN_VALID_WAV_BYTES = 256


def _extract_wav(path):
    import subprocess
    if not _input_has_audio_stream(path):
        raise RuntimeError(
            f"[Whisper Transcribe] {path} 沒有 audio stream，無法做語音辨識。"
            "請確認來源是含音軌的影片或音訊檔；若想轉一段純文字，請改用 MF_TranslateSubtitle。"
        )

    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="mf_whisper_")
    os.close(fd)
    cmd = resolve_ffmpeg_cmd([
        "ffmpeg", "-y", "-v", "error", "-i", path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        tmp,
    ])
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        tail = "\n".join(proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-30:])
        raise RuntimeError(f"[Whisper Transcribe] FFmpeg 抽 WAV 失敗:\n{tail}")

    # Defense-in-depth：舊版 ffmpeg / static-ffmpeg fallback 偶爾會吐 0-byte WAV
    # 但 returncode=0；不擋下，下游 faster-whisper 會 silent return 0 段、產出空 SRT。
    try:
        size = os.path.getsize(tmp)
    except OSError:
        size = 0
    if size < _MIN_VALID_WAV_BYTES:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError(
            f"[Whisper Transcribe] FFmpeg 抽 WAV 後 size={size} byte 過小（無效音訊），"
            f"input 可能是 silent video 或非預期格式。請改傳有音訊的檔案、或先用 MF_ProbeMedia 確認 audio stream 存在"
        )
    return tmp


def _audio_dict_to_tmp_wav(audio_dict):
    """AUDIO dict → 16kHz mono wav for Whisper.

    OpenAI Whisper 與 faster-whisper 都偏好 16kHz mono；高 sr / 多聲道會被內部重採樣，
    但有時 (尤其 faster-whisper 用 CTranslate2) 會誤判長度。我們在 client side 一次處理乾淨。
    """
    import wave
    import numpy as np

    waveform = audio_dict.get("waveform")
    sr = int(audio_dict.get("sample_rate") or 0)
    if waveform is None or sr <= 0:
        raise ValueError(
            "[Whisper Transcribe] AUDIO dict 缺 waveform / sample_rate "
            "（需符合 ComfyUI canonical {'waveform': Tensor[B,C,T], 'sample_rate': int}）"
        )
    wav = waveform[0].detach().cpu().clamp(-1.0, 1.0).numpy()  # [C, T]
    # downmix to mono
    if wav.shape[0] > 1:
        mono = wav.mean(axis=0)
    else:
        mono = wav[0]

    target_sr = 16000
    if sr != target_sr:
        # 線性 resample；avoid scipy 依賴。對 STT 已足夠（Whisper 自己 25ms hop）
        t_old = np.linspace(0.0, 1.0, num=len(mono), endpoint=False, dtype=np.float64)
        n_new = max(1, int(round(len(mono) * target_sr / sr)))
        t_new = np.linspace(0.0, 1.0, num=n_new, endpoint=False, dtype=np.float64)
        mono = np.interp(t_new, t_old, mono).astype(np.float32)

    pcm16 = (mono * 32767.0).round().astype(np.int16)

    fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="mf_whisper_in_")
    os.close(fd)
    with wave.open(tmp_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(target_sr)
        w.writeframes(pcm16.tobytes())
    return tmp_path


def _transcribe_openai_compatible(wav_path, cfg, language, model):
    # lazy import — 避免 plugin 啟動時硬依賴 requests
    try:
        import requests
    except ImportError as e:
        raise RuntimeError(
            "[Whisper Transcribe] openai_compatible 模式需要 `requests` 套件。"
            "請 `pip install requests`。"
        ) from e

    url = f"{cfg['base_url']}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg.get("api_key") else {}
    with open(wav_path, "rb") as f:
        files = {"file": (os.path.basename(wav_path), f, "audio/wav")}
        # verbose_json 而不是 srt：Groq 不接受 srt；OpenAI whisper-1 和 Groq 都回 segments
        data = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
            "language": language,
        }
        # allow_redirects=False：redirect 目的地不在 AIConfig 的 host allowlist
        # 保護範圍內，跟著跳轉可能把 Bearer key 帶去未驗證的 host（R3-1）
        resp = requests.post(
            url, headers=headers, files=files, data=data,
            timeout=600, allow_redirects=False,
        )
    # 只認 2xx — resp.ok 對所有 <400 都是 True（含 300/304 等非 redirect 型 3xx），
    # 不跟 redirect 之後任何 3xx 都可能帶空 body，當成功收會變 silent failure（R4-2）
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(
            f"[Whisper Transcribe] OpenAI-compatible 端點失敗 ({resp.status_code}): {resp.text[:500]}"
            + ("（端點回應 redirect — 基於 key 安全不跟隨，請直接填最終 base_url）"
               if 300 <= resp.status_code < 400 else "")
        )
    # R5-4:不用 resp.text — 端點回應不帶 charset 時 requests 會退 ISO-8859-1 把 UTF-8
    # 中文解成亂碼。顯式解 UTF-8;utf-8-sig 順手吃 BOM。
    body = resp.content.decode("utf-8-sig", errors="replace")
    try:
        segments = json.loads(body)["segments"]
        return _segments_to_srt((s["start"], s["end"], s["text"]) for s in segments)
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(
            f"[Whisper Transcribe] 模型 {model} 的回應沒有可用的時間軸（segments），"
            f"無法產生字幕。回應開頭：{body[:200]}"
        ) from e


def _transcribe_faster_whisper(wav_path, cfg, language, model):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "[Whisper Transcribe] faster_whisper_local 模式需要 `faster-whisper` 套件。"
            "請 `pip install faster-whisper`。"
        ) from e

    model_size = model or cfg.get("model", "base")
    device = cfg.get("device", "auto")
    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"

    model = WhisperModel(model_size, device=device, compute_type="auto")
    segments, _info = model.transcribe(wav_path, language=language or None)

    return _segments_to_srt((seg.start, seg.end, seg.text) for seg in segments)


def _segments_to_srt(segments):
    """(start_sec, end_sec, text) 序列 → SRT 字串。"""
    parts = []
    for i, (start, end, text) in enumerate(segments, start=1):
        parts.append(f"{i}")
        parts.append(f"{_format_srt_time(start)} --> {_format_srt_time(end)}")
        parts.append(text.strip())
        parts.append("")
    return "\n".join(parts)


def _format_srt_time(t):
    if t < 0:
        t = 0
    ms = int(round(t * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


NODE_CLASS_MAPPINGS = {"MF_WhisperTranscribe": MF_WhisperTranscribe}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_WhisperTranscribe": "🗣️ Whisper Transcribe"}
