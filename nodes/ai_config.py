"""MF_AIConfig — 輸出 provider-agnostic AI_CONFIG dict.

v2.1 ROADMAP Phase 5。`AI_CONFIG` 連線型別在 Phase 5 內仍 `experimental`、
保留向後不相容修改權利直到 Whisper / Translate 驗證需求。

合約（experimental, may change）：
    {
        'provider': 'openai_compatible' | 'faster_whisper_local',
        'base_url': str,
        'api_key': str,
        'model': str,
        'device': str,
    }
"""


class MF_AIConfig:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "provider": (["openai_compatible", "faster_whisper_local"],
                             {"default": "openai_compatible"}),
                "base_url": ("STRING", {"default": "https://api.openai.com/v1"}),
                "api_key": ("STRING", {"default": ""}),
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
        下游 node 自行決定如何 sanitize。
        """
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
