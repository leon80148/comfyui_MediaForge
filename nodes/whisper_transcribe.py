"""MF_WhisperTranscribe — AUDIO dict 或 path + AI_CONFIG → SRT_TEXT.

兩種 provider:
- openai_compatible：POST <base_url>/audio/transcriptions，回 SRT
- faster_whisper_local：lazy import faster_whisper、本機推論

雙模式選擇來自 AI_CONFIG.provider，使用者切 provider 就換 backend，不必動下游節點。
"""
import os
import tempfile

from ..utils.ffmpeg import ensure_ffmpeg


class MF_WhisperTranscribe:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "ai_config": ("AI_CONFIG",),
                "audio_path": ("STRING", {"default": "input/sample.mp4"}),
                "language": ("STRING", {"default": "zh"}),
                # STT 與 chat completion 必須用不同 model id；
                # 留空 → 用 ai_config.model；填值 → override 給 STT 端點使用。
                # 這是 Codex Round 2 P2 finding：原版讓 Whisper / Translate 共用 cfg.model，
                # OpenAI 端點 /audio/transcriptions 要 'whisper-1'、/chat/completions 要 'gpt-4o-mini'，
                # 共用會被 422 / 400 拒。
                # R3 P2 修正：預設留空、不寫死 'whisper-1'，否則會把 faster_whisper_local 後端
                # (期望 'base' / 'small' / 'large-v3') 的有效 cfg.model 蓋掉。
                "model_override": ("STRING", {"default": ""}),
            },
            "optional": {
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("srt_text",)
    FUNCTION = "transcribe"
    CATEGORY = "MediaForge/AI"

    def transcribe(self, ai_config, audio_path, language, model_override, audio=None):
        if not isinstance(ai_config, dict):
            raise ValueError(
                f"[Whisper Transcribe] ai_config 必須是 AI_CONFIG dict，"
                f"但拿到 {type(ai_config).__name__}（請接 MF_AIConfig 節點）"
            )
        provider = ai_config.get("provider")

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

            # Provider-aware fallback (R4/R5/R7 dialectic 最終形)：
            #   R4: openai_compatible + cfg.model='gpt-4o-mini' → STT default 'whisper-1'。
            #   R5: faster_whisper_local + cfg.model='large-v3' → 尊重 cfg.model。
            #   R7: faster_whisper_local + cfg.model='gpt-4o-mini' (因為 MF_AIConfig 預設是 chat
            #       model) → 應視為「未設」，走 STT default 'base'。
            # 偵測「chat-shaped」字串：gpt-, claude-, gemini-, llama-, qwen-。命中 → 當 unset。
            # R10 P2 fix：openai_compatible 也用 STT-shape 啟發式 — 否則 Groq's
            # 'whisper-large-v3' / 'gpt-4o-transcribe' 等真實 STT 模型會被忽略硬塞 'whisper-1'。
            # Symmetric with faster_whisper_local 行為。
            user_override = model_override.strip()
            cfg_model = (ai_config.get("model") or "").strip()
            if user_override:
                effective_model = user_override
            elif _looks_like_stt_model(cfg_model):
                effective_model = cfg_model
            elif provider == "faster_whisper_local":
                effective_model = "base"
            else:  # openai_compatible
                effective_model = "whisper-1"

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


def _extract_wav(path):
    import subprocess
    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="mf_whisper_")
    os.close(fd)
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        tmp,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True)
    if proc.returncode != 0:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        tail = "\n".join(proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-30:])
        raise RuntimeError(f"[Whisper Transcribe] FFmpeg 抽 WAV 失敗:\n{tail}")
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
        data = {
            "model": model,
            "response_format": "srt",
            "language": language,
        }
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=600)
    if not resp.ok:
        raise RuntimeError(
            f"[Whisper Transcribe] OpenAI-compatible 端點失敗 ({resp.status_code}): {resp.text[:500]}"
        )
    return resp.text


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

    # 自己組 SRT (faster-whisper segments → SRT)
    parts = []
    for i, seg in enumerate(segments, start=1):
        parts.append(f"{i}")
        parts.append(f"{_format_srt_time(seg.start)} --> {_format_srt_time(seg.end)}")
        parts.append(seg.text.strip())
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
