// MediaForge timeline editor — DOM widget for MF_EditTimeline.
//
// timeline_data (a plain STRING widget) stays the only persisted carrier: the editor
// reads and writes it, Python re-validates it on every run, and when the editor
// cannot start the STRING widget is simply left visible as a JSON text box.
// View state (zoom / active segment / playhead) lives in node.properties so it never
// reaches the prompt and never invalidates the node's cache.
//
// Edits follow one path: drags only update a draft (repainted on rAF) and commit on
// pointerup inside one beforeChange/afterChange pair, so one drag is one undo step.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
    FPS_PRESETS, H3_GRID, autoFill, canonicalize, carrySpec, crossfadeLimit, editCut, editGenerate, phaseMs, schemaError,
    validate,
} from "./timeline_grid.js";
import { S } from "./timeline_strings.js";

const NODE_TYPE = "MF_EditTimeline";
const VIEW_KEY = "mf_timeline_view";
const UPLOAD_SUBFOLDER = "mediaforge_timeline";
const NO_SOURCE = "(none)";
const EDITOR_HEIGHT = 470;
const MIN_NODE_WIDTH = 640;
const DEFAULT_OVERLAP = { "h3_5+17n": 22, "causal_4n+1": 5, "causal_8n+1": 9, free: 0 };
const SEGMENT_COLORS = ["#3d7bd9", "#d98a3d", "#3dae73", "#a95ad1", "#d14f6b", "#4cb0c2"];

function chainCallback(object, property, callback) {
    if (object == undefined) return;
    if (property in object) {
        const orig = object[property];
        object[property] = function () {
            const r = orig.apply(this, arguments);
            callback.apply(this, arguments);
            return r;
        };
    } else {
        object[property] = callback;
    }
}

function findWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function chainWidget(widget, extra) {
    if (!widget) return;
    const orig = widget.callback;
    widget.callback = function () {
        const r = orig?.apply(this, arguments);
        extra();
        return r;
    };
}

function changeTracker() {
    return app.extensionManager?.workflow?.activeWorkflow?.changeTracker ?? null;
}

function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "style") Object.assign(e.style, v);
        else if (k === "text") e.textContent = v;
        else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v);
    }
    for (const c of [].concat(children)) if (c != null) e.append(c);
    return e;
}

function splitSource(value) {
    const i = value.lastIndexOf("/");
    return i < 0 ? { subfolder: "", filename: value } : { subfolder: value.slice(0, i), filename: value.slice(i + 1) };
}

function viewURL({ filename, subfolder, type }) {
    return api.apiURL(`/view?filename=${encodeURIComponent(filename)}&subfolder=${encodeURIComponent(subfolder)}&type=${type}`);
}

function serialize(data) {
    return JSON.stringify({
        version: 1,
        source: { origin_frame: data.source.origin_frame },
        segments: data.segments.map((s) => ({
            start: s.start, end: s.end, prompt: s.prompt,
            ...(s.seam_in.kind === "crossfade" ? { seam_in: s.seam_in } : {}),
        })),
    });
}

function segmentIndexOf(location) {
    const m = /^segments\[(\d+)\]/.exec(location ?? "");
    return m ? Number(m[1]) : null;
}

