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
遮罩遮不了這條路）。

Exfiltration guard（Codex R2-1）：env: 能讀任意 process 環境變數，而 base_url 也
完全由 workflow 控制 — 惡意分享的 workflow 可以配 `env:AWS_SECRET_ACCESS_KEY` +
攻擊者 endpoint，讓下游節點把 secret 放進 Authorization header 外送。因此
env:-resolved key 只允許送往 trusted host：內建清單涵蓋 README recipes（OpenAI /
Groq）與本機推論（localhost 家族）；其他 host 需由使用者用環境變數擴充 —
`MF_AI_KEY_ALLOWED_HOSTS`（全域，逗號分隔 hostname）或
`MF_AI_KEY_ALLOWED_HOSTS_<VARNAME>`（綁定單一變數）。環境變數是 server-side
設定、workflow JSON 碰不到，所以這條 allowlist 攻擊者無法自帶。明文 key 不受限
（使用者自己打的 key 送去哪是使用者自己的決定；workflow 夾帶的明文 key 只會
外洩攻擊者自己的 key）。
"""
import os
from urllib.parse import urlparse


# env:-resolved keys may only be sent to these hosts (+ user-extended ones);
# see module docstring for the threat model.
_BUILTIN_ALLOWED_HOSTS = {
    "api.openai.com",
    "api.groq.com",
    "localhost",
    "127.0.0.1",
    "::1",
}


def _allowed_hosts_for(var_name):
    hosts = set(_BUILTIN_ALLOWED_HOSTS)
    for env_key in (f"MF_AI_KEY_ALLOWED_HOSTS_{var_name}", "MF_AI_KEY_ALLOWED_HOSTS"):
        raw = os.environ.get(env_key, "")
        hosts.update(h.strip().lower() for h in raw.split(",") if h.strip())
    return hosts


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
            # Exfiltration guard（見 module docstring）：只在 openai_compatible 檢查
            # — faster_whisper_local 是 in-process、base_url 根本不會被拿去發請求。
            if provider == "openai_compatible":
                host = (urlparse(base_url).hostname or "").lower()
                if host not in _allowed_hosts_for(var_name):
                    raise ValueError(
                        f"[AI Config] env: 解析出的 key 不允許送往 base_url host "
                        f"{host!r}。env: 間接引用的 key 只能送往信任清單內的 host"
                        f"（內建：{', '.join(sorted(_BUILTIN_ALLOWED_HOSTS))}），"
                        "防止惡意 workflow 用 env: 讀走任意環境變數再外送。"
                        "若這是你自己的 endpoint，請在啟動 ComfyUI 的環境加上 "
                        f"MF_AI_KEY_ALLOWED_HOSTS={host}（全域）或 "
                        f"MF_AI_KEY_ALLOWED_HOSTS_{var_name}={host}（只綁這個變數）。"
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
