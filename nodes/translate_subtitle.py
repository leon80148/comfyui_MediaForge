"""MF_TranslateSubtitle — SRT_TEXT + AI_CONFIG + target_lang → translated SRT_TEXT.

透過 OpenAI-compatible chat completion 端點。保留 SRT 時間戳，只翻譯 text 行。
"""
import re


# SRT block 解析：1 個 index + 1 行時間戳 + N 行文字 + 空行分隔
SRT_BLOCK_RE = re.compile(
    r"(\d+)\s*\n([0-9:,]+\s*-->\s*[0-9:,]+)\s*\n(.*?)(?=\n\n\d+\s*\n|\n*$)",
    re.DOTALL,
)


class MF_TranslateSubtitle:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "ai_config": ("AI_CONFIG",),
                "srt_text": ("STRING", {"default": "", "multiline": True}),
                "target_lang": ("STRING", {"default": "繁體中文"}),
                "system_prompt": (
                    "STRING",
                    {
                        "default": (
                            "你是專業字幕翻譯。將每一段翻成 {target_lang}，"
                            "保留語氣、不解釋、不加註解。每段對應一個輸出段。"
                        ),
                        "multiline": True,
                    },
                ),
                # 一次送 batch 行數 — 太大可能超 context；太小拖慢速度
                "batch_size": ("INT", {"default": 20, "min": 1, "max": 200, "step": 1}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("translated_srt",)
    FUNCTION = "translate"
    CATEGORY = "MediaForge/AI"

    def translate(self, ai_config, srt_text, target_lang, system_prompt, batch_size):
        if not isinstance(ai_config, dict):
            raise ValueError(
                f"[Translate Subtitle] ai_config 必須是 AI_CONFIG dict，"
                f"但拿到 {type(ai_config).__name__}"
            )
        # Translate 只走 OpenAI-compatible chat completion；不接受 faster_whisper_local
        # （那個是 STT 才有的本機模式）
        if ai_config.get("provider") != "openai_compatible":
            raise ValueError(
                f"[Translate Subtitle] 只支援 provider='openai_compatible'，"
                f"但 ai_config.provider={ai_config.get('provider')!r}。"
                "若需本機翻譯請接到支援 chat completion 的本機 LLM (e.g., llama.cpp server) "
                "並把 base_url 指過去、provider 仍填 'openai_compatible'。"
            )
        if not srt_text.strip():
            raise ValueError(
                "[Translate Subtitle] srt_text 為空。"
                "請接 MF_WhisperTranscribe 輸出或手動填 SRT 格式字串"
            )

        blocks = _parse_srt(srt_text)
        if not blocks:
            raise ValueError(
                "[Translate Subtitle] 無法解析 srt_text 為合法 SRT 格式。"
                "請檢查輸入是否為標準 SRT（含 idx / 時間戳 / 文本 三段）"
            )

        translated_lines = self._translate_in_batches(
            [b[2] for b in blocks], ai_config, target_lang, system_prompt, batch_size,
        )
        if len(translated_lines) != len(blocks):
            raise RuntimeError(
                f"[Translate Subtitle] 翻譯回傳段數 {len(translated_lines)} ≠ "
                f"原段數 {len(blocks)}，無法對齊時間戳。"
                "請縮小 batch_size 重試，或改用更穩定的 model"
            )

        out_parts = []
        for (idx, ts, _), translated in zip(blocks, translated_lines):
            out_parts.append(f"{idx}\n{ts}\n{translated.strip()}\n")
        result = "\n".join(out_parts)
        print(f"[Translate Subtitle] 完成（{len(blocks)} 段 → {target_lang}）")
        return (result,)

    @staticmethod
    def _translate_in_batches(texts, cfg, target_lang, system_prompt, batch_size):
        import json

        try:
            import requests
        except ImportError as e:
            raise RuntimeError(
                "[Translate Subtitle] 需要 `requests` 套件，請 `pip install requests`。"
            ) from e

        out = []
        url = f"{cfg['base_url']}/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if cfg.get("api_key"):
            headers["Authorization"] = f"Bearer {cfg['api_key']}"

        formatted_system = system_prompt.replace("{target_lang}", target_lang)

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            user_msg = "\n".join(f"[{j + 1}] {t}" for j, t in enumerate(batch))
            payload = {
                "model": cfg.get("model", "gpt-4o-mini"),
                "messages": [
                    {"role": "system", "content": formatted_system},
                    {
                        "role": "user",
                        "content": (
                            f"請翻譯以下 {len(batch)} 段字幕到 {target_lang}。\n"
                            "**只輸出**翻譯結果，每段一行、開頭保留 `[編號]`、不加任何解釋。\n\n"
                            f"{user_msg}"
                        ),
                    },
                ],
                "temperature": 0.2,
            }
            # allow_redirects=False：redirect 目的地不在 AIConfig 的 host allowlist
            # 保護範圍內，跟著跳轉可能把 Bearer key 帶去未驗證的 host（R3-1）
            resp = requests.post(url, headers=headers,
                                 data=json.dumps(payload), timeout=300,
                                 allow_redirects=False)
            # 只認 2xx — resp.ok 對所有 <400 都是 True（含 300/304 等非 redirect 型
            # 3xx），不跟 redirect 之後任何 3xx 都可能帶空 body（R4-2）
            if not (200 <= resp.status_code < 300):
                raise RuntimeError(
                    f"[Translate Subtitle] chat completion 失敗 ({resp.status_code}): "
                    f"{resp.text[:500]}"
                    + ("（端點回應 redirect — 基於 key 安全不跟隨，請直接填最終 base_url）"
                       if 300 <= resp.status_code < 400 else "")
                )
            data = resp.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as e:
                raise RuntimeError(
                    f"[Translate Subtitle] 回應格式非預期 OpenAI 格式：{data}"
                ) from e

            parsed = _parse_numbered_lines(content, expected=len(batch))
            missing = [n + 1 for n, t in enumerate(parsed) if not t.strip()]
            if missing:
                raise RuntimeError(
                    f"[Translate Subtitle] batch i={i}: AI 未回傳第 {missing} 段（"
                    f"共 {len(batch)} 段）。原始回應前 500 字：{content[:500]}\n"
                    "建議：降低 batch_size、提供更明確的 system_prompt、或重試。"
                )
            out.extend(parsed)
        return out


def _parse_srt(text):
    """SRT → list of (index, time_stamp, lines_text)."""
    blocks = []
    # 把行尾 \r 拿掉以利 regex
    clean = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # 補一個 trailing newline 讓 regex 抓最後一段
    if not clean.endswith("\n"):
        clean += "\n"
    for m in SRT_BLOCK_RE.finditer(clean + "\n"):
        idx = m.group(1)
        ts = m.group(2).strip()
        body = m.group(3).strip()
        blocks.append((idx, ts, body))
    return blocks


# 抓 [N] 開頭行；N 後可以有 . 或 ) 或 空白
NUM_LINE_RE = re.compile(r"^\s*\[?(\d+)\]?[.)\s]?\s*(.*)$")


def _parse_numbered_lines(text, *, expected):
    """從 AI 回應裡抽 [1] xxx / 1. xxx / 1) xxx 行；對齊到 expected 段。"""
    result = [""] * expected
    found = False
    for line in text.splitlines():
        if not line.strip():
            continue
        m = NUM_LINE_RE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= expected:
            result[n - 1] = m.group(2).strip()
            found = True
    if not found:
        # 退階：AI 沒照編號回，但回 N 行 → 直接對齊
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) == expected:
            print("[Translate Subtitle] 注意：AI 未照編號回，按行序對齊")
            return lines
    return result


NODE_CLASS_MAPPINGS = {"MF_TranslateSubtitle": MF_TranslateSubtitle}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_TranslateSubtitle": "🌐 Translate Subtitle"}
