// MediaForge timeline v2 edit commands — pure functions used by the timeline editor.
//
// Every command takes the canonical v2 timeline (version / source / prompt / materials / refs /
// anchors / segments) and an edit context
//   ctx = {mode, fpsKey, grid, h3, sourceFrames, hasSource, snapshots: {material id: verified snapshot}}
// and returns {ok, next, affected, warnings} or {ok: false, reason[, problem]}; the editor commits
// `next` as one undo step. A command never returns a timeline with a structure, segment-chain or
// material-geometry problem the input did not already have (§8.2); prompt and manifest problems are
// reported by timelineProblems() instead. Python re-validates everything on the next run.

import {
    autoFill, carrySpec, crossfadeLimit, editGenerate, lengthOk, overlapOk, resolveSegments, schemaErrorDetail, validate,
} from "./timeline_grid.js";
import {
    H3_LIMITS, MAX_ANCHORS, MAX_MATERIALS, MAX_REFS, nameOk, resolvePrompt, segmentManifest, validateGeometry,
} from "./timeline_materials.js";

export const H3_DEFAULT_GUIDE = 22;

export function emitIntervals(segments) {
    return segments.map((s, i) => [i ? segments[i - 1].end : 0, s.end]);
}

export function segmentAt(segments, frame) {
    const emits = emitIntervals(segments);
    const i = emits.findIndex(([lo, hi]) => frame >= lo && frame < hi);
    return i >= 0 ? i : (frame < 0 ? 0 : emits.length - 1);
}

export function h3Crop(frames) {
    return frames - ((frames - 5) % 17);
}

function isH3GuideLength(n) {
    return n >= 5 && (n - 5) % 17 === 0;
}

function fail(reason, extra = {}) {
    return { ok: false, reason, ...extra };
}

function done(data, next, ctx, affected, warnings = []) {
    const problem = checkEdit(data, next, ctx);
    return problem ? { ok: false, reason: "breaks", problem } : { ok: true, next, affected, warnings };
}

export function newId(prefix, items) {
    const used = new Set(items.map((x) => x.id));
    for (let n = 1; ; n++) if (!used.has(`${prefix}${n}`)) return `${prefix}${n}`;
}

const NAME_RANGES = [[0x30, 0x39], [0x41, 0x5a], [0x61, 0x7a], [0x5f, 0x5f], [0x2d, 0x2d],
    [0x3040, 0x30ff], [0x3400, 0x4dbf], [0x4e00, 0x9fff], [0xac00, 0xd7af], [0xf900, 0xfaff]];

function nameChar(ch) {
    const c = ch.codePointAt(0);
    return NAME_RANGES.some(([lo, hi]) => c >= lo && c <= hi);
}

// File name → alias: NFC, illegal characters → "_", room kept for a "_N" suffix so the result
// never exceeds 32 code points.
export function aliasFromFilename(filename, existingNames) {
    const stem = filename.replace(/^.*[\\/]/, "").replace(/\.[^.]*$/, "").normalize("NFC");
    let base = [...stem].map((ch) => (nameChar(ch) ? ch : "_")).join("").replace(/_+/g, "_").replace(/^_|_$/g, "") || "material";
    const taken = new Set(existingNames);
    const fit = (b, suffix) => [...b].slice(0, 32 - [...suffix].length).join("") + suffix;
    if (!taken.has(fit(base, ""))) return fit(base, "");
    for (let n = 2; ; n++) {
        const candidate = fit(base, `_${n}`);
        if (!taken.has(candidate)) return candidate;
    }
}

function material(data, id) {
    return data.materials.find((m) => m.id === id);
}

function total(data) {
    return data.segments[data.segments.length - 1].end;
}

function templateOf(data, index) {
    return data.segments[index].prompt || data.prompt;
}

function segmentOf(location) {
    const m = /^segments\[(\d+)\]/.exec(location ?? "");
    return m ? Number(m[1]) : null;
}

// Snapshots that still describe `data`: a material whose file or kind changed is unverified again.
function verifiedSnapshots(data, snapshots) {
    const out = {};
    for (const m of data.materials) {
        const s = snapshots?.[m.id];
        if (s && s.ref === m.ref && s.kind === m.kind) out[m.id] = s;
    }
    return out;
}

function covers(data, snaps) {
    return data.refs.every((r) => snaps[r.material]) && data.anchors.every((a) => snaps[a.material]);
}

// ---- validation ----

