// MediaForge timeline v2 materials — pure functions shared by the timeline editor.
//
// Mirrors utils/timeline_materials.py (Python stays the authority and re-validates on every
// run); both sides are checked against tests/golden/timeline_materials_vectors.json. Failures
// come back as {ok: false, error: {code, ...fields}}: the message fields of Python's TimelineError
// plus its machine `detail` (§4.2.5) — `reason` code, `entity` {kind, id} and `field`, each only when
// it applies. Frame fields are capped at MAX_FRAME (2^31−1), so plain Number math on them is exact.

export const KINDS = ["image", "video", "audio"];
export const ANCHOR_ROLE = { image: "keyframe", video: "guide", audio: "lock" };
export const REF_MODES = ["follow", "fixed"];
export const MAX_MATERIALS = 64;
export const MAX_REFS = 256;
export const MAX_ANCHORS = 512;
export const MAX_FRAME = 2 ** 31 - 1;
export const MAX_NAME_CODEPOINTS = 32;
export const H3_LIMITS = { pictures: 9, videos: 3, audios: 3 };
export const H3_MIN_REF_FRAMES = 5;
// explicit ranges rather than \w / \p{L}: Python and JS disagree on some Unicode classes
const NAME_RANGES = [[0x30, 0x39], [0x41, 0x5a], [0x61, 0x7a], [0x5f, 0x5f], [0x2d, 0x2d],
    [0x3040, 0x30ff], [0x3400, 0x4dbf], [0x4e00, 0x9fff], [0xac00, 0xd7af], [0xf900, 0xfaff]];
const ID_RE = /^[A-Za-z0-9_-]{1,32}$/;
const WINDOWS_RESERVED = new Set(["CON", "PRN", "AUX", "NUL", ..."123456789".split("").flatMap((n) => [`COM${n}`, `LPT${n}`])]);
// Python's \s spelled out: JS \s lacks \x1c-\x1f and \x85 but adds
const ANNOTATION_RE = /[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]\[(input|output|temp)\]$/;
const UTF8 = new TextEncoder();

// JSON.parse silently keeps the last duplicate key and turns 5.0 into 5, so the editor could
// show — and later save — a timeline Python rejects. strictParse() rejects duplicate keys like
// Python's object_pairs_hook does (raw level, location "timeline_data"), and wraps non-integer
// number tokens in FloatToken so the field check fails at the same location Python's int check
// does. Every number in timeline_data is an integer, so no FloatToken is ever valid.
export class FloatToken {
    constructor(text) {
        this.text = text;
    }
}

export function strictParse(text) {
    let i = 0;
    const fail = (what) => {
        throw new SyntaxError(`${what} at ${i}`);
    };
    const ws = () => {
        while (i < text.length && " \t\n\r".includes(text[i])) i++;
    };
    const literal = (word, value) => {
        if (text.startsWith(word, i)) {
            i += word.length;
            return value;
        }
        return fail("unexpected token");
    };
    const string = () => {
        i++; // opening quote
        let out = "";
        for (;;) {
            if (i >= text.length) fail("unterminated string");
            const c = text[i++];
            if (c === '"') return out;
            if (c < " ") fail("control character in string");
            if (c !== "\\") {
                out += c;
                continue;
            }
            const e = text[i++];
            const simple = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" }[e];
            if (simple !== undefined) out += simple;
            else if (e === "u" && /^[0-9a-fA-F]{4}$/.test(text.slice(i, i + 4))) {
                out += String.fromCharCode(parseInt(text.slice(i, i + 4), 16));
                i += 4;
            } else fail("bad escape");
        }
    };
    const number = () => {
        const m = /^-?(?:0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(i));
        if (!m) fail("bad number");
        i += m[0].length;
        return m[1] || m[2] ? new FloatToken(m[0]) : Number(m[0]);
    };
    const value = () => {
        ws();
        const c = text[i];
        if (c === "{") {
            i++;
            const obj = {};
            const seen = new Set();
            ws();
            if (text[i] === "}") {
                i++;
                return obj;
            }
            for (;;) {
                ws();
                if (text[i] !== '"') fail("expected key");
                const key = string();
                if (seen.has(key)) fail(`duplicate key ${key}`);
                seen.add(key);
                ws();
                if (text[i++] !== ":") fail("expected ':'");
                // defineProperty: plain assignment of "__proto__" would swap the prototype instead of
                // adding a key, hiding an unknown field that Python rejects
                Object.defineProperty(obj, key, { value: value(), enumerable: true, writable: true, configurable: true });
                ws();
                const d = text[i++];
                if (d === "}") return obj;
                if (d !== ",") fail("expected ',' or '}'");
            }
        }
        if (c === "[") {
            i++;
            const arr = [];
            ws();
            if (text[i] === "]") {
                i++;
                return arr;
            }
            for (;;) {
                arr.push(value());
                ws();
                const d = text[i++];
                if (d === "]") return arr;
                if (d !== ",") fail("expected ',' or ']'");
            }
        }
        if (c === '"') return string();
        if (c === "t") return literal("true", true);
        if (c === "f") return literal("false", false);
        if (c === "n") return literal("null", null);
        return number();
    };
    const out = value();
    ws();
    if (i !== text.length) fail("trailing data");
    return out;
}

