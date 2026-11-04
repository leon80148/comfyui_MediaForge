// Presets behind the 服務 / 模型 dropdowns on MF_AIConfig (web/ai_config_ui.js). No ComfyUI imports,
// so tests/js can run this directly.
//
// A preset is only a shortcut: it writes plain values into the node's own provider / base_url /
// api_key / model widgets, and Python never sees a preset name. Models retire often (checked against
// each vendor's docs on 2026-11-05), and a model dropped from this list just shows as 自訂… on old
// workflows, with its saved value untouched.
//
// Rules tests/test_ai_config.py checks against Python: every baseUrl host is in
// _BUILTIN_ALLOWED_HOSTS (nodes/ai_config.py), and each model's kind agrees with
// _looks_like_stt_model (nodes/whisper_transcribe.py), which would otherwise swap it out.
//
// The first model is what a service starts on. It is a translation model where one exists:
// MF_WhisperTranscribe falls back to the service's speech model by itself, so one config can feed
// both Whisper and Translate, but MF_TranslateSubtitle has no such fallback.

export const CUSTOM_MODEL = "__custom__";
export const CUSTOM_SERVICE = "自訂";

const OPENAI_COMPATIBLE = "openai_compatible";
const FASTER_WHISPER = "faster_whisper_local";
const DOWNLOADED_HINT = "模型名稱（填已下載的模型）";

const speech = (id, note) => ({ id, kind: "speech", note });
const translate = (id) => ({ id, kind: "translate" });

export const SERVICES = [
    {
        name: "OpenAI", provider: OPENAI_COMPATIBLE, baseUrl: "https://api.openai.com/v1", apiKey: "env:OPENAI_API_KEY",
        models: [translate("gpt-6-luna"), translate("gpt-6.1-terra"), speech("whisper-1", "2027-02 停用")],
    },
    {
        name: "Groq", provider: OPENAI_COMPATIBLE, baseUrl: "https://api.groq.com/openai/v1", apiKey: "env:GROQ_API_KEY",
        models: [translate("llama-3.3-70b-versatile"), translate("llama-3.1-8b-instant"),
            speech("whisper-large-v3-turbo"), speech("whisper-large-v3")],
    },
    {
        name: "faster-whisper（僅語音辨識）", provider: FASTER_WHISPER, apiKey: "",
        models: [speech("large-v3"), speech("large-v3-turbo"), speech("medium"), speech("small"), speech("base")],
    },
    {
        name: "Ollama（僅翻譯）", provider: OPENAI_COMPATIBLE, baseUrl: "http://localhost:11434/v1", apiKey: "ollama",
        models: [], modelHint: DOWNLOADED_HINT,
    },
    {
        name: "LM Studio（僅翻譯）", provider: OPENAI_COMPATIBLE, baseUrl: "http://localhost:1234/v1", apiKey: "lm-studio",
        models: [], modelHint: DOWNLOADED_HINT,
    },
    {
        name: "OpenRouter（僅翻譯）", provider: OPENAI_COMPATIBLE, baseUrl: "https://openrouter.ai/api/v1", apiKey: "env:OPENROUTER_API_KEY",
        models: [translate("~openai/gpt-sol-latest"), translate("~anthropic/claude-sonnet-latest")],
    },
    {
        name: "Gemini（僅翻譯）", provider: OPENAI_COMPATIBLE, baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai", apiKey: "env:GEMINI_API_KEY",
        models: [translate("gemini-3.8-flash")],
    },
    { name: CUSTOM_SERVICE, models: [] },
];

const KIND_LABEL = { speech: "語音辨識", translate: "翻譯" };
const isCustom = (service) => service.name === CUSTOM_SERVICE;
const normalizeUrl = (url) => (url ?? "").trim().replace(/\/+$/, "");

export function serviceByName(name) {
    return SERVICES.find((s) => s.name === name) ?? SERVICES.at(-1);
}

// Saved workflows hold no service name, so it is worked out from the values they do hold.
export function serviceFromValues(provider, baseUrl) {
    const url = normalizeUrl(baseUrl);
    const match = provider === FASTER_WHISPER
        ? SERVICES.find((s) => s.provider === FASTER_WHISPER)
        : SERVICES.find((s) => s.provider === provider && s.baseUrl === url);
    return match ?? serviceByName(CUSTOM_SERVICE);
}

// The key is always replaced: a plaintext key belongs to the service it was typed for and must not be
// sent to the next one's host, or stay hidden in the saved workflow when the field is not shown.
export function serviceWrites(service) {
    if (isCustom(service)) return {};
    const writes = { provider: service.provider, api_key: service.apiKey, model: service.models[0]?.id ?? "" };
    if (service.baseUrl) writes.base_url = service.baseUrl;
    return writes;
}

export function modelOptions(service) {
    return [...service.models.map((m) => m.id), CUSTOM_MODEL];
}

export function modelChoice(service, model) {
    return service.models.some((m) => m.id === model) ? model : CUSTOM_MODEL;
}

export function modelLabel(service, value) {
    if (value === CUSTOM_MODEL) return "自訂…";
    const entry = service.models.find((m) => m.id === value);
    if (!entry) return String(value);
    return `${entry.id}（${[KIND_LABEL[entry.kind], entry.note].filter(Boolean).join("，")}）`;
}

export function visibleWidgets(service, choice, provider) {
    const backend = isCustom(service) ? provider : service.provider;
    const http = backend === OPENAI_COMPATIBLE;
    const hasList = service.models.length > 0;
    return {
        provider: isCustom(service),
        base_url: isCustom(service) && http,
        api_key: http,
        mf_model: hasList,
        model: !hasList || choice === CUSTOM_MODEL,
        device: backend === FASTER_WHISPER,
    };
}