// Unverified materials get a stub that lets only the position rules fire (past the timeline end,
// guide across a segment boundary or off the H3 grid, overlaps); length / audio rules wait for the
// snapshot. Problems are keyed by entity id, so an edit is judged by what it adds, not by array
// positions or by problems that were already there.
function geometryProblems(data, ctx) {
    const verified = verifiedSnapshots(data, ctx.snapshots);
    const snaps = Object.fromEntries(data.materials.map((m) => [m.id,
        verified[m.id] ?? { id: m.id, kind: m.kind, frames: Infinity, has_audio: true }]));
    const segs = data.segments;
    const out = [];
    const add = (key, error, frame) => out.push({ key, stage: "geometry", error, segment: segmentAt(segs, frame) });
    for (const r of data.refs) {
        const e = validateGeometry([r], [], snaps, segs, ctx.h3);
        if (e) add(`ref:${r.id}`, e, r.from);
    }
    let clean = [];
    for (const a of data.anchors) {
        const e = validateGeometry([], [a], snaps, segs, ctx.h3);
        if (e) add(`anchor:${a.id}`, e, a.at);
        else clean.push(a);
    }
    // occupancy reports one pair at a time: drop the second anchor and look again
    const flagged = new Set();
    for (;;) {
        const e = validateGeometry([], clean, snaps, segs, ctx.h3);
        const rest = e ? clean.filter((a) => a.id !== e.second) : clean;
        if (!e || rest.length === clean.length) break;
        for (const id of [e.first, e.second]) {
            if (flagged.has(id)) continue;
            flagged.add(id);
            add(`overlap:${id}`, e, data.anchors.find((a) => a.id === id).at);
        }
        clean = rest;
    }
    return out;
}

function blockers(data, ctx) {
    const schema = schemaErrorDetail(data);
    if (schema !== null) return [{ key: "schema", stage: "schema", error: schema, segment: segmentOf(schema.location) }];
    const out = [];
    const grid = validate(data, ctx.mode, ctx.fpsKey, ctx.grid, ctx.sourceFrames ?? null, !!ctx.hasSource);
    if (!grid.ok) out.push({ key: "grid", stage: "grid", error: grid.error, segment: segmentOf(grid.error.location) });
    if (ctx.mode === "generate") out.push(...geometryProblems(data, ctx));
    return out;
}

// The first structure / segment-chain / geometry problem `after` has and `before` did not, or null.
export function checkEdit(before, after, ctx) {
    const had = new Set(blockers(before, ctx).map((p) => p.key));
    return blockers(after, ctx).find((p) => !had.has(p.key)) ?? null;
}

function manifestFor(data, index, snaps, ctx) {
    const segs = data.segments;
    const prev = segs[index - 1];
    const carryIn = prev ? carrySpec(ctx.grid, prev.end - prev.start, prev.end - segs[index].start) : null;
    return segmentManifest(segs, index, data.refs, data.anchors, snaps, ctx.h3, carryIn);
}

// What segment `index` sends to the model (generate mode): {ok, manifest, template, prompt} with
// prompt = resolvePrompt()'s result, {ok: false, error} for a manifest error, or null while a
// material some segment uses has no verified snapshot.
export function segmentInputs(data, index, ctx) {
    const snaps = verifiedSnapshots(data, ctx.snapshots);
    if (!covers(data, snaps)) return null;
    const r = manifestFor(data, index, snaps, ctx);
    if (!r.ok) return r;
    const template = templateOf(data, index);
    return { ok: true, manifest: r.manifest, template, prompt: resolvePrompt(template, r.manifest, data.materials, index, ctx.h3) };
}

// What the status line reports, in Python's order (§4.2): structure → segment chain → geometry
// (refs → anchors → occupancy, every problem) → then resolveSegments(), the shared mirror of
// compile_plan's material pass (all manifests, then all prompts; first error). `verified` is false
// while some material lacks a verified snapshot (never a green "valid" then). With no materials the
// prompts are still resolved, so `<@gone>` and raw `<Picture 1>` show.
export function timelineProblems(data, ctx) {
    const list = blockers(data, ctx);
    const snaps = verifiedSnapshots(data, ctx.snapshots);
    const verified = data.materials.every((m) => snaps[m.id]);
    if (list.length || !covers(data, snaps)) return { list, verified };
    const r = resolveSegments(data, ctx.mode, ctx.grid, snaps);
    if (!r.ok) {
        const e = r.error;
        list.push({ key: "segments", stage: e.code === "E_PROMPT_REF" ? "prompt" : "manifest", error: e, segment: e.segment - 1 });
    }
    return { list, verified };
}

// Python's encode view drops these (timeline_plan._REPORT_KEYS): renaming a material or re-iding a
// binding does not change what the model receives.
const REPORT_KEYS = new Set(["name", "binding", "anchor", "material", "requested_frames", "discarded_tail", "tag",
    "audio_tag", "soundtrack_samples", "samples", "snapshot"]);
const MANIFEST_KINDS = ["pictures", "videos", "audios", "keyframes", "guides", "locks"];

function encodeItem(item) {
    const out = Object.fromEntries(Object.entries(item).filter(([k]) => !REPORT_KEYS.has(k)));
    const { id, name, ...snapshot } = item.snapshot;
    return { ...out, snapshot };
}

