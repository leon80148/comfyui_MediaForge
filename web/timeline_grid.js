// MediaForge timeline grid math — pure functions shared by the timeline editor.
//
// Mirrors utils/timeline_plan.py so the GUI can give instant feedback; Python
// stays the authority and re-validates on every run. Both sides are checked
// against tests/golden/timeline_vectors.json. Frame counts are integers and
// fps is rational [num, den]; sample math goes through BigInt so the half-up
// rounding matches Python exactly.

import {
    FloatToken, MAX_FRAME, intReason, jsonErrorDetail, objectReason, parseAnchors, parseMaterials, parseRefs,
    resolvePrompt, segmentManifest, validateGeometry,
} from "./timeline_materials.js";

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

// Canonical defaults — must match utils/timeline_plan.parse_timeline_data(): v1 comes out in the
// v2 shape, as Python upgrades it. Call it only on data that passed schemaError().
export function canonicalize(data) {
    const materials = parseMaterials(data?.materials ?? []);
    const refs = materials.ok ? parseRefs(data?.refs ?? [], materials.materials) : materials;
    const anchors = materials.ok ? parseAnchors(data?.anchors ?? [], materials.materials) : materials;
    if (!refs.ok || !anchors.ok) throw new TypeError("canonicalize() needs data that passed schemaError()");
    return {
        version: 2,
        source: { origin_frame: data?.source?.origin_frame ?? 0 },
        prompt: data?.prompt ?? "",
        segments: (data?.segments ?? []).map((s) => ({
            start: s.start, end: s.end, prompt: s.prompt ?? "", seed: s.seed ?? null,
            seam_in: s.seam_in?.kind === "crossfade"
                ? { kind: "crossfade", frames: s.seam_in.frames, audio_curve: s.seam_in.audio_curve ?? "auto" }
                : { kind: "cut" },
        })),
        materials: materials.materials,
        refs: refs.refs,
        anchors: anchors.anchors,
    };
}

function fail(location, reason) {
    return { ok: false, error: { code: "E_TIMELINE_JSON", location, ...jsonErrorDetail(location, null, reason) } };
}

function isInt(v) {
    return typeof v === "number" && Number.isSafeInteger(v);
}

// Structural rules of parse_timeline_data (v1 and v2), in the same order, so the first failing
// location matches. canonicalize() would otherwise drop unknown fields / wrong types and the
// editor would show (and later save) a timeline that Python rejects. JSON.parse cannot see
// duplicate keys or integral floats such as 5.0; parse the text with strictParse() to catch them.
// mirrors Python: a lone surrogate can't be UTF-8 encoded for the plan hash
function textOk(value) {
    return typeof value === "string" && !/\p{Cs}/u.test(value);
}

// Frame fields share the materials' MAX_FRAME (v1 too: it is upgraded to v2); seed is not a frame and stays JS-safe.
function frameOk(v, minimum = 0) {
    return isInt(v) && minimum <= v && v <= MAX_FRAME;
}

// The location of the first structure error, or null (the editor's broken-mode check).
export function schemaError(data) {
    return schemaErrorDetail(data)?.location ?? null;
}

function textReason(value) {
    if (typeof value !== "string") return "not_string";
    return textOk(value) ? null : "lone_surrogate";
}

