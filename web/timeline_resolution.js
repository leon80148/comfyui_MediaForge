// UI helpers only: workflows continue to store authoritative integer pixel sizes.
export const ASPECT_RATIOS = {
    "16:9": "16:9 橫式", "9:16": "9:16 直式", "1:1": "1:1 正方形",
    "4:3": "4:3 橫式", "3:4": "3:4 直式", "21:9": "21:9 寬銀幕",
};
export const MP_PRESETS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 8];
export const UPSCALE_FACTORS = { "2": "2×（先生成約 50% 寬高）", "1.5": "1.5×（先生成約 67% 寬高）", "4": "4×（先生成約 25% 寬高）" };
export const megapixels = (width, height) => width * height / 1_000_000;
export const formatMP = (width, height) => `${Number(megapixels(width, height).toFixed(3))} MP`;
export const aspectValue = (key) => {
    const [w, h] = key.split(":").map(Number);
    return w / h;
};
const aligned = (v, grid = 32) => Math.max(grid, Math.min(8192, Math.round(v / grid) * grid));

// Search nearby grid points, preferring aspect accuracy over an exact pixel budget.
// The resulting dimensions and actual MP are always shown before execution.
export function resolutionSize(mp, aspect, grid = 32) {
    if (!Number.isFinite(mp) || mp <= 0 || !Number.isFinite(aspect) || aspect <= 0 || ![32, 64].includes(grid)) {
        throw new Error("Invalid resolution selection");
    }
    const area = Math.max(grid * grid, Math.min(8192 * 8192, mp * 1_000_000));
    const w = aligned(Math.sqrt(area * aspect), grid), h = aligned(Math.sqrt(area / aspect), grid);
    let best, cost = Infinity;
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        const width = aligned(w + dx * grid, grid), height = aligned(h + dy * grid, grid);
        const score = Math.log(width * height / area) ** 2 + 4 * Math.log(width / height / aspect) ** 2;
        if (score < cost) { best = { width, height }; cost = score; }
    }
    return best;
}

export function currentAspect(r) {
    const nearest = Object.keys(ASPECT_RATIOS).sort((a, b) =>
        Math.abs(Math.log(r.width / r.height / aspectValue(a))) - Math.abs(Math.log(r.width / r.height / aspectValue(b))))[0];
    const tolerance = Math.min(0.03, 16 / r.width + 16 / r.height);
    return Math.abs(Math.log(r.width / r.height / aspectValue(nearest))) <= tolerance ? nearest : "custom";
}

export function currentMP(r) {
    const key = currentAspect(r), aspect = key === "custom" ? r.width / r.height : aspectValue(key);
    const exact = MP_PRESETS.find((mp) => {
        const size = resolutionSize(mp, aspect, r.mode === "selflift" ? 64 : 32);
        return size.width === r.width && size.height === r.height;
    });
    if (exact !== undefined) return exact;
    const actual = megapixels(r.width, r.height);
    const nearest = [...MP_PRESETS].sort((a, b) => Math.abs(Math.log(actual / a)) - Math.abs(Math.log(actual / b)))[0];
    return Math.abs(Math.log(actual / nearest)) <= 0.04 ? nearest : "custom";
}

export function scaledBase(width, height, factor = 2) {
    let base_width = Math.min(width, aligned(width / factor)), base_height = Math.min(height, aligned(height / factor));
    if (base_width === width && base_height === height) {
        if (width >= height && width > 32) base_width -= 32;
        else if (height > 32) base_height -= 32;
    }
    return { base_width, base_height };
}

export function currentFactor(r) {
    return Object.keys(UPSCALE_FACTORS).find((factor) => {
        const b = scaledBase(r.width, r.height, Number(factor));
        return b.base_width === r.base_width && b.base_height === r.base_height;
    }) ?? "custom";
}

// Target edits resize the base in the same transaction, including custom base ratios.
export function resizeRender(r, width, height) {
    if (["latent", "two_stage"].includes(r.mode) && width === 32 && height === 32) width = height = 64;
    const factor = currentFactor(r);
    const base = r.mode === "selflift" || r.base_width > r.width || r.base_height > r.height
        ? scaledBase(width, height) : factor !== "custom" ? scaledBase(width, height, Number(factor))
            : { base_width: Math.min(width, aligned(width * r.base_width / r.width)), base_height: Math.min(height, aligned(height * r.base_height / r.height)) };
    if (base.base_width === width && base.base_height === height) Object.assign(base, scaledBase(width, height));
    return { width, height, ...base };
}

export function renderModePatch(r, mode) {
    const grid = mode === "selflift" ? 64 : 32;
    return { mode, ...resizeRender({ ...r, mode }, aligned(r.width, grid), aligned(r.height, grid)) };
}