function inputKeys(data, ctx) {
    if (ctx.mode !== "generate") return null;
    const keys = [];
    for (const [i, s] of data.segments.entries()) {
        const v = segmentInputs(data, i, ctx);
        if (!v) return null;
        const inputs = v.ok
            ? { prompt: v.prompt.ok ? v.prompt.prompt : v.prompt.error,
                materials: Object.fromEntries(MANIFEST_KINDS.map((k) => [k, v.manifest[k].map(encodeItem)])) }
            : { error: v.error };
        keys.push(JSON.stringify({ length: s.end - s.start, ...inputs }));
    }
    return keys;
}

// Segments (0-based) whose model inputs — length, resolved prompt and the encode view of their
// materials — differ after an edit; null while snapshots are unverified.
export function inputImpact(before, after, ctx) {
    const a = inputKeys(before, ctx);
    const b = a && inputKeys(after, ctx);
    return b ? b.flatMap((key, i) => (key === a[i] ? [] : [i])) : null;
}

// A probe answer marks a material ready only as the v2 union of the expected kind with the fields
// the snapshot needs (§9); anything else is an error, never "ready".
export function probeIssue(body, kind) {
    const positive = (v) => Number.isSafeInteger(v) && v > 0;
    if (body?.version !== 2) return "probe_version";
    if (body.kind !== kind) return "kind_mismatch";
    if (typeof body.fingerprint !== "string" || !body.fingerprint) return "probe_fields";
    if (kind === "image") return positive(body.width) && positive(body.height) ? null : "probe_fields";
    if (!positive(body.duration_us)) return "no_duration";
    if (kind === "video" && typeof body.has_audio !== "boolean") return "probe_fields";
    return null;
}

// ---- snapping (§8.2): segment output boundaries > playhead > other bars' edges ----

export function snapTargets(data, playhead, skipKey) {
    const out = emitIntervals(data.segments).map(([lo]) => ({ frame: lo, rank: 0 }));
    out.push({ frame: total(data), rank: 0 }, { frame: playhead, rank: 1 });
    for (const r of data.refs) if (`ref:${r.id}` !== skipKey) out.push({ frame: r.from, rank: 2 }, { frame: r.to, rank: 2 });
    for (const a of data.anchors) {
        if (`anchor:${a.id}` === skipKey) continue;
        out.push({ frame: a.at, rank: 2 });
        if (a.length !== undefined) out.push({ frame: a.at + a.length, rank: 2 });
    }
    return out;
}

// Edge dropped at `frame`: the first legal target within `reach` frames by rank, then distance;
// otherwise the raw frame's own result, which may be a refusal. Never snaps to an illegal target.
export function pickSnap(targets, frame, reach, attempt) {
    const near = targets.filter((t) => Math.abs(t.frame - frame) <= reach)
        .sort((x, y) => x.rank - y.rank || Math.abs(x.frame - frame) - Math.abs(y.frame - frame));
    for (const t of near) {
        const r = attempt(t.frame);
        if (r.ok) return { ...r, frame: t.frame };
    }
    return { ...attempt(frame), frame };
}

// ---- create (card dropped on a track) ----

function snapOf(data, ctx, id) {
    return verifiedSnapshots(data, ctx.snapshots)[id] ?? null;
}

// lane: "refs" | "visual" | "audio"
export function createFromCard(data, materialId, lane, frame, ctx) {
    const m = material(data, materialId);
    if (!m) return fail("no_material");
    // every kind needs a verified snapshot: an image that failed to load must not become a ref either
    const snap = snapOf(data, ctx, m.id);
    if (!snap) return fail("not_ready");
    const length = snap.frames;
    if (m.kind !== "image" && !Number.isSafeInteger(length)) return fail("not_ready");
    const emits = emitIntervals(data.segments);
    const end = total(data);
    const f = Math.max(0, Math.min(end - 1, Math.round(frame)));
    const [lo, hi] = emits[segmentAt(data.segments, f)];
    if (lane === "refs") {
        if (data.refs.length >= MAX_REFS) return fail("too_many_refs");
        const id = newId("r", data.refs);
        let ref;
        if (m.kind === "image") ref = { id, material: m.id, from: lo, to: hi };
        else if (m.kind === "video") {
            const to = Math.min(hi, lo + length);
            if (to <= lo) return fail("material_too_short");
            ref = { id, material: m.id, from: lo, to, mode: "follow", in: 0, audio: !!snap.has_audio };
        } else {
            if (length < 1) return fail("material_too_short");
            ref = { id, material: m.id, from: lo, to: hi, in: 0, length };
        }
        return done(data, { ...data, refs: [...data.refs, ref] }, ctx, [id]);
    }
    const role = { visual: { image: "keyframe", video: "guide" }, audio: { audio: "lock" } }[lane]?.[m.kind];
    if (!role) return fail("wrong_lane");
    if (data.anchors.length >= MAX_ANCHORS) return fail("too_many_anchors");
    const id = newId("a", data.anchors);
    let anchor;
    if (role === "keyframe") anchor = { id, material: m.id, role, at: f };
    else if (role === "guide") {
        let at = f;
        let len;
        if (ctx.h3) {
            // 22 frames (17k+5); a guide that would run past the segment's output moves earlier
            if (length < 5 || hi - lo < 5) return fail("no_room");
            len = Math.min(H3_DEFAULT_GUIDE, h3Crop(Math.min(length, hi - lo)));
            at = Math.min(f, hi - len);
        } else {
            // other grids: from the drop frame to the end of that segment's output
            len = Math.min(length, hi - f);
            if (len < 1) return fail("material_too_short");
        }
        anchor = { id, material: m.id, role, at, in: 0, length: len, audio: false };
    } else {
        const len = Math.min(length, end - f);
        if (len < 1) return fail("no_room");
        anchor = { id, material: m.id, role, at: f, in: 0, length: len };
    }
    return done(data, { ...data, anchors: [...data.anchors, anchor] }, ctx, [id]);
}