// ---- structure (parse_materials / parse_refs / parse_anchors; same check order and locations) ----

const LOCATION_RE = /^(segments|materials|refs|anchors)\[([0-9]+)\](?:\.(.+))?$/;

function detail(reason, entity, field) {
    const out = {};
    if (reason != null) out.reason = reason;
    if (entity != null) out.entity = entity;
    if (field != null) out.field = field;
    return out;
}

// Python's json_detail: `field` = the location inside its object; segments have no id, so their entity is the index.
export function jsonErrorDetail(location, entity = null, reason = null) {
    const m = LOCATION_RE.exec(location);
    if (m && m[1] === "segments") entity = { kind: "segment", id: Number(m[2]) };
    return detail(reason, entity, m ? m[3] : location === "timeline_data" ? null : location);
}

// no entity when the id itself is bad or repeats an earlier one: the GUI would locate a different object
function entityOf(kind, obj, seen) {
    return isObject(obj) && idOk(obj.id, seen) ? { kind, id: obj.id } : null;
}

function fail(location, entity = null) {
    return { ok: false, error: { code: "E_TIMELINE_JSON", location, ...jsonErrorDetail(location, entity) } };
}

function isObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v) && !(v instanceof FloatToken);
}

function onlyKeys(v, allowed) {
    return isObject(v) && Object.keys(v).every((k) => allowed.includes(k));
}

function frameOk(v, minimum = 0) {
    return Number.isInteger(v) && minimum <= v && v <= MAX_FRAME;
}

function idOk(v, seen) {
    return typeof v === "string" && ID_RE.test(v) && !seen.has(v);
}

function orDefault(v, fallback) {
    return v === undefined ? fallback : v;
}

export function nameOk(name) {
    if (typeof name !== "string") return false;
    const chars = [...name];
    return chars.length >= 1 && chars.length <= MAX_NAME_CODEPOINTS && name.normalize("NFC") === name
        && chars.every((c) => NAME_RANGES.some(([lo, hi]) => lo <= c.codePointAt(0) && c.codePointAt(0) <= hi));
}

// Literal rules of utils/timeline_source.check_relative_name (no file access).
export function refOk(rel) {
    // Python's encode("utf-8") raises on lone surrogates; TextEncoder would silently replace them
    if (typeof rel !== "string" || !rel || /\p{Cs}/u.test(rel)) return false;
    if (UTF8.encode(rel).length > 1024 || /[\x00-\x1f\x7f]/.test(rel)) return false;
    if (rel.includes(":") || ANNOTATION_RE.test(rel) || rel[0] === "/" || rel[0] === "\\") return false;
    return rel.split(/[\\/]/).every((part) => part !== "" && part !== "." && part !== ".."
        && !part.endsWith(".") && !part.endsWith(" ") && UTF8.encode(part).length <= 255
        && !WINDOWS_RESERVED.has(part.split(".")[0].toUpperCase()));
}

