// MediaForge timeline editor — DOM widget for MF_EditTimeline.
//
// timeline_data (a plain STRING widget) stays the only persisted carrier: the editor
// reads and writes it, Python re-validates it on every run, and when the editor
// cannot start the STRING widget is simply left visible as a JSON text box.
// View state (zoom / selection / playhead) lives in node.properties so it never
// reaches the prompt and never invalidates the node's cache.
//
// Edits follow one path: drags only update a draft (repainted on rAF) and commit on
// pointerup inside one beforeChange/afterChange pair, so one drag is one undo step.
// Material snapshots (length, audio, size) come from the probe route and live only in
// this editor; until a material is verified the panels say so instead of guessing.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
    FPS_PRESETS, H3_GRID, autoFill, canonicalize, carrySpec, crossfadeLimit, editCut, editGenerate, phaseMs, schemaError,
    validate,
} from "./timeline_grid.js";
import { nameOk, refOk, resolvePrompt, segmentManifest, strictParse, validateGeometry } from "./timeline_materials.js";
import {
    addMaterials, createFromCard, deleteBar, deleteMaterial, editBar, emitIntervals, moveRefInOrder, renameMaterial,
    setShotBoundary, splitBar,
} from "./timeline_edit.js";
import { S } from "./timeline_strings.js";

const NODE_TYPE = "MF_EditTimeline";
const VIEW_KEY = "mf_timeline_view";
const UPLOAD_SUBFOLDER = "mediaforge_timeline";
const NO_SOURCE = "(none)";
const EDITOR_HEIGHT = 680;
const MIN_NODE_WIDTH = 700;
const DEFAULT_OVERLAP = { "h3_5+17n": 22, "causal_4n+1": 5, "causal_8n+1": 9, free: 0 };
const SEGMENT_COLORS = ["#3d7bd9", "#d98a3d", "#3dae73", "#a95ad1", "#d14f6b", "#4cb0c2"];
const KIND_COLORS = { image: "#5c8f3a", video: "#3a6f8f", audio: "#8f6a3a" };
const KIND_ICON = { image: "🖼", video: "🎞", audio: "🔊" };
const LANE_TOP = 100;
const LANE_ROW = 22;
const SNAP_PX = 8;
const UPLOAD_CONCURRENCY = 2;
const AUDIO_EXT = /\.(wav|mp3|flac|ogg|aac|m4a|opus)$/i;
const VIDEO_EXT = /\.(mp4|mov|mkv|webm|avi|m4v|ts|mpg|mpeg)$/i;
const IMAGE_EXT = /\.(png|jpe?g|webp|bmp|gif|tiff?)$/i;

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

function kindOf(file) {
    const t = file.type || "";
    if (t.startsWith("image/") || IMAGE_EXT.test(file.name)) return "image";
    if (t.startsWith("video/") || VIDEO_EXT.test(file.name)) return "video";
    if (t.startsWith("audio/") || AUDIO_EXT.test(file.name)) return "audio";
    return null;
}

function serialize(data) {
    return JSON.stringify({
        version: 2,
        source: { origin_frame: data.source.origin_frame },
        prompt: data.prompt,
        materials: data.materials,
        refs: data.refs,
        anchors: data.anchors,
        segments: data.segments.map((s) => ({
            start: s.start, end: s.end, prompt: s.prompt,
            ...(s.seam_in?.kind === "crossfade" ? { seam_in: s.seam_in } : {}),
            ...(s.seed != null ? { seed: s.seed } : {}),
        })),
    });
}

function segmentIndexOf(location) {
    const m = /^segments\[(\d+)\]/.exec(location ?? "");
    return m ? Number(m[1]) : null;
}

// greedy interval packing: refs may overlap in time, so they get as many rows as needed
function packRows(items, span) {
    const ends = [];
    const rowOf = new Map();
    for (const item of [...items].sort((a, b) => span(a)[0] - span(b)[0])) {
        const [lo, hi] = span(item);
        let row = ends.findIndex((end) => end <= lo);
        if (row < 0) row = ends.push(hi) - 1;
        else ends[row] = hi;
        rowOf.set(item.id, row);
    }
    return { rows: Math.max(1, ends.length), rowOf };
}

function barSpan(kind, item) {
    if (kind === "ref") return [item.from, item.to];
    return [item.at, item.at + (item.length ?? 1)];
}

