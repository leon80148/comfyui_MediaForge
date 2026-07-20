"""MF_AIConfig — 輸出 provider-agnostic AI_CONFIG dict.

v2.1 ROADMAP Phase 5。`AI_CONFIG` 連線型別在 Phase 5 內仍 `experimental`、
保留向後不相容修改權利直到 Whisper / Translate 驗證需求。

合約（experimental, may change）：
    {
        'provider': 'openai_compatible' | 'faster_whisper_local',
        'base_url': str,
        'api_key': str,   # resolved — env: 間接引用在這裡就地展開，下游拿到即用值
        'model': str,
        'device': str,
    }

api_key 支援 `env:VARNAME` 間接引用（如 `env:OPENAI_API_KEY`）：config() 執行時
從環境變數解析真值。這是推薦用法 — widget 裡只存變數名，workflow JSON 匯出 /
分享 / 雲端同步都不會帶出 secret（明文 key 會原樣序列化進 graph state，canvas
遮罩擋不了這條路）。
"""
import os


class MF_AIConfig:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "provider": (["openai_compatible", "faster_whisper_local"],
                             {"default": "openai_compatible"}),
                "base_url": ("STRING", {"default": "https://api.openai.com/v1"}),
                # 建議填 env:VARNAME 間接引用（見 module docstring）；明文 key 會
                # 序列化進 workflow JSON、匯出分享即外洩
                "api_key": ("STRING", {"default": "", "placeholder": "env:OPENAI_API_KEY 或明文 key"}),
                "model": ("STRING", {"default": "gpt-4o-mini"}),
                # local faster_whisper 適用：'base' / 'small' / 'medium' / 'large-v3'
                "device": (["cpu", "cuda", "auto"], {"default": "auto"}),
            },
        }

    RETURN_TYPES = ("AI_CONFIG",)
    RETURN_NAMES = ("ai_config",)
    FUNCTION = "config"
    CATEGORY = "MediaForge/AI"

    def config(self, provider, base_url, api_key, model, device):
        """Build AI_CONFIG dict from widget values.

        `api_key` 在 print log 中只露前 4 字 + *** 以避免 screen-share / log 外流；
        下游 node 自行決定如何 sanitize。`env:VARNAME` 間接引用在這裡就地解析，
        secret 只存在於 runtime dict、不進 workflow JSON。
        """
        api_key = api_key.strip()
        if api_key.startswith("env:"):
            var_name = api_key[4:].strip()
            resolved = os.environ.get(var_name, "")
            if not resolved:
                raise ValueError(
                    f"[AI Config] api_key 指向環境變數 {var_name!r}（env: 間接引用），"
                    "但該變數不存在或為空。請先在啟動 ComfyUI 的環境設定它"
                    f"（例如 export {var_name}=sk-...），或改填明文 key"
                    "（注意：明文 key 會隨 workflow JSON 匯出，分享前請清空）。"
                )
            api_key = resolved
        cfg = {
            "provider": provider,
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "model": model,
            "device": device,
        }
        masked = (api_key[:4] + "***") if len(api_key) > 4 else "***"
        print(f"[AI Config] provider={provider} model={model} base={cfg['base_url']} key={masked}")
        return (cfg,)


NODE_CLASS_MAPPINGS = {"MF_AIConfig": MF_AIConfig}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_AIConfig": "⚙️ AI Config"}