// ---- edit existing bars ----

function replace(list, id, item) {
    return list.map((x) => (x.id === id ? item : x));
}

function withinOneEmit(segments, lo, hi) {
    return emitIntervals(segments).some(([a, e]) => a <= lo && hi <= e);
}

function checkRef(ref, snapshot) {
    if (ref.from < 0 || ref.to <= ref.from) return "empty_range";
    if (ref.in !== undefined && ref.in < 0) return "before_material";
    if (ref.mode === "follow" && snapshot?.frames != null && ref.in + (ref.to - ref.from) > snapshot.frames) return "past_material";
    return null;
}

function checkAnchor(a, segments, snapshot, h3) {
    if (a.at < 0 || (a.in ?? 0) < 0) return "before_material";
    if (a.length !== undefined) {
        if (a.length < 1) return "empty_range";
        if (snapshot?.frames != null && a.in + a.length > snapshot.frames) return "past_material";
    }
    if (a.role === "guide") {
        if (!withinOneEmit(segments, a.at, a.at + a.length)) return "guide_crosses_segment";
        if (h3 && !isH3GuideLength(a.length)) return "guide_length";
    }
    return null;
}

// kind: "ref" | "anchor"; op: "move" | "left" | "right"; delta in frames
export function editBar(data, kind, id, op, delta, ctx) {
    const list = kind === "ref" ? data.refs : data.anchors;
    const item = list.find((x) => x.id === id);
    if (!item) return fail("no_item");
    const snapshot = snapOf(data, ctx, item.material);
    const d = Math.round(delta);
    let next = { ...item };
    if (kind === "ref") {
        if (op === "move") next = { ...next, from: item.from + d, to: item.to + d };
        else if (op === "left") {
            next.from = item.from + d;
            if (item.mode === "follow") next.in = item.in + d;  // keep the remaining frames aligned
        } else next.to = item.to + d;
        const bad = checkRef(next, snapshot);
        if (bad) return fail(bad);
        return done(data, { ...data, refs: replace(data.refs, id, next) }, ctx, [id]);
    }
    if (op === "move") next.at = item.at + d;
    else if (item.role === "keyframe") return fail("keyframe_has_no_length");
    else if (op === "left") {
        let len = item.length - d;
        if (item.role === "guide" && ctx.h3) len = h3SnapLength(len);
        const dd = item.length - len;
        next = { ...next, at: item.at + dd, in: item.in + dd, length: len };
    } else {
        let len = item.length + d;
        if (item.role === "guide" && ctx.h3) len = h3SnapLength(len);
        next.length = len;
    }
    const bad = checkAnchor(next, data.segments, snapshot, ctx.h3);
    if (bad) return fail(bad);
    return done(data, { ...data, anchors: replace(data.anchors, id, next) }, ctx, [id]);
}

function h3SnapLength(len) {
    if (len < 5) return 5;
    const down = h3Crop(len);
    return len - down > 8 ? down + 17 : down;  // nearest legal 17k+5
}

// Inspector fields: `patch` overrides fields, an undefined value removes the field (a follow ref
// has no `length`).
export function patchBar(data, kind, id, patch, ctx) {
    const key = kind === "ref" ? "refs" : "anchors";
    const item = data[key].find((x) => x.id === id);
    if (!item) return fail("no_item");
    const next = Object.fromEntries(Object.entries({ ...item, ...patch }).filter(([, v]) => v !== undefined));
    return done(data, { ...data, [key]: replace(data[key], id, next) }, ctx, [id]);
}

