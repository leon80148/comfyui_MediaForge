// MediaForge timeline grid math — pure functions shared by the timeline editor.
//
// Mirrors utils/timeline_plan.py so the GUI can give instant feedback; Python
// stays the authority and re-validates on every run. Both sides are checked
// against tests/golden/timeline_vectors.json. Frame counts are integers and
// fps is rational [num, den]; sample math goes through BigInt so the half-up
// rounding matches Python exactly.

export const FPS_PRESETS = {
    "16": [16, 1], "24": [24, 1], "25": [25, 1], "30": [30, 1],
    "24000/1001": [24000, 1001], "30000/1001": [30000, 1001],
    "48": [48, 1], "50": [50, 1], "60000/1001": [60000, 1001], "60": [60, 1],
};
export const GRIDS = ["h3_5+17n", "causal_4n+1", "causal_8n+1", "free"];
export const MODES = ["generate", "cut"];
export const MAX_SEGMENTS = 256;
export const H3_GRID = "h3_5+17n";

const GRID_STEPS = { "h3_5+17n": [5, 17], "causal_4n+1": [1, 4], "causal_8n+1": [1, 8] };

export function sampleIndex(frame, sampleRate, fps) {
    const [num, den] = fps;
    const n = BigInt(num);
    return Number((2n * BigInt(frame) * BigInt(sampleRate) * BigInt(den) + n) / (2n * n));
}

export function lengthOk(grid, frames) {
    if (grid === "free") return frames >= 1;
    const [base, step] = GRID_STEPS[grid];
    return frames >= base && (frames - base) % step === 0;
}

export function overlapOk(grid, frames) {
    if (frames === 0 || grid === "free") return frames >= 0;
    const [base, step] = GRID_STEPS[grid];
    return frames >= base && (frames - base) % step === 0;
}

export function latentTokens(grid, frames) {
    if (grid === "free") return null;
    const [base, step] = GRID_STEPS[grid];
    const n = Math.floor((frames - base) / step);
    return grid === H3_GRID ? 2 + 5 * n : 1 + n;
}

export function h3AudioTokens(frames) {
    return Math.floor((10 * frames + 3) / 6);
}

function gcd(a, b) {
    a = Math.abs(a); b = Math.abs(b);
    while (b) [a, b] = [b, a % b];
    return a || 1;
}

export function h3AudioCarry(prevLength, overlap) {
    const d = prevLength - overlap;
    const k0 = Math.floor((10 * d + 3) / 6);
    // phase = k0 - 5d/3 = (3*k0 - 5d) / 3
    const num = 3 * k0 - 5 * d;
    const g = gcd(num, 3);
    return { audio_k0: k0, audio_tokens: h3AudioTokens(prevLength) - k0, audio_phase: [num / g, 3 / g] };
}

export function carrySpec(grid, prevLength, overlap) {
    if (overlap === 0 || grid === "free") return null;
    const spec = { video_tokens: latentTokens(grid, overlap) };
    return grid === H3_GRID ? { ...spec, ...h3AudioCarry(prevLength, overlap) } : spec;
}

export function phaseMs(spec) {
    if (!spec || !spec.audio_phase) return null;
    const [num, den] = spec.audio_phase;
    return (num / den) * 25; // one H3 audio token = 1/40 s = 25 ms
}

// Canonical defaults — must match utils/timeline_plan.parse_timeline_data().
export function canonicalize(data) {
    return {
        version: 1,
        source: { origin_frame: data?.source?.origin_frame ?? 0 },
        segments: (data?.segments ?? []).map((s) => ({
            start: s.start, end: s.end, prompt: s.prompt ?? "",
            seam_in: s.seam_in?.kind === "crossfade"
                ? { kind: "crossfade", frames: s.seam_in.frames, audio_curve: s.seam_in.audio_curve ?? "auto" }
                : { kind: "cut" },
        })),
    };
}

function fail(location, reason) {
    return { ok: false, error: { code: "E_TIMELINE_JSON", location, reason } };
}

function isInt(v) {
    return typeof v === "number" && Number.isSafeInteger(v);
}