export function parseMaterials(raw) {
    if (!Array.isArray(raw) || raw.length > MAX_MATERIALS) return fail("materials");
    const out = [];
    const ids = new Set();
    const names = new Set();
    for (const [i, m] of raw.entries()) {
        const loc = `materials[${i}]`;
        const entity = entityOf("material", m, ids);
        if (!onlyKeys(m, ["id", "kind", "ref", "name"])) return fail(loc, entity);
        if (!idOk(m.id, ids)) return fail(`${loc}.id`);
        ids.add(m.id);
        if (!KINDS.includes(m.kind)) return fail(`${loc}.kind`, entity);
        if (!refOk(m.ref)) return fail(`${loc}.ref`, entity);
        if (!nameOk(m.name) || names.has(m.name)) return fail(`${loc}.name`, entity);
        names.add(m.name);
        out.push({ id: m.id, kind: m.kind, ref: m.ref.replaceAll("\\", "/"), name: m.name });
    }
    return { ok: true, materials: out };
}

export function parseRefs(raw, materials) {
    if (!Array.isArray(raw) || raw.length > MAX_REFS) return fail("refs");
    const byId = new Map(materials.map((m) => [m.id, m]));
    const out = [];
    const ids = new Set();
    for (const [i, r] of raw.entries()) {
        const loc = `refs[${i}]`;
        if (!isObject(r)) return fail(loc);
        const entity = entityOf("ref", r, ids);
        const m = byId.get(r.material);
        if (m === undefined) return fail(`${loc}.material`, entity);
        const mode = m.kind === "video" ? orDefault(r.mode, "follow") : null;
        if (m.kind === "video" && !REF_MODES.includes(mode)) return fail(`${loc}.mode`, entity);
        const allowed = {
            image: ["id", "material", "from", "to"],
            video: ["id", "material", "from", "to", "mode", "in", "audio", ...(mode === "fixed" ? ["length"] : [])],
            audio: ["id", "material", "from", "to", "in", "length"],
        }[m.kind];
        if (!onlyKeys(r, allowed)) return fail(loc, entity);
        if (!idOk(r.id, ids)) return fail(`${loc}.id`);
        ids.add(r.id);
        if (!frameOk(r.from)) return fail(`${loc}.from`, entity);
        if (!frameOk(r.to, r.from + 1)) return fail(`${loc}.to`, entity);
        const ref = { id: r.id, material: m.id, from: r.from, to: r.to };
        if (m.kind !== "image") {
            const start = orDefault(r.in, 0);
            if (!frameOk(start)) return fail(`${loc}.in`, entity);
            if (m.kind === "video") {
                const audio = orDefault(r.audio, false);
                if (typeof audio !== "boolean") return fail(`${loc}.audio`, entity);
                Object.assign(ref, { mode, in: start, audio });
            } else {
                ref.in = start;
            }
        }
        if (m.kind === "audio" || mode === "fixed") {
            if (!frameOk(r.length, 1)) return fail(`${loc}.length`, entity);
            ref.length = r.length;
        }
        out.push(ref);
    }
    return { ok: true, refs: out };
}