export function splitBar(data, kind, id, frame, ctx) {
    const f = Math.round(frame);
    if (kind === "ref") {
        const r = data.refs.find((x) => x.id === id);
        if (!r) return fail("no_item");
        // Refs split on a segment boundary, so each half belongs to different segments and no
        // segment silently gains a second copy of the reference.
        const cuts = emitIntervals(data.segments).map(([lo]) => lo).filter((b) => b > r.from && b < r.to);
        if (!cuts.length) return fail("no_boundary");
        if (data.refs.length >= MAX_REFS) return fail("too_many_refs");
        const b = cuts.reduce((best, c) => (Math.abs(c - f) < Math.abs(best - f) ? c : best));
        const left = { ...r, to: b };
        const right = { ...r, id: newId("r", data.refs), from: b };
        if (r.mode === "follow") right.in = r.in + (b - r.from);
        const refs = data.refs.flatMap((x) => (x.id === id ? [left, right] : [x]));
        return done(data, { ...data, refs }, ctx, [left.id, right.id]);
    }
    const a = data.anchors.find((x) => x.id === id);
    if (!a) return fail("no_item");
    if (a.role === "keyframe") return fail("keyframe_has_no_length");
    if (a.role === "guide" && ctx.h3) return fail("guide_split_h3");
    if (f <= a.at || f >= a.at + a.length) return fail("outside_bar");
    if (data.anchors.length >= MAX_ANCHORS) return fail("too_many_anchors");
    const left = { ...a, length: f - a.at };
    const right = { ...a, id: newId("a", data.anchors), at: f, in: a.in + (f - a.at), length: a.at + a.length - f };
    const anchors = data.anchors.flatMap((x) => (x.id === id ? [left, right] : [x]));
    return done(data, { ...data, anchors }, ctx, [left.id, right.id]);
}

export function deleteBar(data, kind, id, ctx) {
    const key = kind === "ref" ? "refs" : "anchors";
    if (!data[key].some((x) => x.id === id)) return fail("no_item");
    return done(data, { ...data, [key]: data[key].filter((x) => x.id !== id) }, ctx, [id]);
}

export function moveRefInOrder(data, id, step, ctx) {
    const i = data.refs.findIndex((x) => x.id === id);
    const j = i + step;
    if (i < 0 || j < 0 || j >= data.refs.length) return fail("no_move");
    const refs = data.refs.slice();
    [refs[i], refs[j]] = [refs[j], refs[i]];
    return done(data, { ...data, refs }, ctx, [id], ["tag_order_changed"]);
}

// ---- materials ----

export function addMaterials(data, entries, ctx) {
    // entries: [{kind, ref, filename}] in upload order → one commit for the whole batch
    if (data.materials.length + entries.length > MAX_MATERIALS) return fail("too_many_materials");
    const materials = data.materials.slice();
    const affected = [];
    for (const e of entries) {
        const id = newId("m", materials);
        materials.push({ id, kind: e.kind, ref: e.ref, name: aliasFromFilename(e.filename, materials.map((m) => m.name)) });
        affected.push(id);
    }
    return done(data, { ...data, materials }, ctx, affected);
}

export function deleteMaterial(data, id, ctx) {
    if (!material(data, id)) return fail("no_material");
    const gone = (x) => x.material === id;
    const affected = [id, ...data.refs.filter(gone).map((x) => x.id), ...data.anchors.filter(gone).map((x) => x.id)];
    return done(data, {
        ...data, materials: data.materials.filter((m) => m.id !== id),
        refs: data.refs.filter((x) => !gone(x)), anchors: data.anchors.filter((x) => !gone(x)),
    }, ctx, affected);
}

function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function renameMaterial(data, id, name, ctx) {
    const m = material(data, id);
    if (!m) return fail("no_material");
    if (!nameOk(name)) return fail("bad_name");
    if (data.materials.some((x) => x.id !== id && x.name === name)) return fail("duplicate_name");
    // Prompts follow the rename in the same commit, so `<@old>` never dangles.
    const pattern = new RegExp(`<@${escapeRegExp(m.name)}(:audio)?>`, "g");
    const rewrite = (text) => text.replace(pattern, (_, audio) => `<@${name}${audio ?? ""}>`);
    return done(data, {
        ...data, prompt: rewrite(data.prompt),
        materials: data.materials.map((x) => (x.id === id ? { ...x, name } : x)),
        segments: data.segments.map((s) => ({ ...s, prompt: rewrite(s.prompt) })),
    }, ctx, [id]);
}

const RAW_TAG = /<(Picture|Video|Audio) ([0-9]+)>/g;

// Raw `<Picture N>`-style tags of segment `index`'s effective prompt → `<@name>` aliases, written to
// the segment's own prompt. Only when every tag names exactly one material here and the resolved
// prompt stays identical, so the segment's model inputs (and caches) do not change.
export function aliasRawTags(data, index, ctx) {
    const v = ctx.h3 ? segmentInputs(data, index, ctx) : null;
    if (!v?.ok || !v.prompt.ok) return fail("ambiguous_tags");
    const names = new Map(data.materials.map((m) => [m.id, m.name]));
    const alias = new Map();
    for (const x of [...v.manifest.pictures, ...v.manifest.videos, ...v.manifest.audios]) alias.set(x.tag, `<@${names.get(x.material)}>`);
    for (const x of v.manifest.videos) if (x.audio_tag) alias.set(x.audio_tag, `<@${names.get(x.material)}:audio>`);
    let unmapped = false;
    const prompt = v.template.replace(RAW_TAG, (tag) => alias.get(tag) ?? ((unmapped = true), tag));
    if (prompt === v.template) return fail("no_raw_tags");
    const check = resolvePrompt(prompt, v.manifest, data.materials, index, true);
    if (unmapped || !check.ok || check.prompt !== v.prompt.prompt) return fail("ambiguous_tags");
    const segments = data.segments.map((s, i) => (i === index ? { ...s, prompt } : s));
    return done(data, { ...data, segments }, ctx, [String(index)]);
}