// The same first error as a structured E_TIMELINE_JSON object {code, location, reason, entity?, field?} (§4.2.5),
// or null. `reason` is the machine code of the Python branch that rejects the same value.
export function schemaErrorDetail(data) {
    const at = (location, reason) => ({ code: "E_TIMELINE_JSON", location, ...jsonErrorDetail(location, null, reason) });
    // Python picks the key set with `version == 2`, which 2.0 satisfies too
    const v2 = data?.version === 2 || (data?.version instanceof FloatToken && Number(data.version.text) === 2);
    let why = objectReason(data, v2 ? ["version", "source", "prompt", "segments", "materials", "refs", "anchors"]
        : ["version", "source", "segments"]);
    if (why) return at("timeline_data", why);
    why = intReason(data.version, -Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER)
        ?? (data.version === 1 || data.version === 2 ? null : "unsupported_version");
    if (why) return at("version", why);
    const source = data.source === undefined ? {} : data.source;
    why = objectReason(source, ["origin_frame"]);
    if (why) return at("source", why);
    why = source.origin_frame === undefined ? null : intReason(source.origin_frame, -MAX_FRAME, MAX_FRAME);
    if (why) return at("source.origin_frame", why);
    why = data.prompt === undefined ? null : textReason(data.prompt);
    if (why) return at("prompt", why);
    const segs = data.segments;
    if (segs === undefined) return at("segments", "missing_key");
    if (!Array.isArray(segs)) return at("segments", "not_array");
    if (segs.length === 0) return at("segments", "no_segments");
    if (segs.length > MAX_SEGMENTS) return at("segments", "too_many_segments");
    const segmentKeys = v2 ? ["start", "end", "prompt", "seam_in", "seed"] : ["start", "end", "prompt", "seam_in"];
    for (let i = 0; i < segs.length; i++) {
        const loc = `segments[${i}]`;
        const s = segs[i];
        why = objectReason(s, segmentKeys);
        if (why) return at(loc, why);
        for (const key of ["start", "end"]) {
            why = intReason(s[key], 0, MAX_FRAME);
            if (why) return at(`${loc}.${key}`, why);
        }
        why = s.prompt === undefined ? null : textReason(s.prompt);
        if (why) return at(`${loc}.prompt`, why);
        why = s.seed === undefined || s.seed === null ? null : intReason(s.seed, 0, Number.MAX_SAFE_INTEGER);
        if (why) return at(`${loc}.seed`, why);
        if (s.seam_in === undefined) continue;
        const seam = s.seam_in;
        why = objectReason(seam, ["kind", "frames", "audio_curve"]);
        if (why) return at(`${loc}.seam_in`, why);
        if (seam.kind === undefined) return at(`${loc}.seam_in.kind`, "missing_key");
        if (seam.kind !== "cut" && seam.kind !== "crossfade") return at(`${loc}.seam_in.kind`, "bad_enum");
        if (seam.kind === "cut") {
            if (Object.keys(seam).length !== 1) return at(`${loc}.seam_in`, "unknown_key");
            continue;
        }
        if (seam.audio_curve !== undefined && !["auto", "linear", "equal_power"].includes(seam.audio_curve)) {
            return at(`${loc}.seam_in.audio_curve`, "bad_enum");
        }
        why = intReason(seam.frames, 1, MAX_FRAME);
        if (why) return at(`${loc}.seam_in.frames`, why);
    }
    // absent → []; an explicit null still fails, like Python's data.get(key, [])
    const listOf = (key) => (data[key] === undefined ? [] : data[key]);
    const materials = parseMaterials(listOf("materials"));
    if (!materials.ok) return materials.error;
    const refs = parseRefs(listOf("refs"), materials.materials);
    if (!refs.ok) return refs.error;
    const anchors = parseAnchors(listOf("anchors"), materials.materials);
    return anchors.ok ? null : anchors.error;
}