let styleInstalled = false;
function installStyle() {
    if (styleInstalled) return;
    styleInstalled = true;
    document.head.append(el("style", { text: `
.mf-tl { box-sizing: border-box; width: 100%; height: 100%; display: flex; flex-direction: column; gap: 6px;
  font: 12px sans-serif; color: var(--fg-color, #ddd); outline: none; overflow: hidden; }
.mf-tl * { box-sizing: border-box; }
.mf-tl-bar { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.mf-tl-bar button, .mf-tl-form button { background: var(--comfy-input-bg, #222); color: inherit; border: 1px solid var(--border-color, #444);
  border-radius: 4px; padding: 2px 8px; cursor: pointer; }
.mf-tl-bar button:disabled { opacity: .45; cursor: default; }
.mf-tl-video { width: 100%; height: 130px; background: #000; border-radius: 4px; object-fit: contain; }
.mf-tl-label { opacity: .7; font-size: 11px; }
.mf-tl-scroller { position: relative; overflow-x: auto; overflow-y: hidden; height: 112px; border: 1px solid var(--border-color, #444);
  border-radius: 4px; background: var(--comfy-input-bg, #1a1a1a); touch-action: none; }
.mf-tl-track { position: relative; height: 100%; }
.mf-tl-ruler { position: absolute; left: 0; top: 0; height: 18px; }
.mf-tl-seg { position: absolute; height: ${30}px; border-radius: 4px; opacity: .85; cursor: pointer; overflow: hidden;
  font-size: 11px; line-height: 30px; padding: 0 10px; white-space: nowrap; color: #fff; }
.mf-tl-seg.active { outline: 2px solid #fff; opacity: 1; }
.mf-tl-seg.invalid { outline: 2px solid #ff5a5a; }
.mf-tl-handle { position: absolute; top: 0; width: 8px; height: 100%; cursor: ew-resize; background: rgba(255,255,255,.35); }
.mf-tl-handle[data-edge=left] { left: 0; } .mf-tl-handle[data-edge=right] { right: 0; }
.mf-tl-playhead { position: absolute; top: 0; width: 2px; height: 100%; background: #ffd23f; pointer-events: none; }
.mf-tl-form { flex: 1; overflow-y: auto; display: grid; grid-template-columns: auto 1fr; gap: 4px 8px; align-items: center; }
.mf-tl-form input, .mf-tl-form select, .mf-tl-form textarea { background: var(--comfy-input-bg, #222); color: inherit;
  border: 1px solid var(--border-color, #444); border-radius: 3px; padding: 2px 4px; font: inherit; }
.mf-tl-form textarea { resize: vertical; min-height: 38px; }
.mf-tl-panel { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; padding: 4px; border: 1px dashed var(--border-color, #555); border-radius: 4px; }
.mf-tl-panel input { width: 64px; } .mf-tl-panel textarea { width: 100%; min-height: 48px; }
.mf-tl-status { min-height: 16px; font-size: 11px; }
.mf-tl-status.bad { color: #ff7a7a; } .mf-tl-status.good { color: #8fd18f; }
` }));
}

class TimelineEditor {
    constructor(node) {
        this.node = node;
        this.w = Object.fromEntries(["timeline_data", "video", "fps", "grid", "mode"].map((n) => [n, findWidget(node, n)]));
        if (!this.w.timeline_data || typeof node.addDOMWidget !== "function") throw new Error("addDOMWidget unavailable");
        installStyle();
        this.revision = 0;
        this.requests = new Set();
        this.source = null;
        this.sourceError = null;
        this.proxyTried = false;
        this.drag = null;
        this.draft = null;
        this.raf = 0;
        this.notice = "";
        this.view = { active: 0, zoom: null, playhead: 0, ...(node.properties?.[VIEW_KEY] ?? {}) };
        this.build();
        this.resizeObserver = new ResizeObserver(() => this.scheduleTrack());
        this.widget = node.addDOMWidget("timeline_editor", "mf_timeline_editor", this.root, {
            serialize: false,
            hideOnZoom: false,
            surfaces: { canvas: "shown", vueNode: "shown", panel: "never" },
            getMinHeight: () => EDITOR_HEIGHT,
            getHeight: () => EDITOR_HEIGHT,
        });
        // options.serialize only keeps it out of the API prompt; the workflow's widgets_values
        // checks the widget's own flag, and an extra slot would misalign old saves once Edit
        // Timeline gains another widget (C2).
        this.widget.serialize = false;
        this.setJsonVisible(false);
        for (const name of ["fps", "grid", "mode"]) chainWidget(this.w[name], () => this.render());
        chainWidget(this.w.video, () => this.loadSource());
        // While broken, the raw JSON box is the editing surface; pick the timeline back up once it is fixed.
        chainWidget(this.w.timeline_data, () => this.broken && this.hydrate());
        this.resizeObserver.observe(this.scroller);
        if (node.size[0] < MIN_NODE_WIDTH) node.setSize([MIN_NODE_WIDTH, node.size[1]]);
        this.hydrate();
        this.loadSource();
    }

    // ---------- model ----------

    settings() {
        const fpsKey = this.w.fps?.value ?? "24";
        const fps = FPS_PRESETS[fpsKey] ?? [24, 1];
        const hasSource = !!this.w.video?.value && this.w.video.value !== NO_SOURCE;
        const d = this.source?.duration_us;
        const sourceFrames = hasSource && d != null ? Math.floor((d * fps[0]) / (fps[1] * 1e6)) : null;
        return { fpsKey, fps, grid: this.w.grid?.value ?? H3_GRID, mode: this.w.mode?.value ?? "generate", hasSource, sourceFrames };
    }