export function parseAnchors(raw, materials) {
    if (!Array.isArray(raw) || raw.length > MAX_ANCHORS) return fail("anchors");
    const byId = new Map(materials.map((m) => [m.id, m]));
    const out = [];
    const ids = new Set();
    for (const [i, a] of raw.entries()) {
        const loc = `anchors[${i}]`;
        if (!isObject(a)) return fail(loc);
        const entity = entityOf("anchor", a, ids);
        const m = byId.get(a.material);
        if (m === undefined) return fail(`${loc}.material`, entity);
        const role = ANCHOR_ROLE[m.kind];
        if (a.role !== role) return fail(`${loc}.role`, entity);
        const allowed = {
            keyframe: ["id", "material", "role", "at"],
            guide: ["id", "material", "role", "at", "in", "length", "audio"],
            lock: ["id", "material", "role", "at", "in", "length"],
        }[role];
        if (!onlyKeys(a, allowed)) return fail(loc, entity);
        if (!idOk(a.id, ids)) return fail(`${loc}.id`);
        ids.add(a.id);
        if (!frameOk(a.at)) return fail(`${loc}.at`, entity);
        const anchor = { id: a.id, material: m.id, role, at: a.at };
        if (role !== "keyframe") {
            const start = orDefault(a.in, 0);
            if (!frameOk(start)) return fail(`${loc}.in`, entity);
            if (!frameOk(a.length, 1)) return fail(`${loc}.length`, entity);
            Object.assign(anchor, { in: start, length: a.length });
        }
        if (role === "guide") {
            const audio = orDefault(a.audio, false);
            if (typeof audio !== "boolean") return fail(`${loc}.audio`, entity);
            anchor.audio = audio;
        }
        out.push(anchor);
    }
    return { ok: true, anchors: out };
}

// ---- geometry (needs material snapshots, keyed by material id) ----

// core's actual use of a ref video / guide clip: cropped down to 17k+5 (Python's floor modulo)
export function h3Crop(frames) {
    return frames - ((((frames - 5) % 17) + 17) % 17);
}

export function emitIntervals(segments) {
    return segments.map((s, i) => [i ? segments[i - 1].end : 0, s.end]);
}

function span(a) {
    return [a.at, a.at + orDefault(a.length, 1)];
}

// null when valid, else the first error in Python's order (refs → anchors → stream occupancy).
export function validateGeometry(refs, anchors, snapshots, segments, h3) {
    const refError = (r, reason, field) => ({ code: "E_REF", ref: r.id, ...detail(reason, { kind: "ref", id: r.id }, field) });
    const anchorError = (a, reason, field) => ({ code: "E_ANCHOR", anchor: a.id, ...detail(reason, { kind: "anchor", id: a.id }, field) });
    const total = segments[segments.length - 1].end;
    for (const r of refs) {
        const snap = snapshots[r.material];
        if (r.from >= total) return refError(r, "past_timeline", "from");
        if (snap.kind === "image") continue;
        const follow = r.mode === "follow";
        if (r.in + (follow ? r.to - r.from : r.length) > snap.frames) return refError(r, "past_end", follow ? "to" : "length");
        if (r.audio && !snap.has_audio) return refError(r, "no_audio", "audio");
    }
    const emits = emitIntervals(segments);
    for (const a of anchors) {
        const snap = snapshots[a.material];
        if (a.at >= total) return anchorError(a, "past_timeline", "at");
        if (a.role !== "keyframe" && a.in + a.length > snap.frames) return anchorError(a, "past_end", "length");
        if (a.audio && !snap.has_audio) return anchorError(a, "no_audio", "audio");
        if (a.role === "guide") {
            const [lo, hi] = span(a);
            if (!emits.some(([ea, ee]) => ea <= lo && hi <= ee)) return anchorError(a, "crosses_emit", "at");
            if (h3 && h3Crop(a.length) !== a.length) return anchorError(a, "h3_length", "length");
        }
    }
    const streams = [
        ["video", anchors.filter((a) => a.role === "keyframe" || a.role === "guide")],
        ["audio", anchors.filter((a) => a.role === "lock" || a.audio)],
    ];
    for (const [stream, members] of streams) {
        const spans = members.map((a) => [...span(a), a.id])
            .sort((x, y) => x[0] - y[0] || x[1] - y[1] || (x[2] < y[2] ? -1 : 1));
        for (let k = 1; k < spans.length; k++) {
            if (spans[k][0] < spans[k - 1][1]) {
                return { code: "E_ANCHOR_OVERLAP", stream, first: spans[k - 1][2], second: spans[k][2],
                    ...detail(null, { kind: "anchor", id: spans[k][2] }, "at") };
            }
        }
    }
    return null;
}