// ---- shot change / continue ----

function shiftItems(data, from, delta) {
    const warnings = [];
    // continuing a shot pulls its first frames into the previous segment's output
    const landed = (at, id) => at < from && warnings.push(`moved_into_previous:${id}`);
    const refs = data.refs.map((r) => {
        if (r.from >= from) {
            landed(r.from + delta, r.id);
            return { ...r, from: r.from + delta, to: r.to + delta };
        }
        if (r.to > from) warnings.push(`ref_crosses:${r.id}`);
        return r;
    });
    const anchors = data.anchors.map((a) => {
        if (a.at >= from) {
            landed(a.at + delta, a.id);
            return { ...a, at: a.at + delta };
        }
        if (a.length !== undefined && a.at + a.length > from) warnings.push(`anchor_crosses:${a.id}`);
        return a;
    });
    return { refs, anchors, warnings };
}

// cut=true: segment `index` starts a new shot (incoming overlap 0, seam cut, its sampling chain
// restarts); cut=false: it continues the previous shot with `overlap` frames. Later segments and
// the refs / anchors owned by them ripple with it; a ripple that breaks anything is refused.
export function setShotBoundary(data, index, cut, overlap, ctx) {
    if (index < 1 || index >= data.segments.length) return fail("first_segment");
    const prev = data.segments[index - 1];
    const seg = data.segments[index];
    const current = prev.end - seg.start;
    const target = cut ? 0 : overlap;
    const delta = current - target;
    if (delta === 0) return fail("unchanged");
    const boundary = prev.end;  // emit start of `index` before the edit
    const segments = data.segments.map((s, i) => (i < index ? s : {
        ...s, start: s.start + delta, end: s.end + delta, ...(i === index ? { seam_in: { kind: "cut" } } : {}),
    }));
    const { refs, anchors, warnings } = shiftItems(data, boundary, delta);
    return done(data, { ...data, segments, refs, anchors }, ctx, [String(index)], warnings);
}

// ---- material tag buttons (segment form, H3 only) ----

const TAG_CATEGORY = { image: "pictures", video: "videos", audio: "audios" };

function referenced(manifest, id) {
    return ["pictures", "videos", "audios"].some((k) => manifest[k].some((x) => x.material === id));
}

function problemAt(stage, error, index) {
    return { ok: false, reason: "breaks", problem: { stage, error, segment: index } };
}

// What each material's button can do, from one look at the current segment. "add" means it can be tried:
// useRefTag() validates the full result when clicked, and a refusal there says why and changes nothing.
// {hidden} off H3 (tags do not exist there); {blocked} when the segment cannot be read, so the row never
// pretends that nothing is a reference.
export function tagButtons(data, index, ctx) {
    if (!ctx.h3) return { hidden: true };
    const view = segmentInputs(data, index, ctx);
    if (!view) return { blocked: "not_ready" };
    if (!view.ok) return { blocked: "manifest", error: view.error };
    const snaps = verifiedSnapshots(data, ctx.snapshots);
    const items = {};
    for (const m of data.materials) {
        if (referenced(view.manifest, m.id)) {
            const r = resolvePrompt(`<@${m.name}>`, view.manifest, data.materials, index, true);
            items[m.id] = r.ok ? { state: "insert" } : { state: "blocked", reason: r.error.reason };
        } else if (!snaps[m.id]) {
            items[m.id] = { state: "blocked", reason: "not_ready" };
        } else {
            const category = TAG_CATEGORY[m.kind];
            items[m.id] = view.manifest[category].length >= H3_LIMITS[category]
                ? { state: "blocked", reason: "tag_full", category, limit: H3_LIMITS[category] } : { state: "add" };
        }
    }
    return { blocked: null, items };
}