    hydrate() {
        // Malformed JSON stays as typed (never canonicalized and re-saved); the editor pauses
        // and shows the first failing location until the text is fixed.
        try {
            const parsed = JSON.parse(this.w.timeline_data.value);
            this.brokenAt = schemaError(parsed);
            if (this.brokenAt === null) this.data = canonicalize(parsed);
        } catch {
            this.brokenAt = "timeline_data";
        }
        this.broken = this.brokenAt !== null;
        this.setJsonVisible(this.broken);
        this.render();
    }

    commit(next, notice = "") {
        const tracker = changeTracker();
        tracker?.beforeChange?.();
        try {
            this.data = next;
            this.w.timeline_data.value = serialize(next);
            this.w.timeline_data.callback?.(this.w.timeline_data.value);
            this.node.setDirtyCanvas?.(true, true);
        } finally {
            tracker?.afterChange?.();
        }
        this.notice = notice;
        this.render();
    }

    saveView() {
        this.node.properties = this.node.properties ?? {};
        this.node.properties[VIEW_KEY] = { ...this.view };
    }

    setJsonVisible(visible) {
        const w = this.w.timeline_data;
        w.hidden = !visible;
        w.computeSize = visible ? null : () => [0, -4];
        this.node.setDirtyCanvas?.(true, true);
    }

    // ---------- DOM ----------

    build() {
        this.fileInput = el("input", { type: "file", accept: "video/*", style: { display: "none" },
            onchange: () => this.fileInput.files[0] && this.upload(this.fileInput.files[0]) });
        this.previewLabel = el("span", { class: "mf-tl-label" });
        this.video = el("video", { class: "mf-tl-video", muted: "", playsinline: "", preload: "metadata" });
        this.video.addEventListener("error", () => this.onVideoError());
        this.video.addEventListener("timeupdate", () => this.onVideoTime());
        this.frameLabel = el("span", { class: "mf-tl-label" });
        this.playButton = el("button", { text: S.play, onclick: () => this.togglePlay() });
        this.addButton = el("button", { text: S.add, onclick: () => this.addSegment() });
        this.deleteButton = el("button", { text: S.deleteLast, onclick: () => this.deleteLast() });
        this.fillButton = el("button", { text: S.autoFill, onclick: () => this.togglePanel("fill") });
        this.importButton = el("button", { text: S.importScenes, onclick: () => this.togglePanel("import") });
        this.bar = el("div", { class: "mf-tl-bar" }, [
            el("button", { text: S.upload, onclick: () => this.fileInput.click() }), this.fileInput,
            this.playButton, this.frameLabel,
            el("button", { text: S.zoomOut, onclick: () => this.zoom(0.5) }),
            el("button", { text: S.zoomIn, onclick: () => this.zoom(2) }),
            el("button", { text: S.fit, onclick: () => this.zoom(null) }),
            this.addButton, this.deleteButton, this.fillButton, this.importButton,
        ]);
        this.ruler = el("canvas", { class: "mf-tl-ruler", height: "18" });
        this.playhead = el("div", { class: "mf-tl-playhead" });
        this.track = el("div", { class: "mf-tl-track" }, [this.ruler, this.playhead]);
        this.scroller = el("div", { class: "mf-tl-scroller" }, [this.track]);
        this.track.addEventListener("pointerdown", (e) => this.onPointerDown(e));
        this.track.addEventListener("pointermove", (e) => this.onPointerMove(e));
        this.track.addEventListener("pointerup", (e) => this.onPointerUp(e));
        this.track.addEventListener("pointercancel", () => this.cancelDrag());
        this.track.addEventListener("lostpointercapture", () => this.drag && this.cancelDrag());
        this.scroller.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
        this.panel = el("div");
        this.form = el("div", { class: "mf-tl-form" });
        this.status = el("div", { class: "mf-tl-status" });
        this.root = el("div", { class: "mf-tl", tabindex: "0", onkeydown: (e) => this.onKey(e) }, [
            el("div", {}, [this.previewLabel]), this.video, this.bar, this.scroller, this.panel, this.form, this.status,
        ]);
        this.segEls = [];
    }