function item(snap, extra) {
    return { material: snap.id, name: snap.name, ref: snap.ref, fingerprint: snap.fingerprint, snapshot: snap, ...extra };
}

// Materials of segment `index` (generate mode); with h3, tags, limits and core's effective crop too.
export function segmentManifest(segments, index, refs, anchors, snapshots, h3, carryIn = null) {
    const { start: s, end: e } = segments[index];
    const [a0, e0] = emitIntervals(segments)[index];
    const length = e - s;
    const pictures = [], videos = [], audios = [], keyframes = [], guides = [], locks = [];
    const seenPictures = new Set();
    const seenAudios = new Set();
    for (const r of refs) {
        if (r.to <= a0 || r.from >= e0) continue;
        const snap = snapshots[r.material];
        if (snap.kind === "image") {
            if (!seenPictures.has(r.material)) {
                seenPictures.add(r.material);
                pictures.push(item(snap, { binding: r.id }));
            }
        } else if (snap.kind === "video") {
            let start, requested;
            if (r.mode === "follow") {
                const lo = Math.max(r.from, s);
                start = r.in + lo - r.from;
                requested = Math.min(r.to, e) - lo;
            } else {
                [start, requested] = [r.in, r.length];
            }
            let effective = requested;
            if (h3) {
                const n = Math.min(requested, length);
                if (n < H3_MIN_REF_FRAMES) {
                    return { ok: false, error: { code: "E_REF_SHORT", segment: index + 1, name: snap.name, frames: n,
                        ...detail(null, { kind: "ref", id: r.id }, r.mode === "fixed" ? "length" : null) } };
                }
                effective = h3Crop(n);
            }
            videos.push(item(snap, { binding: r.id, mode: r.mode, source_start: start,
                requested_frames: requested, effective_frames: effective, discarded_tail: requested - effective,
                soundtrack: r.audio }));
        } else {
            const key = JSON.stringify([r.material, r.in, r.length]);
            if (!seenAudios.has(key)) {
                seenAudios.add(key);
                audios.push(item(snap, { binding: r.id, source_start: r.in, frames: r.length }));
            }
        }
    }
    for (const a of anchors) {
        const [lo, hi] = span(a);
        const snap = snapshots[a.material];
        if (a.role === "keyframe") {
            if (a0 <= lo && lo < e0) keyframes.push(item(snap, { anchor: a.id, frame_idx: lo - s }));
        } else if (a.role === "guide") {
            if (a0 <= lo && hi <= e0) {
                guides.push(item(snap, { anchor: a.id, frame_idx: lo - s, source_start: a.in,
                    frames: a.length, soundtrack: a.audio }));
            }
        } else if (lo < e && hi > s) {
            const p = Math.max(lo, s) - s;
            const q = Math.min(hi, e) - s;
            const lock = item(snap, { anchor: a.id, frame_idx: p, frames: q - p, source_start: a.in + Math.max(lo, s) - lo });
            if (h3) lock.tokens = [Math.ceil((5 * p) / 3), Math.floor((5 * q) / 3)];
            locks.push(lock);
        }
    }
    for (const group of [keyframes, guides, locks]) group.sort((x, y) => x.frame_idx - y.frame_idx);
    const carried = h3 && carryIn && "audio_tokens" in carryIn ? carryIn.audio_tokens : 0;
    const manifest = { pictures, videos, audios, keyframes, guides, locks,
        carry_conflicts: h3 ? locks.filter((x) => x.tokens[0] < carried).map((x) => x.anchor) : [] };
    if (h3) {
        const error = h3Tags(manifest, index);
        if (error) return { ok: false, error };
        h3Samples(manifest);
    }
    return { ok: true, manifest };
}

