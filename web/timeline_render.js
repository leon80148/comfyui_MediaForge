// Mirrors utils/timeline_render.py. All persisted numeric settings are integers.
import { refOk } from "./timeline_materials.js";
export const DEFAULT_RENDER = { mode: "direct", width: 1344, height: 768, base_width: 672, base_height: 384,
    model: "", high_steps: 2, ref_video_short_edge: 0 };
export const RENDER_MODES = { direct: "直接生成", latent: "Latent 模型放大", two_stage: "二階段生成", selflift: "SelfLift（需 TimelineDirector / Euler）" };

export function renderOk(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || Object.keys(raw).some((k) => !Object.hasOwn(DEFAULT_RENDER, k))) return false;
    const r = { ...DEFAULT_RENDER, ...raw };
    if (!Object.hasOwn(RENDER_MODES, r.mode) || typeof r.model !== "string") return false;
    if (r.model && !refOk(r.model)) return false;
    for (const key of ["width", "height", "base_width", "base_height", "high_steps", "ref_video_short_edge"]) {
        const v = r[key];
        if (!Number.isSafeInteger(v)) return false;
        if (key === "high_steps" ? v < 1 || v > 10000 : key === "ref_video_short_edge"
            ? v < 0 || v > 4096 || v % 32 : v < 32 || v > 8192 || v % 32) return false;
    }
    if (["latent", "two_stage"].includes(r.mode) && (r.base_width > r.width || r.base_height > r.height ||
        r.base_width === r.width && r.base_height === r.height)) return false;
    return r.mode !== "selflift" || !(r.width % 64 || r.height % 64);
}