    render() {
        if (this.broken) {
            this.status.className = "mf-tl-status bad";
            this.status.textContent = `${S.jsonBroken}（${this.brokenAt}）`;
            this.form.replaceChildren();
            for (const b of [this.addButton, this.deleteButton, this.fillButton, this.importButton]) b.disabled = true;
            return;
        }
        const st = this.settings();
        this.result = validate(this.data, st.mode, st.fpsKey, st.grid, st.sourceFrames, st.hasSource);
        this.addButton.disabled = this.fillButton.disabled = this.importButton.disabled = false;
        this.deleteButton.disabled = this.data.segments.length <= 1;
        this.fillButton.style.display = st.mode === "generate" ? "" : "none";
        this.importButton.style.display = st.mode === "cut" ? "" : "none";
        this.renderTrack();
        this.renderForm(st);
        this.renderStatus(st);
    }

    scheduleTrack() {
        if (this.raf) return;
        this.raf = requestAnimationFrame(() => {
            this.raf = 0;
            this.renderTrack();
        });
    }

    pxPerFrame(extent) {
        const fit = Math.max(1, this.scroller.clientWidth - 4) / Math.max(1, extent);
        return this.view.zoom ? fit * this.view.zoom : fit;
    }

    renderTrack() {
        if (this.broken || !this.data) return;
        const st = this.settings();
        const segs = this.draft ?? this.data.segments;
        const extent = Math.max(...segs.map((s) => s.end), st.sourceFrames ?? 0, 1);
        this.px = this.pxPerFrame(extent);
        const width = Math.ceil(extent * this.px) + 2;
        this.track.style.width = `${width}px`;
        this.drawRuler(width, extent, st.fps);
        while (this.segEls.length > segs.length) this.segEls.pop().remove();
        while (this.segEls.length < segs.length) {
            const handles = ["left", "right"].map((edge) => el("div", { class: "mf-tl-handle", "data-edge": edge }));
            const label = el("span");
            const seg = el("div", { class: "mf-tl-seg" }, [label, ...handles]);
            seg.label = label;
            this.track.insertBefore(seg, this.playhead);
            this.segEls.push(seg);
        }
        const bad = this.result && !this.result.ok ? segmentIndexOf(this.result.error.location) : null;
        segs.forEach((s, i) => {
            const e = this.segEls[i];
            e.dataset.index = String(i);
            e.label.textContent = `GEN${i + 1} · ${s.end - s.start}f`;
            Object.assign(e.style, {
                left: `${s.start * this.px}px`, width: `${Math.max(6, (s.end - s.start) * this.px)}px`,
                top: `${22 + (st.mode === "generate" ? (i % 2) * 40 : 20)}px`, background: SEGMENT_COLORS[i % SEGMENT_COLORS.length],
            });
            e.classList.toggle("active", i === this.view.active);
            e.classList.toggle("invalid", i === bad);
        });
        this.playhead.style.left = `${this.view.playhead * this.px}px`;
        const seconds = (this.view.playhead * st.fps[1]) / st.fps[0];
        this.frameLabel.textContent = `${this.view.playhead}f · ${seconds.toFixed(2)}s`;
    }

    drawRuler(width, extent, fps) {
        const canvas = this.ruler;
        canvas.width = Math.min(width, 32767);
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "rgba(255,255,255,.55)";
        ctx.font = "10px sans-serif";
        const perSecond = fps[0] / fps[1];
        // below one second tick by frames, above it by whole seconds so labels stay round
        const frameSteps = [1, 2, 5, 10].filter((f) => f < perSecond);
        const secondSteps = [1, 2, 5, 10, 30, 60, 120, 300, 600].map((s) => Math.round(s * perSecond));
        const step = [...frameSteps, ...secondSteps].find((f) => f * this.px >= 60) ?? Math.ceil(extent / 10);
        for (let f = 0; f <= extent; f += step) {
            const x = f * this.px;
            ctx.fillRect(x, 10, 1, 8);
            ctx.fillText(step < perSecond ? `${f}f` : `${Math.round(f / perSecond)}s`, x + 2, 9);
        }
    }