// samples actually sent to the H3 audio VAE (Python's _h3_samples): Q32k(frames) cut down to whole tokens
// Python's audio_sample_count: Q(end) − Q(start) on global endpoints (§4.3), cut down to a multiple
export function audioSampleCount(dstStart, frames, rate, fps, multiple = 1) {
    const [num, den] = fps.map(BigInt);
    const q = (f) => (2n * BigInt(f) * BigInt(rate) * den + num) / (2n * num);
    const count = Number(q(dstStart + frames) - q(dstStart));
    return count - (count % multiple);
}

function h3Samples(manifest) {
    const samples = (start, frames) => audioSampleCount(start, frames, 32000, [24, 1], 800);
    for (const v of manifest.videos) if (v.soundtrack) v.soundtrack_samples = samples(v.source_start, v.effective_frames);
    for (const a of manifest.audios) a.samples = samples(a.source_start, a.frames);
    for (const g of manifest.guides) if (g.soundtrack) g.samples = samples(g.frame_idx, g.frames);
}

function h3Tags(manifest, index) {
    for (const [kind, limit] of Object.entries(H3_LIMITS)) {
        const count = manifest[kind].length;
        if (count > limit) return { code: "E_REF_LIMIT", segment: index + 1, kind, count, limit, ...detail(null, { kind: "segment", id: index }) };
    }
    manifest.pictures.forEach((p, n) => {
        p.tag = `<Picture ${n + 1}>`;
    });
    // core order: pictures → videos (a paired soundtrack's <Audio j> right before its <Video k>) → standalone audio
    let audioN = 0;
    manifest.videos.forEach((v, n) => {
        v.tag = `<Video ${n + 1}>`;
        if (v.soundtrack) v.audio_tag = `<Audio ${++audioN}>`;
    });
    for (const a of manifest.audios) a.tag = `<Audio ${++audioN}>`;
    return null;
}

// ---- prompt aliases ----

const ALIAS_RE = /<@([^<>]*)>/g;
// core only writes ASCII digits (mirrors Python's [0-9]+)
const TAG_RE = /<(Picture|Video|Audio) ([0-9]+)>/g;

export function tagCounts(manifest) {
    return {
        Picture: manifest.pictures.length,
        Video: manifest.videos.length,
        Audio: manifest.videos.filter((v) => v.soundtrack).length + manifest.audios.length,
    };
}

// `<@name>` / `<@name:audio>` → this segment's tag; hand-written `<Picture N>`-style tags are range-checked.
export function resolvePrompt(template, manifest, materials, index, h3) {
    const fail = (reason) => ({ ok: false, error: { code: "E_PROMPT_REF", segment: index + 1,
        ...detail(reason, { kind: "segment", id: index }, "prompt") } });
    if (!h3) return template.search(ALIAS_RE) >= 0 || template.search(TAG_RE) >= 0 ? fail("not_h3") : { ok: true, prompt: template };
    const counts = tagCounts(manifest);
    for (const [, kind, n] of template.matchAll(TAG_RE)) {
        if (n.length > 9 || !(Number(n) >= 1 && Number(n) <= counts[kind])) return fail("tag_range");
    }
    const byName = new Map(materials.map((m) => [m.name, m]));
    let prompt = "";
    let last = 0;
    for (const match of template.matchAll(ALIAS_RE)) {
        const wantAudio = match[1].endsWith(":audio");
        const m = byName.get(wantAudio ? match[1].slice(0, -":audio".length) : match[1]);
        if (m === undefined) return fail("unknown_name");
        const hits = ["pictures", "videos", "audios"].flatMap((k) => manifest[k].filter((x) => x.material === m.id));
        if (hits.length > 1) return fail("ambiguous");
        if (!hits.length) {
            const anchored = ["keyframes", "guides", "locks"].some((k) => manifest[k].some((x) => x.material === m.id));
            return fail(anchored ? "anchor_only" : "not_in_segment");
        }
        if (wantAudio && !("audio_tag" in hits[0])) return fail("no_soundtrack");
        prompt += template.slice(last, match.index) + (wantAudio ? hits[0].audio_tag : hits[0].tag);
        last = match.index + match[0].length;
    }
    return { ok: true, prompt: prompt + template.slice(last) };
}