// One click: the material becomes this segment's reference and its tag lands in the prompt, as one commit.
// The new tag itself must resolve; other unresolved tags may stay, so a shared prompt that needs two
// references can be completed one at a time.
export function useRefTag(data, index, materialId, ctx) {
    const m = material(data, materialId);
    if (!m) return fail("no_material");
    if (!ctx.h3) return fail("not_h3");
    const view = segmentInputs(data, index, ctx);
    if (!view || !snapOf(data, ctx, m.id)) return fail("not_ready");
    if (!view.ok) return problemAt("manifest", view.error, index);
    const tag = `<@${m.name}>`;
    const inherited = !data.segments[index].prompt && !!data.prompt;
    const isNew = !referenced(view.manifest, m.id);
    let next = data;
    if (isNew) {
        // a new reference renumbers the tags: numbered ones are first pinned to what they mean now
        if (view.template.search(RAW_TAG) >= 0) {
            const pinned = aliasRawTags(next, index, ctx);
            if (!pinned.ok) return fail("raw_tags_block");
            next = pinned.next;
        }
        const added = createFromCard(next, m.id, "refs", emitIntervals(next.segments)[index][0], ctx);
        if (!added.ok) return added;
        next = added.next;
    }
    const template = templateOf(next, index);
    // pressing ＋ for a tag the prompt already has only adds what it was missing: the reference
    const write = !(isNew && template.includes(tag));
    if (write) {
        const prompt = `${template}${template && !template.endsWith(" ") ? " " : ""}${tag}`;
        next = { ...next, segments: next.segments.map((s, i) => (i === index ? { ...s, prompt } : s)) };
    }
    const after = segmentInputs(next, index, ctx);
    if (!after) return fail("not_ready");
    if (!after.ok) return problemAt("manifest", after.error, index);
    const resolved = resolvePrompt(tag, after.manifest, next.materials, index, true);
    if (!resolved.ok) return problemAt("prompt", resolved.error, index);
    const nowOwn = !!next.segments[index].prompt;
    return done(data, next, ctx, [m.id, String(index)], inherited && nowOwn ? ["copied_shared"] : []);
}

// ---- what an edit did to each segment's references ----

function tagTable(data, index, ctx) {
    const view = segmentInputs(data, index, ctx);
    if (!view?.ok) return null;
    const m = view.manifest;
    const names = new Map(data.materials.map((x) => [x.id, x.name]));
    const tags = new Map();
    const note = (tag, x, suffix = "") => tags.set(tag, { binding: x.binding, material: x.material, start: x.source_start ?? null,
        length: x.effective_frames ?? x.frames ?? null, name: `${names.get(x.material)}${suffix}` });
    for (const x of [...m.pictures, ...m.videos, ...m.audios]) note(x.tag, x);
    for (const x of m.videos) if (x.audio_tag) note(x.audio_tag, x, ":audio");
    return {
        tags,
        used: new Set([...m.pictures, ...m.videos, ...m.audios].map((x) => names.get(x.material))),
        written: new Set([...view.template.matchAll(RAW_TAG)].map(([, kind, n]) => `<${kind} ${Number(n)}>`)),
    };
}

// A timeline can pass every check and still mean something else: a segment's reference moved away, or a
// numbered tag now names another material. Those stay legal, so the editor reports them instead of refusing.
export function refChanges(before, after, ctx) {
    if (!ctx.h3) return [];
    const out = [];
    const count = Math.min(before.segments.length, after.segments.length);
    for (let i = 0; i < count; i++) {
        const a = tagTable(before, i, ctx);
        const b = tagTable(after, i, ctx);
        if (!a || !b) continue;
        for (const [tag, was] of a.tags) {
            const now = b.tags.get(tag);
            if (!now || !b.written.has(tag) || now.binding === was.binding) continue;
            // another reference with the same content (a split, say) reads the same
            if (now.material === was.material && now.start === was.start && now.length === was.length) continue;
            out.push({ kind: "tag", segment: i, tag, from: was.name, to: now.name, fromStart: was.start, toStart: now.start, fromLength: was.length, toLength: now.length });
        }
        for (const name of a.used) if (!b.used.has(name)) out.push({ kind: "gone", segment: i, name });
        for (const name of b.used) if (!a.used.has(name)) out.push({ kind: "added", segment: i, name });
    }
    return out;
}

// ---- segment lengths and overlaps ----

// Fixed order, front to back: each segment's length, then its overlap, always on the latest candidate. Only
// illegal values change (later segments still shift with them), so this is not a global optimum. A fade that no
// longer fits is shortened. Prompt-level problems this causes are not refused: they are reported once the
// lengths are legal, and an undo restores everything.
export function conformSegments(data, ctx) {
    let segs = data.segments;
    let changed = false;
    for (let i = 0; i < segs.length; i++) {
        for (const [edge, illegal, frame] of [
            ["right", () => !lengthOk(ctx.grid, segs[i].end - segs[i].start), () => segs[i].end],
            ["left", () => i > 0 && !overlapOk(ctx.grid, segs[i - 1].end - segs[i].start), () => segs[i].start],
        ]) {
            if (!illegal()) continue;
            const r = editGenerate(segs, i, edge, frame(), ctx.grid);
            if (!r.ok) return fail(r.reason);
            segs = r.segments;
            changed = true;
        }
    }
    if (!changed) return fail("unchanged");
    const fades = [];
    segs = segs.map((s, i) => {
        if (i === 0 || s.seam_in?.kind !== "crossfade") return s;
        const limit = crossfadeLimit(segs, i);
        if (s.seam_in.frames <= limit) return s;
        fades.push(i);
        return { ...s, seam_in: limit < 1 ? { kind: "cut" } : { ...s.seam_in, frames: limit } };
    });
    const next = { ...data, segments: segs };
    const valid = validate(next, ctx.mode, ctx.fpsKey, ctx.grid, ctx.sourceFrames ?? null, !!ctx.hasSource);
    if (!valid.ok) return problemAt("grid", valid.error, segmentOf(valid.error.location));
    const result = done(data, next, ctx, segs.map((_, i) => String(i)));
    return result.ok ? { ...result, fades } : result;
}