    renderForm(st) {
        const segs = this.data.segments;
        const i = Math.min(this.view.active, segs.length - 1);
        const s = segs[i];
        const prev = segs[i - 1];
        const overlap = st.mode === "generate" && prev ? prev.end - s.start : 0;
        const sec = (f) => (f * st.fps[1]) / st.fps[0];
        const number = (value, onchange, attrs = {}) =>
            el("input", { type: "number", step: "1", value: String(value), onchange: (e) => onchange(Number(e.target.value)), ...attrs });
        const rows = [
            el("b", { text: S.segment(i + 1) }),
            el("span", { class: "mf-tl-label", text: `${S.seconds(sec(s.start))} → ${S.seconds(sec(s.end))}${overlap ? " · " + S.overlapIn(overlap) : ""}` }),
            el("span", { text: S.start }), number(s.start, (v) => this.editEdge(i, "left", v)),
            el("span", { text: S.end }), number(s.end, (v) => this.editEdge(i, "right", v)),
            el("span", { text: S.prompt }),
            el("textarea", { onchange: (e) => this.commit(this.withSegment(i, { prompt: e.target.value })) }, [s.prompt]),
        ];
        if (st.mode === "generate" && i > 0 && overlap > 0) {
            const limit = crossfadeLimit(segs, i);
            const kind = el("select", { onchange: (e) => this.commit(this.withSegment(i, {
                seam_in: e.target.value === "crossfade" ? { kind: "crossfade", frames: Math.min(6, limit), audio_curve: "auto" } : { kind: "cut" },
            })) }, [el("option", { value: "cut", text: S.seamCut }), el("option", { value: "crossfade", text: S.seamCrossfade })]);
            kind.value = s.seam_in.kind;
            rows.push(el("span", { text: S.seam }), kind);
            if (s.seam_in.kind === "crossfade") {
                const curve = el("select", { onchange: (e) => this.commit(this.withSegment(i, { seam_in: { ...s.seam_in, audio_curve: e.target.value } })) },
                    ["auto", "linear", "equal_power"].map((v) => el("option", { value: v, text: v })));
                curve.value = s.seam_in.audio_curve;
                rows.push(el("span", { text: S.crossfadeFrames(limit) }),
                    number(s.seam_in.frames, (v) => this.commit(this.withSegment(i, { seam_in: { ...s.seam_in, frames: v } })), { min: "1", max: String(limit) }),
                    el("span", { text: S.audioCurve }), curve);
            }
            if (st.grid === H3_GRID) {
                const ms = phaseMs(carrySpec(H3_GRID, prev.end - prev.start, overlap));
                rows.push(el("span"), el("span", { class: "mf-tl-label", text: ms ? S.phase(ms) : S.phaseZero }));
            }
        }
        this.form.replaceChildren(...rows);
    }

    renderStatus(st) {
        const r = this.result;
        if (r.ok) {
            this.status.className = "mf-tl-status good";
            this.status.textContent = `${S.valid} · ${S.total(r.totalFrames, (r.totalFrames * st.fps[1]) / st.fps[0])}${this.notice ? " · " + this.notice : ""}`;
        } else {
            this.status.className = "mf-tl-status bad";
            this.status.textContent = S.invalid(r.error.location, S.reasons[r.error.reason] ?? r.error.reason);
        }
        if (this.sourceError) this.status.textContent += ` · ${this.sourceError}`;
    }

    // ---------- editing ----------

    withSegment(i, patch) {
        return { ...this.data, segments: this.data.segments.map((s, j) => (j === i ? { ...s, ...patch } : s)) };
    }

    applyEdit(segs, i, edge, frame) {
        const st = this.settings();
        return st.mode === "cut" ? editCut(segs, i, edge, frame) : editGenerate(segs, i, edge, frame, st.grid);
    }

    editEdge(i, edge, frame) {
        const r = this.applyEdit(this.data.segments, i, edge, frame);
        if (!r.ok) {
            this.notice = `${S.noLegal}（${S.reasons[r.reason] ?? r.reason}）`;
            return this.render();
        }
        const landed = edge === "left" ? r.segments[i].start : r.segments[i].end;
        this.commit({ ...this.data, segments: r.segments }, landed === frame ? "" : S.snapNote);
    }

