// MediaForge timeline v2 edit commands — pure functions used by the timeline editor.
//
// Every command takes the canonical v2 timeline (version / source / prompt / materials / refs /
// anchors / segments) and returns {ok, next, affected, warnings} or {ok: false, reason}; the
// editor commits `next` as one undo step. Python re-validates everything on the next run, so
// these only aim to produce legal data and to say up front when an edit changes model inputs.

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

function fail(reason) {
    return { ok: false, reason };
}

function done(next, affected, warnings = []) {
    return { ok: true, next, affected, warnings };
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

// ---- create (card dropped on a track) ----

// lane: "refs" | "visual" | "audio"
export function createFromCard(data, materialId, lane, frame, snapshot, h3) {
    const m = material(data, materialId);
    if (!m) return fail("no_material");
    const length = snapshot?.frames;
    if (m.kind !== "image" && !Number.isSafeInteger(length)) return fail("not_ready");
    const emits = emitIntervals(data.segments);
    const end = total(data);
    const f = Math.max(0, Math.min(end - 1, Math.round(frame)));
    const [lo, hi] = emits[segmentAt(data.segments, f)];
    if (lane === "refs") {
        const id = newId("r", data.refs);
        let ref;
        if (m.kind === "image") ref = { id, material: m.id, from: lo, to: hi };
        else if (m.kind === "video") {
            const to = Math.min(hi, lo + length);
            if (to <= lo) return fail("material_too_short");
            ref = { id, material: m.id, from: lo, to, mode: "follow", in: 0, audio: !!snapshot.has_audio };
        } else {
            if (length < 1) return fail("material_too_short");
            ref = { id, material: m.id, from: lo, to: hi, in: 0, length };
        }
        return done({ ...data, refs: [...data.refs, ref] }, [id]);
    }
    const id = newId("a", data.anchors);
    if (lane === "visual" && m.kind === "image") {
        return done({ ...data, anchors: [...data.anchors, { id, material: m.id, role: "keyframe", at: f }] }, [id]);
    }
    if (lane === "visual" && m.kind === "video") {
        let len = h3 ? H3_DEFAULT_GUIDE : Math.min(length, hi - lo);
        if (h3 && (length < 5 || hi - lo < 5)) return fail("no_room");
        if (h3) len = Math.min(len, h3Crop(Math.min(length, hi - lo)));
        let at = f;
        if (at + len > hi) at = hi - len;
        if (at < lo || len < 1) return fail("no_room");
        return done({ ...data, anchors: [...data.anchors, { id, material: m.id, role: "guide", at, in: 0, length: len, audio: false }] }, [id]);
    }
    if (lane === "audio" && m.kind === "audio") {
        const len = Math.min(length, end - f);
        if (len < 1) return fail("no_room");
        return done({ ...data, anchors: [...data.anchors, { id, material: m.id, role: "lock", at: f, in: 0, length: len }] }, [id]);
    }
    return fail("wrong_lane");
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
export function editBar(data, kind, id, op, delta, snapshot, h3) {
    const list = kind === "ref" ? data.refs : data.anchors;
    const item = list.find((x) => x.id === id);
    if (!item) return fail("no_item");
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
        return done({ ...data, refs: replace(data.refs, id, next) }, [id]);
    }
    if (op === "move") next.at = item.at + d;
    else if (item.role === "keyframe") return fail("keyframe_has_no_length");
    else if (op === "left") {
        let len = item.length - d;
        if (item.role === "guide" && h3) len = h3SnapLength(len);
        const dd = item.length - len;
        next = { ...next, at: item.at + dd, in: item.in + dd, length: len };
    } else {
        let len = item.length + d;
        if (item.role === "guide" && h3) len = h3SnapLength(len);
        next.length = len;
    }
    const bad = checkAnchor(next, data.segments, snapshot, h3);
    if (bad) return fail(bad);
    return done({ ...data, anchors: replace(data.anchors, id, next) }, [id]);
}

function h3SnapLength(len) {
    if (len < 5) return 5;
    const down = h3Crop(len);
    return len - down > 8 ? down + 17 : down;  // nearest legal 17k+5
}

export function splitBar(data, kind, id, frame, h3) {
    const f = Math.round(frame);
    if (kind === "ref") {
        const r = data.refs.find((x) => x.id === id);
        if (!r) return fail("no_item");
        // Refs split on a segment boundary, so each half belongs to different segments and no
        // segment silently gains a second copy of the reference.
        const cuts = emitIntervals(data.segments).map(([lo]) => lo).filter((b) => b > r.from && b < r.to);
        if (!cuts.length) return fail("no_boundary");
        const b = cuts.reduce((best, c) => (Math.abs(c - f) < Math.abs(best - f) ? c : best));
        const left = { ...r, to: b };
        const right = { ...r, id: newId("r", data.refs), from: b };
        if (r.mode === "follow") right.in = r.in + (b - r.from);
        const refs = data.refs.flatMap((x) => (x.id === id ? [left, right] : [x]));
        return done({ ...data, refs }, [left.id, right.id]);
    }
    const a = data.anchors.find((x) => x.id === id);
    if (!a) return fail("no_item");
    if (a.role === "keyframe") return fail("keyframe_has_no_length");
    if (a.role === "guide" && h3) return fail("guide_split_h3");
    if (f <= a.at || f >= a.at + a.length) return fail("outside_bar");
    const left = { ...a, length: f - a.at };
    const right = { ...a, id: newId("a", data.anchors), at: f, in: a.in + (f - a.at), length: a.at + a.length - f };
    const anchors = data.anchors.flatMap((x) => (x.id === id ? [left, right] : [x]));
    return done({ ...data, anchors }, [left.id, right.id]);
}

export function deleteBar(data, kind, id) {
    const key = kind === "ref" ? "refs" : "anchors";
    if (!data[key].some((x) => x.id === id)) return fail("no_item");
    return done({ ...data, [key]: data[key].filter((x) => x.id !== id) }, [id]);
}

export function moveRefInOrder(data, id, step) {
    const i = data.refs.findIndex((x) => x.id === id);
    const j = i + step;
    if (i < 0 || j < 0 || j >= data.refs.length) return fail("no_move");
    const refs = data.refs.slice();
    [refs[i], refs[j]] = [refs[j], refs[i]];
    return done({ ...data, refs }, [id], ["tag_order_changed"]);
}

// ---- materials ----

export function addMaterials(data, entries) {
    // entries: [{kind, ref, filename}] in upload order → one commit for the whole batch
    const materials = data.materials.slice();
    const affected = [];
    for (const e of entries) {
        const id = newId("m", materials);
        materials.push({ id, kind: e.kind, ref: e.ref, name: aliasFromFilename(e.filename, materials.map((m) => m.name)) });
        affected.push(id);
    }
    return done({ ...data, materials }, affected);
}

export function deleteMaterial(data, id) {
    if (!material(data, id)) return fail("no_material");
    const gone = (x) => x.material === id;
    const affected = [id, ...data.refs.filter(gone).map((x) => x.id), ...data.anchors.filter(gone).map((x) => x.id)];
    return done({
        ...data, materials: data.materials.filter((m) => m.id !== id),
        refs: data.refs.filter((x) => !gone(x)), anchors: data.anchors.filter((x) => !gone(x)),
    }, affected);
}

function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function renameMaterial(data, id, name, nameOk) {
    const m = material(data, id);
    if (!m) return fail("no_material");
    if (!nameOk(name)) return fail("bad_name");
    if (data.materials.some((x) => x.id !== id && x.name === name)) return fail("duplicate_name");
    // Prompts follow the rename in the same commit, so `<@old>` never dangles.
    const pattern = new RegExp(`<@${escapeRegExp(m.name)}(:audio)?>`, "g");
    const rewrite = (text) => text.replace(pattern, (_, audio) => `<@${name}${audio ?? ""}>`);
    return done({
        ...data, prompt: rewrite(data.prompt),
        materials: data.materials.map((x) => (x.id === id ? { ...x, name } : x)),
        segments: data.segments.map((s) => ({ ...s, prompt: rewrite(s.prompt) })),
    }, [id]);
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
// the refs / anchors owned by them ripple with it.
export function setShotBoundary(data, index, cut, overlap) {
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
    return done({ ...data, segments, refs, anchors }, [String(index)], warnings);
}