// Auto-fill keeps what the user wrote: prompts and seeds by position, fades that still fit. Materials are not
// moved; if that breaks a placement the gate refuses, and prompt problems are for the preview to report.
export function refillSegments(data, count, length, overlap, ctx) {
    const filled = autoFill(count, length, overlap, ctx.grid);
    if (!filled.ok) return fail(filled.reason);
    const old = data.segments;
    const segs = filled.segments.map((s, i) => ({ ...s, prompt: old[i]?.prompt ?? "", seed: old[i]?.seed ?? null }));
    const seamReset = [];
    for (const [i, s] of segs.entries()) {
        const seam = old[i]?.seam_in;
        if (seam?.kind !== "crossfade") continue;
        if (i > 0 && seam.frames <= crossfadeLimit(segs, i)) s.seam_in = seam;
        else seamReset.push(i);
    }
    const removed = old.slice(count).map((_, k) => count + k);
    const result = done(data, { ...data, segments: segs }, ctx, segs.map((_, i) => String(i)));
    return result.ok ? { ...result, seamReset, removed } : result;
}

// ---- cut mode: mark in / out, add, delete (ranges of one source, in source order) ----

function sourceBounds(data, ctx) {
    if (!ctx.hasSource) return { error: fail("needs_source") };
    if (ctx.sourceFrames == null) return { error: fail("source_unknown") };
    const origin = data.source.origin_frame;
    return { origin, end: origin + ctx.sourceFrames };
}

// Marks commit directly, one undo each: there is no pending range to forget to apply. A start past the current
// end moves the whole range and keeps its length where there is room.
export function markCut(data, index, edge, frame, ctx) {
    const segs = data.segments;
    const s = segs[index];
    if (!s) return fail("no_item");
    const bounds = sourceBounds(data, ctx);
    if (bounds.error) return bounds.error;
    const f = Math.round(frame);
    const prev = segs[index - 1];
    const after = segs[index + 1];
    let range;
    let moved = false;
    if (edge === "left") {
        if (f < bounds.origin) return fail("before_source");
        if (f >= bounds.end) return fail("at_source_end");
        if (prev && f < prev.end) return fail("overlaps_segment", { segment: index - 1 });
        if (after && f >= after.start) return fail("overlaps_segment", { segment: index + 1 });
        moved = f >= s.end;
        range = { start: f, end: moved ? Math.min(f + (s.end - s.start), after ? after.start : bounds.end, bounds.end) : s.end };
    } else {
        if (f <= s.start) return fail("end_before_start");
        if (f > bounds.end) return fail("past_source_end");
        if (after && f > after.start) return fail("overlaps_segment", { segment: index + 1 });
        range = { start: s.start, end: f };
    }
    const next = { ...data, segments: segs.map((x, i) => (i === index ? { ...x, ...range } : x)) };
    const result = done(data, next, ctx, [String(index)]);
    return result.ok ? { ...result, moved, shortened: moved && range.end - range.start < s.end - s.start } : result;
}

export function insertCut(data, frame, length, ctx) {
    const segs = data.segments;
    const bounds = sourceBounds(data, ctx);
    if (bounds.error) return bounds.error;
    const f = Math.round(frame);
    if (f < bounds.origin) return fail("before_source");
    if (f >= bounds.end) return fail("at_source_end");
    const inside = segs.findIndex((s) => f >= s.start && f < s.end);
    if (inside >= 0) return fail("inside_segment", { segment: inside });
    const later = segs.findIndex((s) => s.start > f);
    const index = later < 0 ? segs.length : later;
    const end = Math.min(f + length, later < 0 ? bounds.end : segs[later].start, bounds.end);
    const added = { start: f, end, prompt: "", seam_in: { kind: "cut" }, seed: null };
    const result = done(data, { ...data, segments: [...segs.slice(0, index), added, ...segs.slice(index)] }, ctx, [String(index)]);
    return result.ok ? { ...result, index } : result;
}

export function deleteCut(data, index, ctx) {
    if (data.segments.length <= 1) return fail("last_segment");
    if (!data.segments[index]) return fail("no_item");
    return done(data, { ...data, segments: data.segments.filter((_, i) => i !== index) }, ctx, [String(index)]);
}

// Cut mode refuses materials, but hides the library: this is the way out. Files stay in input/; prompts are
// left alone (cut does not resolve them, and generate reports what is missing if the user goes back).
export function removeAllMaterials(data, ctx) {
    return done(data, { ...data, materials: [], refs: [], anchors: [] }, ctx, data.materials.map((m) => m.id));
}