// Same check order as the Python validators, so the first reported location matches.
export function validate(data, mode, fpsKey, grid, sourceFrames = null, hasSource = false) {
    const fps = FPS_PRESETS[fpsKey];
    if (!fps) return fail("fps", "unknown fps");
    const effectiveGrid = mode === "cut" ? "free" : grid;
    const segs = data.segments;
    if (!Array.isArray(segs) || segs.length === 0) return fail("segments", "at least one segment");
    if (segs.length > MAX_SEGMENTS) return fail("segments", `at most ${MAX_SEGMENTS}`);
    for (let i = 0; i < segs.length; i++) {
        if (!isInt(segs[i].start) || segs[i].start < 0) return fail(`segments[${i}].start`, "integer >= 0");
        if (!isInt(segs[i].end) || segs[i].end < 0) return fail(`segments[${i}].end`, "integer >= 0");
    }
    if (effectiveGrid === H3_GRID && (fps[0] !== 24 || fps[1] !== 1)) {
        return { ok: false, error: { code: "E_H3_FPS", location: "fps", reason: "h3 needs 24 fps" } };
    }
    if (mode === "generate") {
        if (segs[0].start !== 0) return fail("segments[0].start", "first segment starts at 0");
        for (let i = 0; i < segs.length; i++) {
            const loc = `segments[${i}]`;
            const s = segs[i];
            const length = s.end - s.start;
            if (length <= 0) return fail(`${loc}.end`, "end > start");
            if (!lengthOk(effectiveGrid, length)) return fail(loc, "illegal length");
            if (i === 0) {
                if (s.seam_in.kind !== "cut") return fail(`${loc}.seam_in`, "first seam is cut");
                continue;
            }
            const p = segs[i - 1];
            if (s.start <= p.start || s.end <= p.end) return fail(loc, "start and end must increase");
            if (s.start > p.end) return fail(`${loc}.start`, "no gaps in generate mode");
            // increasing start and end already imply overlap < both lengths
            const overlap = p.end - s.start;
            if (!overlapOk(effectiveGrid, overlap)) return fail(`${loc}.start`, "illegal overlap");
            if (s.seam_in.kind === "crossfade") {
                const limit = crossfadeLimit(segs, i);
                if (overlap === 0 || !isInt(s.seam_in.frames) || s.seam_in.frames < 1 || s.seam_in.frames > limit) {
                    return fail(`${loc}.seam_in.frames`, `crossfade 1..${limit}`);
                }
            }
        }
        return { ok: true, effectiveGrid, totalFrames: segs[segs.length - 1].end, diagnostics: diagnostics(segs) };
    }
    if (!hasSource) return fail("video", "cut mode needs a source");
    const origin = data.source.origin_frame;
    for (let i = 0; i < segs.length; i++) {
        const loc = `segments[${i}]`;
        const s = segs[i];
        if (s.end <= s.start) return fail(`${loc}.end`, "end > start");
        if (s.seam_in.kind !== "cut") return fail(`${loc}.seam_in`, "cut mode seams are cut");
        if (i && s.start < segs[i - 1].end) return fail(`${loc}.start`, "sorted, non-overlapping");
        if (s.start < origin || (sourceFrames !== null && s.end > origin + sourceFrames)) return fail(loc, "outside source");
    }
    return { ok: true, effectiveGrid, totalFrames: segs.reduce((n, s) => n + s.end - s.start, 0), diagnostics: [] };
}

function diagnostics(segs) {
    const out = [];
    for (let i = 1; i < segs.length - 1; i++) {
        const incoming = segs[i - 1].end - segs[i].start;
        const outgoing = segs[i].end - segs[i + 1].start;
        if (incoming + outgoing > segs[i].end - segs[i].start) out.push({ code: "carry_reuses_incoming", segment: i });
    }
    return out;
}

// Pending frames can only be held back from the previous segment's own output [end_{i-2}, end_{i-1}).
export function crossfadeLimit(segs, i) {
    const p = segs[i - 1];
    const overlap = p.end - segs[i].start;
    const prevEmit = p.end - (i >= 2 ? segs[i - 2].end : 0);
    return Math.min(overlap, prevEmit);
}