// Same check order as the Python validators, so the first reported location matches.
export function validate(data, mode, fpsKey, grid, sourceFrames = null, hasSource = false) {
    if (!MODES.includes(mode)) return fail("mode", "unknown_mode");
    if (!Object.hasOwn(FPS_PRESETS, fpsKey)) return fail("fps", "unknown_fps");
    if (!GRIDS.includes(grid)) return fail("grid", "unknown_grid");
    const fps = FPS_PRESETS[fpsKey];
    const effectiveGrid = mode === "cut" ? "free" : grid;
    const segs = data.segments;
    if (!Array.isArray(segs) || segs.length === 0) return fail("segments", "no_segments");
    if (segs.length > MAX_SEGMENTS) return fail("segments", "too_many_segments");
    for (let i = 0; i < segs.length; i++) {
        if (!frameOk(segs[i].start)) return fail(`segments[${i}].start`, "not_frame_index");
        if (!frameOk(segs[i].end)) return fail(`segments[${i}].end`, "not_frame_index");
    }
    if (effectiveGrid === H3_GRID && (fps[0] !== 24 || fps[1] !== 1)) {
        return { ok: false, error: { code: "E_H3_FPS", location: "fps", ...jsonErrorDetail("fps", null, "h3_fps") } };
    }
    if (mode === "generate") {
        if (segs[0].start !== 0) return fail("segments[0].start", "first_start_zero");
        for (let i = 0; i < segs.length; i++) {
            const loc = `segments[${i}]`;
            const s = segs[i];
            const length = s.end - s.start;
            if (length <= 0) return fail(`${loc}.end`, "end_before_start");
            if (!lengthOk(effectiveGrid, length)) return fail(loc, "illegal_length");
            if (i === 0) {
                if (s.seam_in.kind !== "cut") return fail(`${loc}.seam_in`, "first_seam_cut");
                continue;
            }
            const p = segs[i - 1];
            if (s.start <= p.start || s.end <= p.end) return fail(loc, "not_increasing");
            if (s.start > p.end) return fail(`${loc}.start`, "gap");
            // increasing start and end already imply overlap < both lengths
            const overlap = p.end - s.start;
            if (!overlapOk(effectiveGrid, overlap)) return fail(`${loc}.start`, "illegal_overlap");
            if (s.seam_in.kind === "crossfade") {
                const limit = crossfadeLimit(segs, i);
                if (overlap === 0 || !isInt(s.seam_in.frames) || s.seam_in.frames < 1 || s.seam_in.frames > limit) {
                    return fail(`${loc}.seam_in.frames`, "crossfade_limit");
                }
            }
        }
        return { ok: true, effectiveGrid, totalFrames: segs[segs.length - 1].end, diagnostics: diagnostics(segs) };
    }
    if (!hasSource) return fail("video", "needs_source");
    if (data.materials?.length) return fail("materials", "cut_no_materials");
    const origin = data.source.origin_frame;
    for (let i = 0; i < segs.length; i++) {
        const loc = `segments[${i}]`;
        const s = segs[i];
        if (s.end <= s.start) return fail(`${loc}.end`, "end_before_start");
        if (s.seam_in.kind !== "cut") return fail(`${loc}.seam_in`, "cut_seam_only");
        if (i && s.start < segs[i - 1].end) return fail(`${loc}.start`, "overlapping_cut");
        if (s.start < origin || (sourceFrames !== null && s.end > origin + sourceFrames)) return fail(loc, "outside_source");
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

// compile_plan()'s material pass in §4.2.4 order — geometry, then every segment's manifest, then every
// segment's prompt — so the first error is the one Python reports. Call it on data that passed validate();
// snapshots are keyed by material id. Cut mode keeps manifests empty and prompts unresolved, as Python does.
export function resolveSegments(data, mode, grid, snapshots) {
    const segs = data.segments;
    const generate = mode === "generate";
    const h3 = generate && grid === H3_GRID;
    if (data.materials.length) {
        const error = validateGeometry(data.refs, data.anchors, snapshots, segs, h3);
        if (error) return { ok: false, error };
    }
    const manifests = [];
    for (const [i, spec] of segmentSpecs(segs, mode, grid).entries()) {
        let manifest = { pictures: [], videos: [], audios: [], keyframes: [], guides: [], locks: [], carry_conflicts: [] };
        if (generate && data.materials.length) {
            const m = segmentManifest(segs, i, data.refs, data.anchors, snapshots, h3, spec.carry_in);
            if (!m.ok) return m;
            manifest = m.manifest;
        }
        manifests.push(manifest);
    }
    const out = [];
    for (const [i, s] of segs.entries()) {
        let prompt = s.prompt || data.prompt;
        if (generate) {
            const p = resolvePrompt(prompt, manifests[i], data.materials, i, h3);
            if (!p.ok) return p;
            prompt = p.prompt;
        }
        out.push({ prompt, manifest: manifests[i] });
    }
    return { ok: true, segments: out };
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
        if (target === null) return { ok: false, reason: "no_legal_length" };
        delta = s.start + target - s.end;
        return { ok: true, segments: segs.map((x, i) => (i < index ? x : i === index ? { ...x, end: x.end + delta } : { ...x, start: x.start + delta, end: x.end + delta })) };
    }
    if (index === 0) return { ok: false, reason: "first_start_zero" };
    const p = segs[index - 1];
    const maxOverlap = Math.min(p.end - p.start, s.end - s.start) - 1;
    const choices = [];
    for (let o = 0; o <= maxOverlap; o++) if (overlapOk(grid, o)) choices.push(o);
    const overlap = nearest(choices, p.end - frame);
    if (overlap === null) return { ok: false, reason: "no_legal_overlap" };
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
    if (!lengthOk(grid, length)) return { ok: false, reason: "illegal_length" };
    if (count > 1 && (!overlapOk(grid, overlap) || overlap >= length)) return { ok: false, reason: "illegal_overlap" };
    if (count < 1 || count > MAX_SEGMENTS) return { ok: false, reason: "segment_count" };
    const segs = [];
    for (let i = 0; i < count; i++) {
        const start = i * (length - overlap);
        segs.push({ start, end: start + length, prompt: "", seam_in: { kind: "cut" } });
    }
    return { ok: true, segments: segs };
}