let styleInstalled = false;
function installStyle() {
    if (styleInstalled) return;
    styleInstalled = true;
    document.head.append(el("style", { text: `
.mf-tl { box-sizing: border-box; width: 100%; height: 100%; display: flex; flex-direction: column; gap: 5px;
  font: 12px sans-serif; color: var(--fg-color, #ddd); outline: none; overflow: hidden; }
.mf-tl * { box-sizing: border-box; }
.mf-tl:focus-visible, .mf-tl button:focus-visible, .mf-tl-card:focus-visible { outline: 2px solid #ffd23f; }
.mf-tl-bar { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.mf-tl-bar button, .mf-tl-form button, .mf-tl-lib button { background: var(--comfy-input-bg, #222); color: inherit;
  border: 1px solid var(--border-color, #444); border-radius: 4px; padding: 2px 8px; cursor: pointer; }
.mf-tl-bar button:disabled, .mf-tl-form button:disabled { opacity: .45; cursor: default; }
.mf-tl-top { display: flex; gap: 6px; height: 118px; }
.mf-tl-video { width: 210px; height: 118px; background: #000; border-radius: 4px; object-fit: contain; flex: none; }
.mf-tl-lib { flex: 1; display: flex; gap: 4px; overflow-x: auto; overflow-y: hidden; align-items: stretch;
  border: 1px dashed var(--border-color, #555); border-radius: 4px; padding: 4px; }
.mf-tl-lib.drop { border-color: #ffd23f; }
.mf-tl-card { flex: none; width: 84px; display: flex; flex-direction: column; gap: 2px; padding: 3px; border-radius: 4px;
  background: var(--comfy-input-bg, #262626); border: 1px solid transparent; cursor: grab; touch-action: none; user-select: none; }
.mf-tl-card.selected { border-color: #fff; }
.mf-tl-card.pending { cursor: default; opacity: .6; }
.mf-tl-card img { width: 100%; height: 46px; object-fit: cover; border-radius: 3px; background: #111; pointer-events: none; }
.mf-tl-card .icon { height: 46px; display: flex; align-items: center; justify-content: center; font-size: 22px; background: #111; border-radius: 3px; }
.mf-tl-card .name { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mf-tl-card .state { font-size: 10px; opacity: .75; }
.mf-tl-card .state.error { color: #ff7a7a; opacity: 1; }
.mf-tl-ghost { position: fixed; z-index: 10000; pointer-events: none; padding: 2px 8px; border-radius: 4px; background: #ffd23f;
  color: #000; font: 12px sans-serif; transform: translate(8px, 8px); }
.mf-tl-label { opacity: .7; font-size: 11px; }
.mf-tl-scroller { position: relative; overflow: auto; height: 220px; flex: none; border: 1px solid var(--border-color, #444);
  border-radius: 4px; background: var(--comfy-input-bg, #1a1a1a); touch-action: pan-x pan-y; }
.mf-tl-track { position: relative; }
.mf-tl-ruler { position: absolute; left: 0; top: 0; height: 18px; }
.mf-tl-lane { position: absolute; left: 0; height: ${LANE_ROW - 2}px; background: rgba(255,255,255,.04); pointer-events: none; }
.mf-tl-lane.drop { background: rgba(255,210,63,.18); }
.mf-tl-lane span { position: sticky; left: 2px; font-size: 10px; opacity: .55; line-height: ${LANE_ROW - 2}px; }
.mf-tl-seg { position: absolute; height: 30px; border-radius: 4px; opacity: .85; cursor: pointer; overflow: hidden;
  font-size: 11px; line-height: 30px; padding: 0 10px; white-space: nowrap; color: #fff; touch-action: none; }
.mf-tl-seg.active { outline: 2px solid #fff; opacity: 1; }
.mf-tl-seg.invalid { outline: 2px solid #ff5a5a; }
.mf-tl-seg .overlap { position: absolute; top: 0; left: 0; height: 100%;
  background: repeating-linear-gradient(45deg, rgba(0,0,0,.35) 0 4px, transparent 4px 8px); pointer-events: none; }
.mf-tl-handle { position: absolute; top: 0; width: 8px; height: 100%; cursor: ew-resize; background: rgba(255,255,255,.35); }
.mf-tl-handle[data-edge=left] { left: 0; } .mf-tl-handle[data-edge=right] { right: 0; }
.mf-tl-item { position: absolute; height: ${LANE_ROW - 4}px; border-radius: 3px; color: #fff; font-size: 10px;
  line-height: ${LANE_ROW - 4}px; padding: 0 8px; white-space: nowrap; overflow: hidden; cursor: grab; touch-action: none; }
.mf-tl-item.selected { outline: 2px solid #fff; }
.mf-tl-item.keyframe { width: 12px !important; padding: 0; transform: translateX(-6px) rotate(45deg) scale(.7); border-radius: 2px; }
.mf-tl-item .mf-tl-handle { width: 6px; }
.mf-tl-playhead { position: absolute; top: 0; width: 2px; height: 100%; background: #ffd23f; pointer-events: none; }
.mf-tl-form { flex: 1; overflow-y: auto; display: grid; grid-template-columns: auto 1fr; gap: 4px 8px; align-items: center; }
.mf-tl-form input, .mf-tl-form select, .mf-tl-form textarea { background: var(--comfy-input-bg, #222); color: inherit;
  border: 1px solid var(--border-color, #444); border-radius: 3px; padding: 2px 4px; font: inherit; }
.mf-tl-form textarea { resize: vertical; min-height: 38px; }
.mf-tl-form .full { grid-column: 1 / -1; }
.mf-tl-form .box { grid-column: 1 / -1; border: 1px solid var(--border-color, #444); border-radius: 4px; padding: 4px 6px; }
.mf-tl-form .warn { color: #ffb36b; } .mf-tl-form .bad { color: #ff7a7a; }
.mf-tl-chips { display: flex; flex-wrap: wrap; gap: 3px; }
.mf-tl-chips button { padding: 0 6px; font-size: 11px; }
.mf-tl-panel { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; padding: 4px; border: 1px dashed var(--border-color, #555); border-radius: 4px; }
.mf-tl-panel input { width: 64px; } .mf-tl-panel textarea { width: 100%; min-height: 48px; }
.mf-tl-status { min-height: 16px; font-size: 11px; }
.mf-tl-status.bad { color: #ff7a7a; } .mf-tl-status.good { color: #8fd18f; } .mf-tl-status.warn { color: #ffb36b; }
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
        this.draftData = null;
        this.cardDrag = null;
        this.raf = 0;
        this.notice = "";
        this.media = new Map();          // material id → {ref, rev, status, info, error}
        this.mediaRevision = 0;
        this.probeChain = Promise.resolve();
        this.pendingUploads = [];
        this.destroyed = false;
        this.view = { active: 0, zoom: null, playhead: 0, sel: null, ...(node.properties?.[VIEW_KEY] ?? {}) };
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
        const grid = this.w.grid?.value ?? H3_GRID;
        const mode = this.w.mode?.value ?? "generate";
        return { fpsKey, fps, grid, mode, hasSource, sourceFrames, h3: mode === "generate" && grid === H3_GRID };
    }

    hydrate() {
        // Malformed JSON stays as typed (never canonicalized and re-saved); the editor pauses
        // and shows the first failing location until the text is fixed. strictParse sees the
        // duplicate keys and 5.0-style numbers that JSON.parse would silently accept.
        try {
            const parsed = strictParse(this.w.timeline_data.value);
            this.brokenAt = schemaError(parsed);
            if (this.brokenAt === null) this.data = canonicalize(parsed);
        } catch {
            this.brokenAt = "timeline_data";
        }
        this.broken = this.brokenAt !== null;
        this.setJsonVisible(this.broken);
        if (!this.broken) this.syncMedia();
        this.render();
    }

    commit(next, notice = "") {
        // Every edit path ends here; a panel or form left over from before the JSON broke must not
        // overwrite the text the user is fixing.
        if (this.broken) return;
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
        this.syncMedia();
        this.render();
    }

    apply(result, okNotice = "") {
        if (!result.ok) {
            this.notice = S.editReasons[result.reason] ?? result.reason;
            return this.render();
        }
        const warn = result.warnings?.length ? result.warnings.join("、") : "";
        this.commit(result.next, okNotice || warn);
    }

    saveView() {
        this.node.properties = this.node.properties ?? {};
        this.node.properties[VIEW_KEY] = { ...this.view };
    }

    select(sel) {
        this.view.sel = sel;
        if (sel?.type === "segment") this.view.active = sel.index;
        this.saveView();
    }

    setJsonVisible(visible) {
        const w = this.w.timeline_data;
        w.hidden = !visible;
        w.computeSize = visible ? null : () => [0, -4];
        this.node.setDirtyCanvas?.(true, true);
    }

    // ---------- material snapshots (probe route; never persisted) ----------

    syncMedia() {
        const ids = new Set(this.data.materials.map((m) => m.id));
        for (const id of [...this.media.keys()]) if (!ids.has(id)) this.media.delete(id);
        for (const m of this.data.materials) {
            const entry = this.media.get(m.id);
            if (!entry || entry.ref !== m.ref || entry.kind !== m.kind) this.probeMaterial(m);
        }
    }

    probeMaterial(m) {
        const rev = ++this.mediaRevision;
        this.media.set(m.id, { ref: m.ref, kind: m.kind, rev, status: "probing", info: null, error: null });
        // one probe at a time: the route has its own small concurrency / queue limits
        this.probeChain = this.probeChain.then(() => {
            const current = this.media.get(m.id);
            if (this.destroyed || !current || current.rev !== rev) return null;
            return this.request("/mediaforge/timeline/probe", { version: 1, source: { type: "input", ...splitSource(m.ref) } })
                .then(({ ok, body }) => {
                    const entry = this.media.get(m.id);
                    if (this.destroyed || !entry || entry.rev !== rev) return;  // deleted / changed / undone meanwhile
                    if (ok && body.kind === m.kind) Object.assign(entry, { status: "ready", info: body });
                    else Object.assign(entry, { status: "error", error: ok ? S.editReasons.wrong_lane : body?.error?.message ?? S.previewError });
                    this.render();
                })
                .catch((e) => {
                    const entry = this.media.get(m.id);
                    if (e.name !== "AbortError" && entry?.rev === rev) {
                        Object.assign(entry, { status: "error", error: S.previewError });
                        this.render();
                    }
                });
        });
    }

    // Snapshot in the same shape Python's describe_material builds (frames at the current fps).
    snapshot(id) {
        const m = this.data.materials.find((x) => x.id === id);
        const entry = this.media.get(id);
        if (!m || entry?.status !== "ready") return null;
        const info = entry.info;
        const [num, den] = this.settings().fps;
        const frames = info.duration_us == null ? null
            : Number((BigInt(info.duration_us) * BigInt(num)) / (BigInt(den) * 1000000n));
        return { ...info, id: m.id, kind: m.kind, name: m.name, ref: m.ref, frames };
    }

    snapshots() {
        const out = {};
        for (const m of this.data.materials) {
            const snap = this.snapshot(m.id);
            if (!snap) return null;
            out[m.id] = snap;
        }
        return out;
    }

    // ---------- DOM ----------

    build() {
        this.fileInput = el("input", { type: "file", accept: "video/*", style: { display: "none" },
            onchange: () => this.fileInput.files[0] && this.upload(this.fileInput.files[0]) });
        this.materialInput = el("input", { type: "file", accept: "image/*,video/*,audio/*", multiple: "", style: { display: "none" },
            onchange: () => this.uploadMaterials([...this.materialInput.files]) });
        this.previewLabel = el("span", { class: "mf-tl-label" });
        this.video = el("video", { class: "mf-tl-video", muted: "", playsinline: "", preload: "metadata" });
        this.video.addEventListener("error", () => this.onVideoError());
        this.video.addEventListener("timeupdate", () => this.onVideoTime());
        this.lib = el("div", { class: "mf-tl-lib", title: S.library });
        this.frameLabel = el("span", { class: "mf-tl-label" });
        this.playButton = el("button", { text: S.play, onclick: () => this.togglePlay() });
        this.addButton = el("button", { text: S.add, onclick: () => this.addSegment() });
        this.deleteButton = el("button", { text: S.deleteLast, onclick: () => this.deleteLast() });
        this.fillButton = el("button", { text: S.autoFill, onclick: () => this.togglePanel("fill") });
        this.importButton = el("button", { text: S.importScenes, onclick: () => this.togglePanel("import") });
        this.materialButton = el("button", { text: S.addMaterials, onclick: () => this.materialInput.click() });
        this.splitButton = el("button", { text: S.split, onclick: () => this.splitSelected() });
        this.bar = el("div", { class: "mf-tl-bar" }, [
            el("button", { text: S.upload, onclick: () => this.fileInput.click() }), this.fileInput,
            this.materialButton, this.materialInput,
            this.playButton, this.frameLabel,
            el("button", { text: S.zoomOut, onclick: () => this.zoom(0.5) }),
            el("button", { text: S.zoomIn, onclick: () => this.zoom(2) }),
            el("button", { text: S.fit, onclick: () => this.zoom(null) }),
            this.addButton, this.deleteButton, this.fillButton, this.importButton, this.splitButton,
        ]);
        this.ruler = el("canvas", { class: "mf-tl-ruler", height: "18" });
        this.playhead = el("div", { class: "mf-tl-playhead" });
        this.track = el("div", { class: "mf-tl-track" }, [this.ruler, this.playhead]);
        this.scroller = el("div", { class: "mf-tl-scroller" }, [this.track]);
        this.track.addEventListener("pointerdown", (e) => this.onPointerDown(e));
        this.track.addEventListener("pointermove", (e) => this.onPointerMove(e));
        this.track.addEventListener("pointerup", (e) => this.onPointerUp(e));
        this.track.addEventListener("pointercancel", () => this.cancelDrag());
        this.track.addEventListener("lostpointercapture", () => (this.drag || this.barDrag) && this.cancelDrag());
        this.scroller.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
        this.panel = el("div");
        this.form = el("div", { class: "mf-tl-form" });
        this.status = el("div", { class: "mf-tl-status", role: "status" });
        this.root = el("div", { class: "mf-tl", tabindex: "0", onkeydown: (e) => this.onKey(e) }, [
            el("div", {}, [this.previewLabel]), el("div", { class: "mf-tl-top" }, [this.video, this.lib]),
            this.bar, this.scroller, this.panel, this.form, this.status,
        ]);
        // OS file drops go to the library; stopping them here keeps the canvas from treating
        // the file as a workflow to load.
        for (const type of ["dragenter", "dragover"]) {
            this.root.addEventListener(type, (e) => {
                if (![...(e.dataTransfer?.types ?? [])].includes("Files")) return;
                e.preventDefault();
                e.stopPropagation();
                this.lib.classList.add("drop");
            });
        }
        this.root.addEventListener("dragleave", () => this.lib.classList.remove("drop"));
        this.root.addEventListener("drop", (e) => {
            if (!e.dataTransfer?.files?.length) return;
            e.preventDefault();
            e.stopPropagation();
            this.lib.classList.remove("drop");
            this.uploadMaterials([...e.dataTransfer.files]);
        });
        this.segEls = [];
        this.itemEls = new Map();
        this.laneEls = [];
    }

    render() {
        if (this.broken) {
            this.status.className = "mf-tl-status bad";
            this.status.textContent = `${S.jsonBroken}（${this.brokenAt}）`;
            this.form.replaceChildren();
            this.panel.replaceChildren();
            this.panelKind = null;
            this.drag = this.draft = this.barDrag = this.draftData = null;
            for (const b of [this.addButton, this.deleteButton, this.fillButton, this.importButton, this.materialButton, this.splitButton]) {
                b.disabled = true;
            }
            return;
        }
        const st = this.settings();
        this.result = validate(this.data, st.mode, st.fpsKey, st.grid, st.sourceFrames, st.hasSource);
        this.addButton.disabled = this.fillButton.disabled = this.importButton.disabled = false;
        this.materialButton.disabled = st.mode !== "generate";
        this.deleteButton.disabled = this.data.segments.length <= 1;
        this.splitButton.disabled = !["ref", "anchor"].includes(this.view.sel?.type);
        this.fillButton.style.display = st.mode === "generate" ? "" : "none";
        this.importButton.style.display = st.mode === "cut" ? "" : "none";
        this.renderLibrary(st);
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

    renderLibrary(st) {
        const cards = this.data.materials.map((m) => {
            const entry = this.media.get(m.id);
            const status = entry?.status ?? "probing";
            const thumb = m.kind === "image"
                ? el("img", { src: viewURL({ ...splitSource(m.ref), type: "input" }), alt: "" })
                : el("div", { class: "icon", text: KIND_ICON[m.kind] });
            const card = el("div", {
                class: `mf-tl-card${this.view.sel?.type === "material" && this.view.sel.id === m.id ? " selected" : ""}`,
                tabindex: "0", title: `${S.kinds[m.kind]}：${m.ref}`,
                onclick: () => {
                    this.select({ type: "material", id: m.id });
                    this.render();
                },
            }, [thumb, el("span", { class: "name", text: m.name }),
                el("span", { class: `state${status === "error" ? " error" : ""}`, text: entry?.error ?? S.status[status] })]);
            card.addEventListener("pointerdown", (e) => this.startCardDrag(e, m, card));
            return card;
        });
        const pending = this.pendingUploads.map((p) => el("div", { class: "mf-tl-card pending" }, [
            el("div", { class: "icon", text: KIND_ICON[p.kind] ?? "?" }), el("span", { class: "name", text: p.file.name }),
            el("span", { class: "state", text: S.status.uploading }),
        ]));
        const empty = st.mode === "generate" && !cards.length && !pending.length
            ? [el("span", { class: "mf-tl-label", text: S.emptyLibrary })] : [];
        this.lib.replaceChildren(...cards, ...pending, ...empty);
        this.lib.style.display = st.mode === "generate" ? "" : "none";
    }

    layout(data, st) {
        const lanes = [];
        if (st.mode !== "generate") return { lanes, height: LANE_TOP };
        const refRows = packRows(data.refs, (r) => barSpan("ref", r));
        let top = LANE_TOP;
        for (let r = 0; r < refRows.rows; r++) lanes.push({ lane: "refs", row: r, top: top + r * LANE_ROW, label: r ? "" : S.laneRefs });
        top += refRows.rows * LANE_ROW;
        lanes.push({ lane: "visual", row: 0, top, label: S.laneVisual });
        lanes.push({ lane: "audio", row: 0, top: top + LANE_ROW, label: S.laneAudio });
        return { lanes, refRows, height: top + 2 * LANE_ROW + 6 };
    }

    renderTrack() {
        if (this.broken || !this.data) return;
        const st = this.settings();
        const data = this.draftData ?? this.data;
        const segs = this.draft ?? data.segments;
        const extent = Math.max(...segs.map((s) => s.end), st.sourceFrames ?? 0, 1);
        this.px = this.pxPerFrame(extent);
        const width = Math.ceil(extent * this.px) + 2;
        const geometry = this.layout(data, st);
        this.lanes = geometry.lanes;
        this.track.style.width = `${width}px`;
        this.track.style.height = `${Math.max(geometry.height, 96)}px`;
        this.drawRuler(width, extent, st.fps);
        while (this.segEls.length > segs.length) this.segEls.pop().remove();
        while (this.segEls.length < segs.length) {
            const handles = ["left", "right"].map((edge) => el("div", { class: "mf-tl-handle", "data-edge": edge }));
            const label = el("span");
            const overlap = el("div", { class: "overlap" });
            const seg = el("div", { class: "mf-tl-seg" }, [overlap, label, ...handles]);
            seg.label = label;
            seg.overlap = overlap;
            this.track.insertBefore(seg, this.playhead);
            this.segEls.push(seg);
        }
        const bad = this.result && !this.result.ok ? segmentIndexOf(this.result.error.location) : null;
        segs.forEach((s, i) => {
            const e = this.segEls[i];
            e.dataset.index = String(i);
            e.label.textContent = `GEN${i + 1} · ${s.end - s.start}f${s.seed != null ? " · 🎲" : ""}`;
            // incoming overlap is hatched: those frames belong to the previous segment's output
            const incoming = st.mode === "generate" && i ? Math.max(0, segs[i - 1].end - s.start) : 0;
            e.overlap.style.width = `${incoming * this.px}px`;
            Object.assign(e.style, {
                left: `${s.start * this.px}px`, width: `${Math.max(6, (s.end - s.start) * this.px)}px`,
                top: `${22 + (st.mode === "generate" ? (i % 2) * 36 : 20)}px`, background: SEGMENT_COLORS[i % SEGMENT_COLORS.length],
            });
            e.classList.toggle("active", i === this.view.active && this.view.sel?.type !== "ref" && this.view.sel?.type !== "anchor");
            e.classList.toggle("invalid", i === bad);
        });
        this.renderLanes(geometry, width);
        this.renderItems(data, geometry, st);
        this.playhead.style.left = `${this.view.playhead * this.px}px`;
        const seconds = (this.view.playhead * st.fps[1]) / st.fps[0];
        this.frameLabel.textContent = `${this.view.playhead}f · ${seconds.toFixed(2)}s`;
    }

    renderLanes(geometry, width) {
        while (this.laneEls.length > geometry.lanes.length) this.laneEls.pop().remove();
        while (this.laneEls.length < geometry.lanes.length) {
            const lane = el("div", { class: "mf-tl-lane" }, [el("span")]);
            this.track.insertBefore(lane, this.track.firstChild.nextSibling);
            this.laneEls.push(lane);
        }
        geometry.lanes.forEach((lane, i) => {
            const e = this.laneEls[i];
            e.style.top = `${lane.top}px`;
            e.style.width = `${width}px`;
            e.firstChild.textContent = lane.label;
            e.classList.toggle("drop", this.dropLane === lane);
        });
    }

    renderItems(data, geometry, st) {
        const seen = new Set();
        const names = Object.fromEntries(data.materials.map((m) => [m.id, m]));
        const place = (kind, item, top) => {
            const key = `${kind}:${item.id}`;
            seen.add(key);
            let e = this.itemEls.get(key);
            if (!e) {
                e = el("div", { class: "mf-tl-item" }, [el("span")]);
                e.dataset.kind = kind;
                e.dataset.id = item.id;
                this.track.insertBefore(e, this.playhead);
                this.itemEls.set(key, e);
            }
            const m = names[item.material];
            const keyframe = item.role === "keyframe";
            e.classList.toggle("keyframe", keyframe);
            e.classList.toggle("selected", this.view.sel?.type === kind && this.view.sel.id === item.id);
            if (!keyframe && e.querySelectorAll(".mf-tl-handle").length === 0) {
                e.append(el("div", { class: "mf-tl-handle", "data-edge": "left" }), el("div", { class: "mf-tl-handle", "data-edge": "right" }));
            } else if (keyframe) {
                e.querySelectorAll(".mf-tl-handle").forEach((h) => h.remove());
            }
            const [lo, hi] = barSpan(kind, item);
            e.firstChild.textContent = keyframe ? "" : `${m?.name ?? "?"}${kind === "ref" ? "" : " · " + S.roles[item.role]}`;
            e.title = `${m?.name ?? "?"} · ${kind === "ref" ? S.roles.ref : S.roles[item.role]}`;
            Object.assign(e.style, {
                left: `${lo * this.px}px`, width: `${Math.max(6, (hi - lo) * this.px)}px`, top: `${top + 1}px`,
                background: KIND_COLORS[m?.kind] ?? "#666",
            });
        };
        if (st.mode === "generate") {
            const laneTop = (lane, row = 0) => geometry.lanes.find((l) => l.lane === lane && l.row === row)?.top ?? LANE_TOP;
            for (const r of data.refs) place("ref", r, laneTop("refs", geometry.refRows.rowOf.get(r.id)));
            for (const a of data.anchors) place("anchor", a, laneTop(a.role === "lock" ? "audio" : "visual"));
        }
        for (const [key, e] of this.itemEls) {
            if (!seen.has(key)) {
                e.remove();
                this.itemEls.delete(key);
            }
        }
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

    // ---------- inspector ----------

    renderForm(st) {
        const sel = this.view.sel;
        const rows = [];
        if (st.mode === "generate") {
            rows.push(el("span", { class: "full mf-tl-label", text: S.globalPrompt }),
                el("textarea", { class: "full", onchange: (e) => this.commit({ ...this.data, prompt: e.target.value }) }, [this.data.prompt]));
        }
        if (sel?.type === "ref" && this.data.refs.some((r) => r.id === sel.id)) rows.push(...this.refForm(st, sel.id));
        else if (sel?.type === "anchor" && this.data.anchors.some((a) => a.id === sel.id)) rows.push(...this.anchorForm(st, sel.id));
        else if (sel?.type === "material" && this.data.materials.some((m) => m.id === sel.id)) rows.push(...this.materialForm(st, sel.id));
        else rows.push(...this.segmentForm(st));
        this.form.replaceChildren(...rows);
    }

    number(value, onchange, attrs = {}) {
        return el("input", { type: "number", step: "1", value: String(value), onchange: (e) => onchange(Number(e.target.value)), ...attrs });
    }

    segmentForm(st) {
        const segs = this.data.segments;
        const i = Math.min(this.view.active, segs.length - 1);
        const s = segs[i];
        const prev = segs[i - 1];
        const overlap = st.mode === "generate" && prev ? prev.end - s.start : 0;
        const sec = (f) => (f * st.fps[1]) / st.fps[0];
        const rows = [
            el("b", { text: S.segment(i + 1) }),
            el("span", { class: "mf-tl-label", text: `${S.seconds(sec(s.start))} → ${S.seconds(sec(s.end))}${overlap ? " · " + S.overlapIn(overlap) : ""}` }),
            el("span", { text: S.start }), this.number(s.start, (v) => this.editEdge(i, "left", v)),
            el("span", { text: S.end }), this.number(s.end, (v) => this.editEdge(i, "right", v)),
            el("span", { text: S.prompt }),
            el("textarea", { placeholder: st.mode === "generate" ? S.inheritGlobal : "",
                onchange: (e) => this.commit(this.withSegment(i, { prompt: e.target.value })) }, [s.prompt]),
        ];
        if (st.mode === "generate" && this.data.materials.length) {
            rows.push(el("span"), el("div", { class: "mf-tl-chips" }, this.data.materials.map((m) => el("button", {
                text: `<@${m.name}>`, title: S.insertAlias,
                onclick: () => this.commit(this.withSegment(i, { prompt: `${s.prompt}${s.prompt && !s.prompt.endsWith(" ") ? " " : ""}<@${m.name}>` })),
            }))));
        }
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
                    this.number(s.seam_in.frames, (v) => this.commit(this.withSegment(i, { seam_in: { ...s.seam_in, frames: v } })), { min: "1", max: String(limit) }),
                    el("span", { text: S.audioCurve }), curve);
            }
            if (st.grid === H3_GRID) {
                const ms = phaseMs(carrySpec(H3_GRID, prev.end - prev.start, overlap));
                rows.push(el("span"), el("span", { class: "mf-tl-label", text: ms ? S.phase(ms) : S.phaseZero }));
            }
        }
        if (st.mode === "generate") {
            const seed = el("input", { type: "number", min: "0", step: "1", placeholder: S.seedAuto, value: s.seed ?? "",
                onchange: (e) => this.commit(this.withSegment(i, { seed: e.target.value === "" ? null : Math.max(0, Math.floor(Number(e.target.value))) })) });
            rows.push(el("span", { text: S.seed }), el("div", { class: "mf-tl-chips" }, [seed,
                el("button", { text: S.reroll, onclick: () => this.commit(this.withSegment(i, { seed: Math.floor(Math.random() * 2 ** 31) })) }),
                el("span", { class: "mf-tl-label", text: S.seedNote })]));
            if (i > 0) {
                const cut = overlap > 0;
                rows.push(el("span"), el("button", {
                    text: cut ? S.shotCut : S.shotContinue,
                    onclick: () => this.apply(setShotBoundary(this.data, i, cut, Math.min(DEFAULT_OVERLAP[st.grid] ?? 0, s.end - s.start - 1, prev.end - prev.start - 1))),
                }));
            }
            rows.push(...this.modelInputs(st, i));
        }
        return rows;
    }

    // What actually reaches the model for this segment — same rules as the Python compiler,
    // shown only once every material has a verified snapshot.
    modelInputs(st, i) {
        const box = el("div", { class: "box" }, [el("b", { text: S.modelInputs })]);
        const snaps = this.snapshots();
        if (!this.data.materials.length) return [];
        if (!snaps) {
            box.append(el("div", { class: "warn", text: S.unverified }));
            return [box];
        }
        const segs = this.data.segments;
        const prev = segs[i - 1];
        const carryIn = prev && st.mode === "generate" ? carrySpec(st.grid, prev.end - prev.start, prev.end - segs[i].start) : null;
        const geometry = validateGeometry(this.data.refs, this.data.anchors, snaps, segs, st.h3);
        if (geometry) {
            box.append(el("div", { class: "bad", text: this.errorText(geometry) }));
            return [box];
        }
        const r = segmentManifest(segs, i, this.data.refs, this.data.anchors, snaps, st.h3, carryIn);
        if (!r.ok) {
            box.append(el("div", { class: "bad", text: this.errorText(r.error) }));
            return [box];
        }
        const m = r.manifest;
        for (const p of m.pictures) box.append(el("div", { text: S.tagTable(p.tag ?? "picture", p.name) }));
        for (const v of m.videos) {
            box.append(el("div", { text: `${S.tagTable(v.tag ?? "video", v.name)} · ${S.refFrames(v.effective_frames, v.requested_frames)}` }));
            if (v.audio_tag) box.append(el("div", { text: S.tagTable(v.audio_tag, `${v.name}:audio`) }));
        }
        for (const a of m.audios) box.append(el("div", { text: S.tagTable(a.tag ?? "audio", a.name) }));
        for (const k of m.keyframes) box.append(el("div", { text: `${S.roles.keyframe} ${k.name} @ ${k.frame_idx}` }));
        for (const g of m.guides) box.append(el("div", { text: `${S.roles.guide} ${g.name} @ ${g.frame_idx} · ${g.frames}f` }));
        for (const l of m.locks) {
            box.append(el("div", { text: `${S.roles.lock} ${l.name} @ ${l.frame_idx} · ${l.frames}f` + (l.tokens ? ` · ${S.lockCoverage(l.tokens[0], l.tokens[1])}` : "") }));
        }
        if (m.carry_conflicts?.length) box.append(el("div", { class: "warn", text: S.lockCarry }));
        const template = segs[i].prompt || this.data.prompt;
        const prompt = resolvePrompt(template, m, this.data.materials, i, st.h3);
        box.append(el("div", { class: "mf-tl-label", text: S.resolvedPrompt }),
            el("div", { class: prompt.ok ? "" : "bad", text: prompt.ok ? prompt.prompt : this.errorText(prompt.error) }));
        if (/<(Picture|Video|Audio) \d+>/.test(template)) box.append(el("div", { class: "warn", text: S.rawTagHint }));
        return [box];
    }

    errorText(error) {
        const fields = Object.entries(error).filter(([k]) => k !== "code").map(([k, v]) => `${k}=${v}`).join("，");
        return `${error.code}${fields ? "：" + fields : ""}`;
    }

    refForm(st, id) {
        const r = this.data.refs.find((x) => x.id === id);
        const m = this.data.materials.find((x) => x.id === r.material);
        const patch = (p) => this.commit({ ...this.data, refs: this.data.refs.map((x) => (x.id === id ? { ...x, ...p } : x)) });
        const rows = [
            el("b", { text: `${S.roles.ref}：${m?.name ?? "?"}` }), el("span", { class: "mf-tl-label", text: S.kinds[m?.kind] ?? "" }),
            el("span", { text: S.from }), this.number(r.from, (v) => patch({ from: v })),
            el("span", { text: S.to }), this.number(r.to, (v) => patch({ to: v })),
        ];
        if (m?.kind === "video") {
            const mode = el("select", { onchange: (e) => patch(e.target.value === "fixed"
                ? { mode: "fixed", length: Math.max(1, r.to - r.from) } : { mode: "follow", length: undefined }) },
            [el("option", { value: "follow", text: S.modeFollow }), el("option", { value: "fixed", text: S.modeFixed })]);
            mode.value = r.mode;
            rows.push(el("span", { text: S.mode }), mode);
            const audio = el("input", { type: "checkbox", onchange: (e) => patch({ audio: e.target.checked }) });
            audio.checked = !!r.audio;
            rows.push(el("span", { text: S.withAudio }), audio);
        }
        if (r.in !== undefined) rows.push(el("span", { text: S.contentIn }), this.number(r.in, (v) => patch({ in: v })));
        if (r.length !== undefined) rows.push(el("span", { text: S.contentLength }), this.number(r.length, (v) => patch({ length: v })));
        rows.push(el("span"), el("div", { class: "mf-tl-chips" }, [
            el("button", { text: S.moveUp, onclick: () => this.apply(moveRefInOrder(this.data, id, -1)) }),
            el("button", { text: S.moveDown, onclick: () => this.apply(moveRefInOrder(this.data, id, 1)) }),
            el("button", { text: S.split, onclick: () => this.splitSelected() }),
            el("button", { text: S.remove, onclick: () => this.removeSelected() }),
        ]));
        return rows;
    }

    anchorForm(st, id) {
        const a = this.data.anchors.find((x) => x.id === id);
        const m = this.data.materials.find((x) => x.id === a.material);
        const patch = (p) => this.commit({ ...this.data, anchors: this.data.anchors.map((x) => (x.id === id ? { ...x, ...p } : x)) });
        const rows = [
            el("b", { text: `${S.roles[a.role]}：${m?.name ?? "?"}` }), el("span"),
            el("span", { text: S.at }), this.number(a.at, (v) => patch({ at: v })),
        ];
        if (a.role !== "keyframe") {
            rows.push(el("span", { text: S.contentIn }), this.number(a.in, (v) => patch({ in: v })),
                el("span", { text: S.contentLength }), this.number(a.length, (v) => patch({ length: v })));
        }
        if (a.role === "guide") {
            const audio = el("input", { type: "checkbox", onchange: (e) => patch({ audio: e.target.checked }) });
            audio.checked = !!a.audio;
            rows.push(el("span", { text: S.withAudio }), audio);
        }
        rows.push(el("span"), el("div", { class: "mf-tl-chips" }, [
            el("button", { text: S.split, onclick: () => this.splitSelected() }),
            el("button", { text: S.remove, onclick: () => this.removeSelected() }),
        ]));
        return rows;
    }

    materialForm(st, id) {
        const m = this.data.materials.find((x) => x.id === id);
        const entry = this.media.get(id);
        const info = entry?.info;
        const describe = !info ? (entry?.error ?? S.status[entry?.status ?? "probing"])
            : m.kind === "image" ? `${info.width}×${info.height}`
                : `${((info.duration_us ?? 0) / 1e6).toFixed(2)} 秒${m.kind === "video" ? ` · ${info.display_size?.join("×")}${info.has_audio ? " · 有音軌" : ""}` : ` · ${info.audio_sample_rate} Hz`}`;
        const segs = this.data.segments;
        const i = Math.min(this.view.active, segs.length - 1);
        return [
            el("b", { text: `${KIND_ICON[m.kind]} ${S.kinds[m.kind]}` }), el("span", { class: "mf-tl-label", text: describe }),
            el("span", { text: S.rename }),
            el("input", { type: "text", value: m.name, onchange: (e) => this.apply(renameMaterial(this.data, id, e.target.value.normalize("NFC"), nameOk)) }),
            el("span", { text: S.materialRef }),
            el("input", { type: "text", value: m.ref, onchange: (e) => {
                // a bad path would make the saved JSON fail strict parsing on the next load
                if (!refOk(e.target.value)) {
                    this.notice = S.badRef;
                    return this.render();
                }
                this.commit({ ...this.data, materials: this.data.materials.map((x) => (x.id === id ? { ...x, ref: e.target.value } : x)) });
            } }),
            el("span"), el("div", { class: "mf-tl-chips" }, [
                el("button", { text: S.insertAlias, onclick: () => {
                    const p = segs[i].prompt;
                    this.commit(this.withSegment(i, { prompt: `${p}${p && !p.endsWith(" ") ? " " : ""}<@${m.name}>` }));
                } }),
                el("button", { text: S.remove, onclick: () => {
                    this.apply(deleteMaterial(this.data, id));
                    this.select(null);
                } }),
            ]),
        ];
    }

    renderStatus(st) {
        const r = this.result;
        const unverified = st.mode === "generate" && this.data.materials.length && !this.snapshots();
        let geometry = null;
        if (r.ok && st.mode === "generate" && this.data.materials.length && !unverified) {
            geometry = validateGeometry(this.data.refs, this.data.anchors, this.snapshots(), this.data.segments, st.h3);
        }
        if (!r.ok) {
            this.status.className = "mf-tl-status bad";
            this.status.textContent = S.invalid(r.error.location, S.reasons[r.error.reason] ?? r.error.reason);
        } else if (geometry) {
            this.status.className = "mf-tl-status bad";
            this.status.textContent = `${S.invalid("materials", this.errorText(geometry))}`;
        } else {
            this.status.className = `mf-tl-status ${unverified ? "warn" : "good"}`;
            this.status.textContent = `${unverified ? S.unverified : S.valid} · ${S.total(r.totalFrames, (r.totalFrames * st.fps[1]) / st.fps[0])}`;
        }
        if (this.notice) this.status.textContent += ` · ${this.notice}`;
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
            next = { start: last.end, end, prompt: "", seam_in: { kind: "cut" }, seed: null };
        } else {
            const prev = segs[segs.length - 2];
            const overlap = Math.min(prev ? prev.end - last.start : DEFAULT_OVERLAP[st.grid] ?? 0, length - 1);
            next = { start: last.end - overlap, end: last.end - overlap + length, prompt: "", seam_in: { kind: "cut" }, seed: null };
        }
        this.select({ type: "segment", index: segs.length });
        this.commit({ ...this.data, segments: [...segs, next] });
    }

    deleteLast() {
        const segs = this.data.segments;
        if (segs.length <= 1) return;
        this.select({ type: "segment", index: Math.min(this.view.active, segs.length - 2) });
        this.commit({ ...this.data, segments: segs.slice(0, -1) });
    }

    splitSelected() {
        const sel = this.view.sel;
        if (sel?.type !== "ref" && sel?.type !== "anchor") return;
        this.apply(splitBar(this.data, sel.type, sel.id, this.view.playhead, this.settings().h3));
    }

    removeSelected() {
        const sel = this.view.sel;
        if (sel?.type !== "ref" && sel?.type !== "anchor") return;
        this.apply(deleteBar(this.data, sel.type, sel.id));
        this.select(null);
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
                    this.select({ type: "segment", index: 0 });
                    this.commit({ ...this.data, segments: r.segments.map((s) => ({ ...s, seed: null })) });
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
                    this.select({ type: "segment", index: 0 });
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
            if (end > start) segs.push({ start, end, prompt: "", seam_in: { kind: "cut" }, seed: null });
        }
        return segs.length && segs.length <= 256 ? segs : null;
    }

    zoom(factor) {
        this.view.zoom = factor === null ? null : Math.min(64, Math.max(1, (this.view.zoom ?? 1) * factor));
        this.saveView();
        this.renderTrack();
    }

    // ---------- uploads ----------

    async uploadFile(file) {
        const form = new FormData();
        form.append("image", file);
        form.append("type", "input");
        form.append("subfolder", UPLOAD_SUBFOLDER);
        form.append("overwrite", "false");
        const res = await api.fetchApi("/upload/image", { method: "POST", body: form });
        if (!res.ok) throw new Error(String(res.status));
        const { name, subfolder } = await res.json();
        return subfolder ? `${subfolder}/${name}` : name;  // the server may have renamed the file
    }

    async upload(file) {
        this.previewLabel.textContent = S.uploading;
        try {
            const value = await this.uploadFile(file);
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

    // A batch of materials (picker or OS drop) uploads two at a time and lands as one commit /
    // one undo, merged into whatever the timeline looks like when the batch finishes.
    async uploadMaterials(files) {
        this.materialInput.value = "";
        if (this.broken || this.settings().mode !== "generate") return;
        const batch = files.map((file) => ({ file, kind: kindOf(file) }));
        const unsupported = batch.filter((p) => !p.kind).map((p) => p.file.name);
        const queue = batch.filter((p) => p.kind);
        this.pendingUploads.push(...queue);
        this.render();
        const done = [];
        const failed = [...unsupported];
        const worker = async () => {
            while (queue.length) {
                const p = queue.shift();
                try {
                    done.push({ order: batch.indexOf(p), kind: p.kind, ref: await this.uploadFile(p.file), filename: p.file.name });
                } catch {
                    failed.push(p.file.name);
                }
            }
        };
        await Promise.all(Array.from({ length: UPLOAD_CONCURRENCY }, worker));
        this.pendingUploads = this.pendingUploads.filter((p) => !batch.includes(p));
        if (this.destroyed || this.broken) return;
        done.sort((a, b) => a.order - b.order);
        if (done.length) this.commit(addMaterials(this.data, done).next);
        this.notice = failed.length ? S.uploadFailedNames(failed) : "";
        this.render();
    }

    // ---------- pointer / keyboard ----------

    trackScale() {
        const rect = this.track.getBoundingClientRect();
        return { rect, scale: rect.width / (this.track.offsetWidth || rect.width || 1) };  // graph zoom is already in rect
    }

    frameAt(clientX) {
        const { rect, scale } = this.trackScale();
        return Math.max(0, Math.round((clientX - rect.left) / (this.px * scale)));
    }

    laneAt(clientX, clientY) {
        const box = this.scroller.getBoundingClientRect();
        if (clientX < box.left || clientX > box.right || clientY < box.top || clientY > box.bottom) return null;
        const { rect, scale } = this.trackScale();
        const y = (clientY - rect.top) / scale;
        return (this.lanes ?? []).find((l) => y >= l.top && y < l.top + LANE_ROW) ?? null;
    }

    // Snap an edge to segment output boundaries, the playhead or other bars within SNAP_PX
    // screen pixels; nearest candidate wins, boundaries first on ties.
    snap(frame, skipKey) {
        const data = this.data;
        const candidates = [...emitIntervals(data.segments).map(([lo]) => lo), data.segments.at(-1).end, this.view.playhead];
        for (const r of data.refs) if (`ref:${r.id}` !== skipKey) candidates.push(r.from, r.to);
        for (const a of data.anchors) if (`anchor:${a.id}` !== skipKey) candidates.push(...barSpan("anchor", a));
        const { scale } = this.trackScale();
        let best = frame;
        let bestPx = SNAP_PX + 1;
        for (const c of candidates) {
            const px = Math.abs(c - frame) * this.px * scale;
            if (px < bestPx) [best, bestPx] = [c, px];
        }
        return best;
    }

    startCardDrag(e, m, card) {
        if (this.broken || e.button !== 0) return;
        e.stopPropagation();
        const ghost = el("div", { class: "mf-tl-ghost", text: `${KIND_ICON[m.kind]} ${m.name}` });
        document.body.append(ghost);
        this.cardDrag = { id: m.id, pointerId: e.pointerId, ghost, card, startX: e.clientX, startY: e.clientY, moved: false };
        try {
            card.setPointerCapture(e.pointerId);
        } catch {
            // already released (very fast click); the click handler still selects the card
        }
        const move = (ev) => {
            if (!this.cardDrag || ev.pointerId !== this.cardDrag.pointerId) return;
            this.cardDrag.moved ||= Math.hypot(ev.clientX - this.cardDrag.startX, ev.clientY - this.cardDrag.startY) > 4;
            Object.assign(ghost.style, { left: `${ev.clientX}px`, top: `${ev.clientY}px` });
            const lane = this.laneAt(ev.clientX, ev.clientY);
            if (lane !== this.dropLane) {
                this.dropLane = lane;
                this.renderTrack();
            }
        };
        const end = (ev, drop) => {
            if (!this.cardDrag || ev.pointerId !== this.cardDrag.pointerId) return;
            card.removeEventListener("pointermove", move);
            card.removeEventListener("pointerup", up);
            card.removeEventListener("pointercancel", cancel);
            ghost.remove();
            const lane = drop && this.cardDrag.moved ? this.laneAt(ev.clientX, ev.clientY) : null;
            this.cardDrag = null;
            this.dropLane = null;
            if (!lane) return this.renderTrack();
            const st = this.settings();
            this.apply(createFromCard(this.data, m.id, lane.lane, this.frameAt(ev.clientX), this.snapshot(m.id), st.h3));
        };
        const up = (ev) => end(ev, true);
        const cancel = (ev) => end(ev, false);
        card.addEventListener("pointermove", move);
        card.addEventListener("pointerup", up);
        card.addEventListener("pointercancel", cancel);
    }

    onPointerDown(e) {
        if (this.broken || e.button !== 0) return;
        e.stopPropagation();
        this.root.focus({ preventScroll: true });
        const item = e.target.closest(".mf-tl-item");
        const segEl = e.target.closest(".mf-tl-seg");
        const handle = e.target.closest(".mf-tl-handle");
        if (item) {
            const kind = item.dataset.kind;
            const id = item.dataset.id;
            this.select({ type: kind, id });
            const source = (kind === "ref" ? this.data.refs : this.data.anchors).find((x) => x.id === id);
            const op = handle ? handle.dataset.edge : "move";
            const [lo, hi] = barSpan(kind, source);
            this.barDrag = { kind, id, op, pointerId: e.pointerId, origin: this.frameAt(e.clientX), edge: op === "right" ? hi : lo };
            e.preventDefault();
        } else if (segEl) {
            this.select({ type: "segment", index: Number(segEl.dataset.index) });
            if (handle) {
                e.preventDefault();
                this.drag = { index: Number(segEl.dataset.index), edge: handle.dataset.edge, pointerId: e.pointerId, base: this.data.segments };
            }
        } else {
            this.seek(this.frameAt(e.clientX));
        }
        if (this.drag || this.barDrag) {
            try {
                this.track.setPointerCapture(e.pointerId);
            } catch {
                // the pointer may already be gone (very fast click); the drag still works without capture
            }
        }
        this.render();
    }

    onPointerMove(e) {
        if (this.drag && e.pointerId === this.drag.pointerId) {
            const r = this.applyEdit(this.drag.base, this.drag.index, this.drag.edge, this.frameAt(e.clientX));
            if (r.ok) {
                this.draft = r.segments;
                this.scheduleTrack();
            }
            return;
        }
        const drag = this.barDrag;
        if (!drag || e.pointerId !== drag.pointerId) return;
        const target = this.snap(drag.edge + this.frameAt(e.clientX) - drag.origin, `${drag.kind}:${drag.id}`);
        const source = (drag.kind === "ref" ? this.data.refs : this.data.anchors).find((x) => x.id === drag.id);
        const r = editBar(this.data, drag.kind, drag.id, drag.op, target - drag.edge, this.snapshot(source.material), this.settings().h3);
        drag.reason = r.ok ? null : r.reason;
        if (r.ok) {
            this.draftData = r.next;
            this.scheduleTrack();
        }
    }

    onPointerUp(e) {
        if (this.drag && e.pointerId === this.drag.pointerId) {
            const draft = this.draft;
            this.drag = null;
            this.draft = null;
            this.releaseCapture(e.pointerId);
            if (draft) this.commit({ ...this.data, segments: draft });
            else this.render();
            return;
        }
        const drag = this.barDrag;
        if (!drag || e.pointerId !== drag.pointerId) return;
        const next = this.draftData;
        this.barDrag = null;
        this.draftData = null;
        this.releaseCapture(e.pointerId);
        if (next) this.commit(next);
        else {
            if (drag.reason) this.notice = S.editReasons[drag.reason] ?? drag.reason;
            this.render();
        }
    }

    releaseCapture(pointerId) {
        if (this.track.hasPointerCapture?.(pointerId)) this.track.releasePointerCapture(pointerId);
    }

    cancelDrag() {
        this.drag = null;
        this.draft = null;
        this.barDrag = null;
        this.draftData = null;
        this.renderTrack();
    }

    onKey(e) {
        // never steal keys from text fields or an IME composition
        if (e.isComposing || e.target.closest("input, textarea, select, [contenteditable]")) return;
        const sel = this.view.sel;
        const bar = sel?.type === "ref" || sel?.type === "anchor";
        let handled = true;
        if (e.key === " ") this.togglePlay();
        else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && e.altKey && bar) {
            const source = (sel.type === "ref" ? this.data.refs : this.data.anchors).find((x) => x.id === sel.id);
            const step = (e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 10 : 1);
            if (source) this.apply(editBar(this.data, sel.type, sel.id, "move", step, this.snapshot(source.material), this.settings().h3));
        } else if (e.key === "ArrowLeft") this.seek(this.view.playhead - (e.shiftKey ? 10 : 1));
        else if (e.key === "ArrowRight") this.seek(this.view.playhead + (e.shiftKey ? 10 : 1));
        else if ((e.key === "s" || e.key === "S") && bar) this.splitSelected();
        else if ((e.key === "Delete" || e.key === "Backspace") && bar) this.removeSelected();
        else if (e.key === "Escape" && (this.drag || this.barDrag)) this.cancelDrag();
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
        if (this.video.paused || this.drag || this.barDrag) return;
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
                this.source = ok && body.kind === "video" ? body : null;
                this.sourceError = ok ? (body.kind === "video" ? null : S.previewError) : body?.error?.message ?? S.previewError;
                this.render();
                // the <video> may have failed before the probe answered; the proxy needs its fingerprint
                if (this.source && this.proxyWanted) this.makeProxy();
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

    destroy() {
        this.destroyed = true;
        for (const ctrl of this.requests) ctrl.abort();
        cancelAnimationFrame(this.raf);
        this.resizeObserver?.disconnect();
        this.cardDrag?.ghost.remove();
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
                this.mfTimeline.view = { active: 0, zoom: null, playhead: 0, sel: null, ...(this.properties?.[VIEW_KEY] ?? {}) };
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