    addSegment() {
        const st = this.settings();
        const segs = this.data.segments;
        const last = segs[segs.length - 1];
        const length = last.end - last.start;
        let next;
        if (st.mode === "cut") {
            const end = st.sourceFrames != null ? Math.min(last.end + length, st.sourceFrames) : last.end + length;
            if (end <= last.end) return;
            next = { start: last.end, end, prompt: "", seam_in: { kind: "cut" } };
        } else {
            const prev = segs[segs.length - 2];
            const overlap = Math.min(prev ? prev.end - last.start : DEFAULT_OVERLAP[st.grid] ?? 0, length - 1);
            next = { start: last.end - overlap, end: last.end - overlap + length, prompt: "", seam_in: { kind: "cut" } };
        }
        this.view.active = segs.length;
        this.saveView();
        this.commit({ ...this.data, segments: [...segs, next] });
    }

    deleteLast() {
        const segs = this.data.segments;
        if (segs.length <= 1) return;
        this.view.active = Math.min(this.view.active, segs.length - 2);
        this.saveView();
        this.commit({ ...this.data, segments: segs.slice(0, -1) });
    }

    togglePanel(kind) {
        if (this.panelKind === kind) {
            this.panelKind = null;
            return this.panel.replaceChildren();
        }
        this.panelKind = kind;
        const close = () => {
            this.panelKind = null;
            this.panel.replaceChildren();
        };
        if (kind === "fill") {
            const st = this.settings();
            const count = el("input", { type: "number", value: "3", min: "1", max: "256" });
            const length = el("input", { type: "number", value: String(this.data.segments[0].end - this.data.segments[0].start) });
            const overlap = el("input", { type: "number", value: String(DEFAULT_OVERLAP[st.grid] ?? 0) });
            const preview = el("span", { class: "mf-tl-label" });
            const plan = () => autoFill(Number(count.value), Number(length.value), Number(overlap.value), st.grid);
            const update = () => {
                const r = plan();
                preview.textContent = r.ok ? S.total(r.segments.at(-1).end, (r.segments.at(-1).end * st.fps[1]) / st.fps[0]) : S.reasons[r.reason] ?? r.reason;
            };
            for (const input of [count, length, overlap]) input.addEventListener("input", update);
            update();
            this.panel.replaceChildren(el("div", { class: "mf-tl-panel" }, [
                el("span", { text: S.segments }), count, el("span", { text: S.length }), length, el("span", { text: S.overlap }), overlap, preview,
                el("button", { text: S.apply, onclick: () => {
                    const r = plan();
                    if (!r.ok) return;
                    close();
                    this.view.active = 0;
                    this.saveView();
                    this.commit({ ...this.data, segments: r.segments });
                } }),
                el("button", { text: S.cancel, onclick: close }),
            ]));
        } else {
            const text = el("textarea", { placeholder: S.importHint });
            this.panel.replaceChildren(el("div", { class: "mf-tl-panel" }, [
                text,
                el("button", { text: S.apply, onclick: () => {
                    const segs = this.scenesToSegments(text.value);
                    if (!segs) {
                        this.notice = S.noLegal;
                        return this.render();
                    }
                    close();
                    this.view.active = 0;
                    this.saveView();
                    this.commit({ ...this.data, segments: segs });
                } }),
                el("button", { text: S.cancel, onclick: close }),
            ]));
        }
    }

    scenesToSegments(text) {
        let ranges;
        try {
            ranges = JSON.parse(text);
        } catch {
            return null;
        }
        if (!Array.isArray(ranges)) return null;
        const { fps } = this.settings();
        const origin = this.data.source.origin_frame;
        const segs = [];
        for (const r of ranges) {
            if (!Array.isArray(r) || r.length !== 2 || !r.every(Number.isFinite)) return null;
            const start = Math.max(origin + Math.round((r[0] * fps[0]) / fps[1]), segs.at(-1)?.end ?? 0);
            const end = origin + Math.round((r[1] * fps[0]) / fps[1]);
            if (end > start) segs.push({ start, end, prompt: "", seam_in: { kind: "cut" } });
        }
        return segs.length && segs.length <= 256 ? segs : null;
    }

    zoom(factor) {
        this.view.zoom = factor === null ? null : Math.min(64, Math.max(1, (this.view.zoom ?? 1) * factor));
        this.saveView();
        this.renderTrack();
    }

    // ---------- pointer / keyboard ----------

    frameAt(clientX) {
        const rect = this.track.getBoundingClientRect();
        const scale = rect.width / (this.track.offsetWidth || rect.width || 1);  // graph zoom is already in rect
        return Math.max(0, Math.round((clientX - rect.left) / (this.px * scale)));
    }