export function segmentSpecs(segs, mode, grid) {
    const effectiveGrid = mode === "cut" ? "free" : grid;
    let cursor = 0;
    return segs.map((s, i) => {
        const p = segs[i - 1];
        const n = segs[i + 1];
        const generate = mode === "generate";
        const overlapIn = generate && p ? p.end - s.start : 0;
        const overlapOut = generate && n ? s.end - n.start : 0;
        const length = s.end - s.start;
        let emit;
        if (generate) emit = [p ? p.end : 0, s.end];
        else { emit = [cursor, cursor + length]; cursor += length; }
        return {
            index: i, length, overlap_in: overlapIn, overlap_out: overlapOut, emit_interval: emit,
            expected_tokens: latentTokens(effectiveGrid, length),
            carry_in: p ? carrySpec(effectiveGrid, p.end - p.start, overlapIn) : null,
            carry_out: n ? carrySpec(effectiveGrid, length, overlapOut) : null,
        };
    });
}

// ---- editing helpers (used by the editor; pure so node can test them) ----

export function legalLengths(grid, minimum, maximum) {
    const out = [];
    for (let f = Math.max(1, minimum); f <= maximum; f++) if (lengthOk(grid, f)) out.push(f);
    return out;
}

function nearest(candidates, target) {
    let best = null;
    for (const c of candidates) if (best === null || Math.abs(c - target) < Math.abs(best - target)) best = c;
    return best;
}

// generate mode: right edge = change this segment's length; later segments shift with it.
// left edge (i > 0) = change the incoming overlap; this and later segments shift together.
// Both keep every other length and overlap, so the chain stays legal without searching
// for a start that satisfies two congruences at once (rarely possible on the h3 grid).
export function editGenerate(segs, index, edge, frame, grid) {
    const s = segs[index];
    let delta;
    if (edge === "right") {
        const p = segs[index - 1];
        const minLen = Math.max(1, (p ? p.end - s.start : 0) + 1, index + 1 < segs.length ? s.end - segs[index + 1].start + 1 : 1);
        const target = nearest(legalLengths(grid, minLen, Math.max(minLen, frame - s.start + 200)), frame - s.start);
        if (target === null) return { ok: false, reason: "no legal length" };
        delta = s.start + target - s.end;
        return { ok: true, segments: segs.map((x, i) => (i < index ? x : i === index ? { ...x, end: x.end + delta } : { ...x, start: x.start + delta, end: x.end + delta })) };
    }
    if (index === 0) return { ok: false, reason: "first segment starts at 0" };
    const p = segs[index - 1];
    const maxOverlap = Math.min(p.end - p.start, s.end - s.start) - 1;
    const choices = [];
    for (let o = 0; o <= maxOverlap; o++) if (overlapOk(grid, o)) choices.push(o);
    const overlap = nearest(choices, p.end - frame);
    if (overlap === null) return { ok: false, reason: "no legal overlap" };
    delta = (p.end - overlap) - s.start;
    return { ok: true, segments: segs.map((x, i) => (i < index ? x : { ...x, start: x.start + delta, end: x.end + delta })) };
}

// cut mode: edges are independent, clamped between neighbours and to at least one frame.
export function editCut(segs, index, edge, frame) {
    const s = segs[index];
    const lo = edge === "left" ? (index ? segs[index - 1].end : 0) : s.start + 1;
    const hi = edge === "left" ? s.end - 1 : (index + 1 < segs.length ? segs[index + 1].start : Number.MAX_SAFE_INTEGER);
    const f = Math.min(hi, Math.max(lo, Math.round(frame)));
    return { ok: true, segments: segs.map((x, i) => (i === index ? { ...x, [edge === "left" ? "start" : "end"]: f } : x)) };
}

export function autoFill(count, length, overlap, grid) {
    if (!lengthOk(grid, length)) return { ok: false, reason: "illegal length" };
    if (count > 1 && (!overlapOk(grid, overlap) || overlap >= length)) return { ok: false, reason: "illegal overlap" };
    if (count < 1 || count > MAX_SEGMENTS) return { ok: false, reason: "segment count" };
    const segs = [];
    for (let i = 0; i < count; i++) {
        const start = i * (length - overlap);
        segs.push({ start, end: start + length, prompt: "", seam_in: { kind: "cut" } });
    }
    return { ok: true, segments: segs };
}