    onPointerDown(e) {
        if (this.broken || e.button !== 0) return;
        e.stopPropagation();
        const segEl = e.target.closest(".mf-tl-seg");
        const handle = e.target.closest(".mf-tl-handle");
        if (segEl) {
            this.view.active = Number(segEl.dataset.index);
            this.saveView();
        } else {
            this.seek(this.frameAt(e.clientX));
        }
        if (handle && segEl) {
            e.preventDefault();
            this.drag = { index: Number(segEl.dataset.index), edge: handle.dataset.edge, pointerId: e.pointerId, base: this.data.segments };
            try {
                this.track.setPointerCapture(e.pointerId);
            } catch {
                // the pointer may already be gone (very fast click); the drag still works without capture
            }
        }
        this.render();
    }

    onPointerMove(e) {
        if (!this.drag || e.pointerId !== this.drag.pointerId) return;
        const r = this.applyEdit(this.drag.base, this.drag.index, this.drag.edge, this.frameAt(e.clientX));
        if (r.ok) {
            this.draft = r.segments;
            this.scheduleTrack();
        }
    }

    onPointerUp(e) {
        if (!this.drag || e.pointerId !== this.drag.pointerId) return;
        const draft = this.draft;
        this.drag = null;
        this.draft = null;
        if (this.track.hasPointerCapture?.(e.pointerId)) this.track.releasePointerCapture(e.pointerId);
        if (draft) this.commit({ ...this.data, segments: draft });
        else this.render();
    }

    cancelDrag() {
        this.drag = null;
        this.draft = null;
        this.renderTrack();
    }

    onKey(e) {
        if (e.target.closest("input, textarea, select")) return;
        let handled = true;
        if (e.key === " ") this.togglePlay();
        else if (e.key === "ArrowLeft") this.seek(this.view.playhead - (e.shiftKey ? 10 : 1));
        else if (e.key === "ArrowRight") this.seek(this.view.playhead + (e.shiftKey ? 10 : 1));
        else if (e.key === "Escape" && this.drag) this.cancelDrag();
        else handled = false;
        if (handled) {
            e.preventDefault();
            e.stopPropagation();
        }
    }

    // ---------- source preview ----------

    seek(frame) {
        const { fps } = this.settings();
        this.view.playhead = Math.max(0, frame);
        this.saveView();
        if (this.video.getAttribute("src")) {
            this.video.currentTime = Math.max(0, ((this.view.playhead - this.data.source.origin_frame) * fps[1]) / fps[0]);
        }
        this.renderTrack();
    }

    togglePlay() {
        if (!this.video.getAttribute("src")) return;
        if (this.video.paused) this.video.play().catch(() => {});
        else this.video.pause();
        this.playButton.textContent = this.video.paused ? S.play : S.pause;
    }

    onVideoTime() {
        if (this.video.paused || this.drag) return;
        const { fps } = this.settings();
        this.view.playhead = this.data.source.origin_frame + Math.round((this.video.currentTime * fps[0]) / fps[1]);
        this.scheduleTrack();
    }

    request(path, body) {
        const ctrl = new AbortController();
        this.requests.add(ctrl);
        return api.fetchApi(path, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctrl.signal,
        }).then(async (res) => ({ ok: res.ok, body: await res.json() })).finally(() => this.requests.delete(ctrl));
    }

    loadSource() {
        const value = this.w.video?.value;
        const rev = ++this.revision;
        for (const ctrl of this.requests) ctrl.abort();
        this.source = null;
        this.sourceError = null;
        this.proxyTried = false;
        this.proxyWanted = false;
        this.video.pause();
        if (!value || value === NO_SOURCE) {
            this.video.removeAttribute("src");
            this.video.load();
            this.previewLabel.textContent = S.noSource;
            return this.render();
        }
        this.sourceRef = splitSource(value);
        this.video.src = viewURL({ ...this.sourceRef, type: "input" });
        this.previewLabel.textContent = S.sourcePreview;
        this.request("/mediaforge/timeline/probe", { version: 1, source: { type: "input", ...this.sourceRef } })
            .then(({ ok, body }) => {
                if (rev !== this.revision) return;
                this.source = ok ? body : null;
                this.sourceError = ok ? null : body?.error?.message ?? S.previewError;
                this.render();
                // the <video> may have failed before the probe answered; the proxy needs its fingerprint
                if (ok && this.proxyWanted) this.makeProxy();
            })
            .catch((e) => {
                if (e.name !== "AbortError" && rev === this.revision) {
                    this.sourceError = S.previewError;
                    this.render();
                }
            });
        this.render();
    }

    onVideoError() {
        if (!this.video.getAttribute("src") || this.proxyTried) {
            this.previewLabel.textContent = S.previewError;
            return;
        }
        this.proxyTried = true;
        this.previewLabel.textContent = S.loading;
        this.makeProxy();
    }

    makeProxy() {
        const rev = this.revision;
        this.proxyWanted = !this.source?.fingerprint;
        if (this.proxyWanted) return;
        this.previewLabel.textContent = S.loading;
        this.request("/mediaforge/timeline/proxy", {
            version: 1, source: { type: "input", ...this.sourceRef }, fingerprint: this.source.fingerprint, preset: "timeline_v1",
        }).then(({ ok, body }) => {
            if (rev !== this.revision) return;
            if (!ok) {
                this.previewLabel.replaceChildren(`${S.previewError}：${body?.error?.message ?? ""} `,
                    el("button", { text: S.retry, onclick: () => this.makeProxy() }));
                return;
            }
            this.video.src = viewURL(body.view);
            this.previewLabel.textContent = S.proxyPreview;
        }).catch((e) => {
            if (e.name !== "AbortError" && rev === this.revision) this.previewLabel.textContent = S.previewError;
        });
    }

    async upload(file) {
        this.previewLabel.textContent = S.uploading;
        const form = new FormData();
        form.append("image", file);
        form.append("type", "input");
        form.append("subfolder", UPLOAD_SUBFOLDER);
        form.append("overwrite", "false");
        try {
            const res = await api.fetchApi("/upload/image", { method: "POST", body: form });
            if (!res.ok) throw new Error(String(res.status));
            const { name, subfolder } = await res.json();
            const value = subfolder ? `${subfolder}/${name}` : name;  // the server may have renamed the file
            const values = this.w.video.options?.values;
            if (Array.isArray(values) && !values.includes(value)) values.push(value);
            const tracker = changeTracker();
            tracker?.beforeChange?.();
            try {
                this.w.video.value = value;
                this.w.video.callback?.(value);
            } finally {
                tracker?.afterChange?.();
            }
        } catch (e) {
            this.previewLabel.textContent = `${S.uploadFailed}（${e.message}）`;
        } finally {
            this.fileInput.value = "";
        }
    }

    destroy() {
        for (const ctrl of this.requests) ctrl.abort();
        cancelAnimationFrame(this.raf);
        this.resizeObserver?.disconnect();
        this.video.pause();
        this.video.removeAttribute("src");
        this.video.load();
    }
}

function fallback(node, error) {
    console.error("[MediaForge] timeline editor:", error);
    node.mfTimeline?.destroy();
    node.mfTimeline = null;
    // a half-built editor must not sit next to the JSON box
    const editor = findWidget(node, "timeline_editor");
    if (editor) {
        editor.onRemove?.();
        node.widgets.splice(node.widgets.indexOf(editor), 1);
    }
    const w = findWidget(node, "timeline_data");
    if (w) {
        w.hidden = false;
        w.computeSize = null;
        w.label = `timeline_data — ${S.editorFailed} ${error?.message ?? error}`;
    }
    node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
    name: "MediaForge.TimelineEditor",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) return;
        chainCallback(nodeType.prototype, "onNodeCreated", function () {
            try {
                this.mfTimeline = new TimelineEditor(this);
            } catch (e) {
                fallback(this, e);
            }
        });
        chainCallback(nodeType.prototype, "onConfigure", function () {
            if (!this.mfTimeline) return;
            try {
                this.mfTimeline.view = { active: 0, zoom: null, playhead: 0, ...(this.properties?.[VIEW_KEY] ?? {}) };
                this.mfTimeline.hydrate();
                this.mfTimeline.loadSource();
            } catch (e) {
                fallback(this, e);
            }
        });
        chainCallback(nodeType.prototype, "onRemoved", function () {
            this.mfTimeline?.destroy();
        });
    },
});
