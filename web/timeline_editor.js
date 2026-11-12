// MediaForge timeline editor — DOM widget for MF_EditTimeline.
//
// timeline_data (a plain STRING widget) stays the only persisted carrier: the editor
// reads and writes it, Python re-validates it on every run, and when the editor
// cannot start the STRING widget is simply left visible as a JSON text box.
// View state (zoom / selection / playhead / open sections) lives only in the editor and is handed to the editor
// that replaces it in a graph reload (undo / redo); it is never written into the workflow, so selecting or seeking
// is not an edit (no undo step, no "modified" mark) and never reaches the prompt.
//
// Edits follow one path: drags only update a draft (repainted on rAF) and commit on
// pointerup inside one beforeChange/afterChange pair, so one drag is one undo step.
// Every commit passes timeline_edit's gate (no new structure / segment / geometry problem).
// Material snapshots (length, audio, size) come from the probe route and live only in
// this editor; until a material is verified the panels say so instead of guessing.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
    FPS_PRESETS, H3_GRID, canonicalize, carrySpec, commonLengths, commonOverlaps, crossfadeLimit, editCut, editGenerate,
    h3AudioTokens, lengthOk, overlapAtMost, overlapOk, phaseMs, schemaError, stepLength, stepOverlap, validate,
} from "./timeline_grid.js";
import { MAX_MATERIALS, refOk, strictParse } from "./timeline_materials.js";
import {
    addMaterials, aliasRawTags, checkEdit, conformSegments, createFromCard, deleteBar, deleteCut, deleteMaterial, editBar,
    emitIntervals, inputImpact, insertCut, markCut, moveRefInOrder, patchBar, pickSnap, probeIssue, refChanges, refillSegments,
    removeAllMaterials, renameMaterial, segmentInputs, setShotBoundary, snapTargets, splitBar, tagButtons, timelineProblems,
    useRefTag,
} from "./timeline_edit.js";
import { S } from "./timeline_strings.js";

const NODE_TYPE = "MF_EditTimeline";
const freshView = () => ({ active: 0, zoom: null, playhead: 0, sel: null, open: {} });
const OWNERS = new WeakMap();      // editor root → its editor
const HANDOVER = new Map();        // node id → { graphId, view } of an editor destroyed during the current task
const UPLOAD_SUBFOLDER = "mediaforge_timeline";
const NO_SOURCE = "(none)";
const EDITOR_HEIGHT = 700;
const MIN_NODE_WIDTH = 700;
const DEFAULT_OVERLAP = { "h3_5+17n": 22, "causal_4n+1": 5, "causal_8n+1": 9, free: 0 };
const SEGMENT_COLORS = ["#3d7bd9", "#d98a3d", "#3dae73", "#a95ad1", "#d14f6b", "#4cb0c2"];
const KIND_COLORS = { image: "#5c8f3a", video: "#3a6f8f", audio: "#8f6a3a" };
const KIND_ICON = { image: "🖼", video: "🎞", audio: "🔊" };
const ACCEPTS = { image: ["refs", "visual"], video: ["refs", "visual"], audio: ["refs", "audio"] };  // createFromCard's table
const LANE_TOP = 100;
const LANE_ROW = 22;
const SNAP_PX = 8;
const TRACK_MAX_HEIGHT = 240;
const SCROLLBAR_ROOM = 14;
const UPLOAD_CONCURRENCY = 2;
const RAW_TAG = /<(Picture|Video|Audio) [0-9]+>/;
const AUDIO_EXT = /\.(wav|mp3|flac|ogg|aac|m4a|opus)$/i;
const VIDEO_EXT = /\.(mp4|mov|mkv|webm|avi|m4v|ts|mpg|mpeg)$/i;
const IMAGE_EXT = /\.(png|jpe?g|webp|bmp|gif|tiff?)$/i;
const FOCUSABLE = "button, input, select, textarea, summary, .mf-tl-card";

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

function button(text, onclick, attrs = {}) {
    return el("button", { text, onclick, ...attrs });
}

// What identifies a control across a rebuild: its field name, or a button's own words.
function fieldKey(control) {
    return control.getAttribute?.("data-fk") || (control.matches?.("button, summary") ? control.textContent : null);
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
.mf-tl:focus-visible, .mf-tl button:focus-visible, .mf-tl summary:focus-visible, .mf-tl-card:focus-visible, .mf-tl-item:focus-visible,
.mf-tl-seg:focus-visible { outline: 2px solid #ffd23f; }
.mf-tl button { background: var(--comfy-input-bg, #222); color: inherit; border: 1px solid var(--border-color, #444);
  border-radius: 4px; padding: 2px 8px; cursor: pointer; }
.mf-tl button:disabled { opacity: .45; cursor: default; }
.mf-tl-bar { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; flex: none; }
.mf-tl-group { display: flex; gap: 4px; align-items: center; }
.mf-tl-top { display: flex; gap: 6px; height: 118px; flex: none; }
.mf-tl-col { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.mf-tl-col.preview { width: 210px; flex: none; }
.mf-tl-col.library { flex: 1; }
.mf-tl-head { display: flex; align-items: center; justify-content: space-between; gap: 6px; height: 22px; flex: none; }
.mf-tl-head .mf-tl-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mf-tl-head button { flex: none; padding: 1px 6px; font-size: 11px; }
.mf-tl-video { width: 100%; flex: 1; min-height: 0; background: #000; border-radius: 4px; object-fit: contain; }
.mf-tl-placeholder { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; text-align: center;
  padding: 4px 8px; border: 1px dashed var(--border-color, #555); border-radius: 4px; font-size: 11px; opacity: .8; }
.mf-tl-lib { flex: 1; min-height: 0; display: flex; gap: 4px; overflow-x: auto; overflow-y: hidden; align-items: stretch;
  border: 1px dashed var(--border-color, #555); border-radius: 4px; padding: 4px; }
.mf-tl-lib.drop { border-color: #ffd23f; }
.mf-tl-lib > .mf-tl-label { align-self: center; padding: 0 6px; }
.mf-tl-card { flex: none; width: 100px; display: flex; flex-direction: column; gap: 1px; padding: 3px; border-radius: 4px;
  background: var(--comfy-input-bg, #262626); border: 1px solid transparent; cursor: grab; touch-action: none; user-select: none; }
.mf-tl-card.selected { border-color: #fff; }
.mf-tl-card.pending { cursor: default; opacity: .6; }
.mf-tl-card.failed { opacity: 1; }
.mf-tl-card .acts { display: flex; gap: 2px; }
.mf-tl-card .acts button { padding: 0 4px; font-size: 10px; }
.mf-tl-card img { width: 100%; height: 40px; flex: none; object-fit: cover; border-radius: 3px; background: #111; pointer-events: none; }
.mf-tl-card .icon { height: 40px; flex: none; display: flex; align-items: center; justify-content: center; font-size: 20px; background: #111; border-radius: 3px; }
.mf-tl-card.failed .icon { height: 16px; font-size: 12px; }
.mf-tl-card .name { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mf-tl-card .state { font-size: 10px; opacity: .75; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mf-tl-card .state.error { color: #ff7a7a; opacity: 1; }
.mf-tl-ghost { position: fixed; z-index: 10000; pointer-events: none; padding: 2px 8px; border-radius: 4px; background: #ffd23f;
  color: #000; font: 12px sans-serif; transform: translate(8px, 8px); }
.mf-tl-label { opacity: .75; font-size: 11px; }
.mf-tl-timeline { display: flex; flex: none; overflow: hidden; border: 1px solid var(--border-color, #444); border-radius: 4px;
  background: var(--comfy-input-bg, #1a1a1a); }
.mf-tl-gutter { position: relative; width: 62px; flex: none; overflow: hidden; border-right: 1px solid var(--border-color, #444); }
.mf-tl-gutter-rows { position: absolute; left: 0; right: 0; top: 0; }
.mf-tl-gutter-rows span { position: absolute; left: 0; right: 0; padding: 0 6px; font-size: 11px; white-space: nowrap;
  overflow: hidden; line-height: ${LANE_ROW - 2}px; }
.mf-tl-scroller { position: relative; overflow: auto; flex: 1; min-width: 0; touch-action: pan-x pan-y; }
.mf-tl-track { position: relative; }
.mf-tl-ruler { position: absolute; left: 0; top: 0; height: 18px; }
.mf-tl-lane { position: absolute; left: 0; height: ${LANE_ROW - 2}px; background: rgba(255,255,255,.04); pointer-events: none; }
.mf-tl-lane.drop { background: rgba(255,210,63,.18); }
.mf-tl-lane.refuse { background: rgba(255,90,90,.22); }
.mf-tl-lane.accept { background: rgba(255,210,63,.08); box-shadow: inset 0 0 0 1px rgba(255,210,63,.5); }
.mf-tl-lane.reject { opacity: .3; }
.mf-tl-lane span { position: sticky; left: 6px; font-size: 11px; opacity: .55; line-height: ${LANE_ROW - 2}px; white-space: nowrap; }
.mf-tl-seg { position: absolute; height: 30px; border-radius: 4px; opacity: .85; cursor: pointer; overflow: hidden;
  font-size: 11px; line-height: 30px; padding: 0 10px; white-space: nowrap; color: #fff; touch-action: none; }
.mf-tl-seg > span { position: relative; padding: 1px 4px; border-radius: 3px; background: rgba(0,0,0,.45); }
.mf-tl-seg.active { outline: 2px solid #fff; opacity: 1; }
.mf-tl-seg.invalid { outline: 2px solid #ff5a5a; }
.mf-tl-seg .overlap { position: absolute; top: 0; left: 0; height: 100%;
  background: repeating-linear-gradient(45deg, rgba(0,0,0,.28) 0 4px, transparent 4px 8px); pointer-events: none; }
.mf-tl-handle { position: absolute; top: 0; width: 8px; height: 100%; cursor: ew-resize; background: rgba(255,255,255,.35); }
.mf-tl-handle[data-edge=left] { left: 0; } .mf-tl-handle[data-edge=right] { right: 0; }
.mf-tl-item { position: absolute; height: ${LANE_ROW - 4}px; border-radius: 3px; color: #fff; font-size: 10px;
  line-height: ${LANE_ROW - 4}px; padding: 0 8px; white-space: nowrap; overflow: hidden; cursor: grab; touch-action: none; }
.mf-tl-item.selected { outline: 2px solid #fff; }
.mf-tl-item.invalid { outline: 2px solid #ff5a5a; }
.mf-tl-item.refused, .mf-tl-seg.refused { outline: 2px dashed #ff5a5a; }
.mf-tl-item.keyframe { width: 12px !important; padding: 0; transform: translateX(-6px) rotate(45deg) scale(.7); border-radius: 2px; }
.mf-tl-item .mf-tl-handle { width: 6px; }
.mf-tl-playhead { position: absolute; top: 0; width: 2px; height: 100%; background: #ffd23f; pointer-events: none; }
.mf-tl-status { flex: none; display: flex; flex-direction: column; gap: 1px; min-height: 34px; }
.mf-tl-status-row { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; min-height: 19px; }
.mf-tl-status.bad .mf-tl-status-text { color: #ff7a7a; } .mf-tl-status.good .mf-tl-status-text { color: #8fd18f; }
.mf-tl-status.warn .mf-tl-status-text { color: #ffb36b; }
.mf-tl-status.locate .mf-tl-status-text { cursor: pointer; text-decoration: underline dotted; }
.mf-tl-status button { padding: 0 6px; font-size: 11px; }
.mf-tl-status details { font-size: 11px; opacity: .8; } .mf-tl-status summary { cursor: pointer; }
.mf-tl-notice { font-size: 11px; opacity: .8; min-height: 14px; }
.mf-tl-form { flex: 1; min-height: 0; overflow-y: auto; display: grid; grid-template-columns: auto 1fr; gap: 4px 8px;
  align-items: center; align-content: start; }
.mf-tl-form input, .mf-tl-form select, .mf-tl-form textarea, .mf-tl-panel input, .mf-tl-panel select, .mf-tl-panel textarea {
  background: var(--comfy-input-bg, #222); color: inherit; border: 1px solid var(--border-color, #444); border-radius: 3px;
  padding: 2px 4px; font: inherit; }
.mf-tl-form textarea { resize: vertical; min-height: 38px; width: 100%; }
.mf-tl-form textarea.main { min-height: 56px; }
.mf-tl-form input[type=checkbox] { justify-self: start; }
.mf-tl-form .full { grid-column: 1 / -1; }
.mf-tl-form > button { justify-self: start; }
.mf-tl-form .hint { font-size: 11px; opacity: .75; }
.mf-tl-form .warn { color: #ffb36b; } .mf-tl-form .bad { color: #ff7a7a; }
.mf-tl-form details { border: 1px solid var(--border-color, #444); border-radius: 4px; padding: 3px 6px; min-width: 0; }
.mf-tl-form summary { cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mf-tl-form details > .body { display: grid; grid-template-columns: auto 1fr; gap: 4px 8px; align-items: center; margin-top: 4px; }
.mf-tl-chips { display: flex; flex-wrap: wrap; gap: 3px; align-items: center; }
.mf-tl-chips button { padding: 0 6px; font-size: 11px; }
.mf-tl-chips button.add { border-style: dashed; }
.mf-tl-chips input.seed { width: 270px; }
.mf-tl-stepper { display: flex; align-items: center; gap: 6px; }
.mf-tl-stepper button { padding: 0 8px; }
.mf-tl-panel { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; padding: 4px; border: 1px dashed var(--border-color, #555); border-radius: 4px; }
.mf-tl-panel input { width: 64px; } .mf-tl-panel textarea { width: 100%; min-height: 48px; }
.mf-tl-panel > div { width: 100%; font-size: 11px; }
.mf-tl-panel .bad { color: #ff7a7a; }
.mf-tl-cov { position: relative; height: 12px; margin: 3px 0; border-radius: 2px; background: rgba(255,255,255,.08); overflow: hidden; }
.mf-tl-cov div { position: absolute; top: 0; height: 100%; }
.mf-tl-cov .carry { background: repeating-linear-gradient(45deg, #3d7bd9 0 3px, transparent 3px 6px); }
.mf-tl-cov .lost { background: repeating-linear-gradient(45deg, #ff5a5a 0 3px, transparent 3px 6px); }
.mf-tl-cov .master { background: rgba(217,138,61,.4); }
.mf-tl-cov .locked { top: 3px; height: 6px; background: #d98a3d; }
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
        this.gesture = null;             // the one pointer gesture in progress: segment edge / bar / card
        this.editGen = 0;                // bumps whenever this.data is replaced; an older gesture never commits
        this.loadGen = 0;                // bumps on hydrate only (undo / redo / configure / JSON fix)
        this.held = false;               // library / inspector rebuild deferred until the gesture or typing ends
        this.raf = 0;
        this.notice = "";
        this.statusBase = { className: "mf-tl-status", text: "" };
        this.media = new Map();          // material id → {ref, rev, status, info, error}
        this.mediaRevision = 0;
        this.probeChain = Promise.resolve();
        this.uploads = [];               // editor-wide queue: {file, kind, batch, status: queued|uploading|done|failed, ref, error}
        this.uploading = 0;
        this.sourceUpload = null;        // the latest 上傳影片 pick; only that one may land (see upload())
        this.deferredBatches = [];       // batches that finished during a gesture; committed after it
        this.previewKey = null;          // material shown in the preview slot instead of the source
        this.press = null;               // the press in progress on an inspector / library control
        this.fieldCommit = false;        // a field's change event is committing: rebuild once focus has settled
        this.fieldTimer = 0;
        this.fixKey = "";
        this.gutterKey = "";
        this.destroyed = false;
        this.view = freshView();
        this.build();
        OWNERS.set(this.root, this);
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
        this.applyWidgetLabels();
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
        // The data is replaced (undo / redo / configure / JSON fix): nothing started against the
        // old data may land on the new one — drags end here, upload batches and the inspector are
        // dropped / rebuilt.
        this.endGesture();
        this.endPress();
        this.fieldCommit = false;
        clearTimeout(this.fieldTimer);
        this.loadGen++;
        this.editGen++;
        this.uploads = [];
        this.deferredBatches = [];
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
        this.render(true);
    }

    commit(next, notice = "") {
        // Every edit path ends here; a panel or form left over from before the JSON broke must not
        // overwrite the text the user is fixing.
        if (this.broken || this.destroyed) return;
        const tracker = changeTracker();
        tracker?.beforeChange?.();
        try {
            this.data = next;
            this.editGen++;
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

    // command result (timeline_edit) → commit, or the refusal in the status line. → whether it was committed
    apply(result, okNotice = "") {
        if (!result.ok) {
            this.notice = this.refusalText(result);
            this.render();
            return false;
        }
        this.commitWithImpact(result.next, [okNotice, ...(result.warnings ?? []).map((w) => this.warningText(w))]);
        return true;
    }

    // Edits that are not timeline_edit commands (segment edges, prompts, seams, auto-fill…) pass the same gate.
    // → whether it was committed
    propose(next, notice = "") {
        const problem = checkEdit(this.data, next, this.editContext());
        if (problem) return this.apply({ ok: false, reason: "breaks", problem });
        this.commitWithImpact(next, [notice]);
        return true;
    }

    // §8.2: an edit that changes what some segment sends to the model says so (snapshots permitting). A legal
    // edit can also change what the prompt means, so those changes are named: always for a numbered tag that now
    // names another material, and for references changing segment when the segments themselves moved.
    commitWithImpact(next, notes) {
        const ctx = this.editContext();
        const impact = inputImpact(this.data, next, ctx);
        if (impact?.length) {
            const shape = (d) => d.segments.map((s) => `${s.start},${s.end}`).join();
            const moved = shape(this.data) !== shape(next);
            notes.push(...refChanges(this.data, next, ctx).filter((c) => c.kind === "tag" || moved).map((c) => this.refChangeText(c)));
            notes.push(S.impact(impact.map((i) => i + 1)));
        }
        this.commit(next, notes.filter(Boolean).join(" · "));
    }

    refChangeText(c) {
        if (c.kind === "tag") {
            const same = c.from === c.to;
            return S.refTagMoved(c.segment + 1, c.tag, same ? S.refTagStart(c.from, c.fromStart, c.fromLength) : c.from, same ? S.refTagStart(c.to, c.toStart, c.toLength) : c.to);
        }
        return c.kind === "gone" ? S.refGone(c.segment + 1, c.name) : S.refAdded(c.segment + 1, c.name);
    }

    editContext(st = this.settings()) {
        const snapshots = {};
        for (const m of this.data.materials) {
            const snap = this.snapshot(m.id);
            if (snap) snapshots[m.id] = snap;
        }
        return { mode: st.mode, fpsKey: st.fpsKey, grid: st.grid, h3: st.h3, sourceFrames: st.sourceFrames, hasSource: st.hasSource, snapshots };
    }

    refusalText(r) {
        if (r.reason === "breaks") return `${S.editReasons.breaks}：${this.problemText(r.problem)}`;
        const text = S.editReasons[r.reason] ?? S.reasons[r.reason] ?? S.problemReasons[r.reason] ?? r.reason;
        return typeof text === "function" ? text(r) : text;
    }

    warningText(w) {
        const [key, id] = w.split(":");
        const text = S.editWarnings[key];
        return typeof text === "function" ? text(this.itemLabel(id)) : text ?? w;
    }

    itemLabel(id) {
        const ref = this.data.refs.find((x) => x.id === id);
        const item = ref ?? this.data.anchors.find((x) => x.id === id);
        if (!item) return id;
        const name = this.data.materials.find((m) => m.id === item.material)?.name ?? "?";
        return `${name}（${ref ? S.roles.ref : S.roles[item.role]}）`;
    }

    // `segments[1].start` → 第 2 段; the raw location stays available in the status line's details
    placeText(location) {
        const segment = /^segments\[(\d+)\]/.exec(location ?? "");
        if (segment) return S.segment(Number(segment[1]) + 1);
        return S.places[/^[a-z_]+/.exec(location ?? "")?.[0]] ?? location;
    }

    problemText(p) {
        const e = p.error;
        const where = p.segment != null ? `${S.segment(p.segment + 1)}：` : "";
        const why = () => S.problemReasons[e.reason] ?? S.unknownProblem;
        if (p.stage === "schema") return S.badField(this.placeText(e.location));
        if (p.stage === "grid") return S.invalid(this.placeText(e.location), S.reasons[e.reason] ?? e.reason);
        if (e.code === "E_ANCHOR_OVERLAP") {
            return where + S.anchorOverlap(S.streams[e.stream] ?? e.stream, this.itemLabel(e.first), this.itemLabel(e.second));
        }
        if (p.stage === "geometry") return `${where}${this.itemLabel(e.ref ?? e.anchor)}：${why()}`;
        if (e.code === "E_REF_SHORT") return where + S.refShort(e.name, e.frames);
        if (e.code === "E_REF_LIMIT") return where + S.refLimit(S.tagKinds[e.kind] ?? e.kind, e.count, e.limit);
        if (p.stage === "prompt") return where + S.promptProblem(why());
        return where + why();
    }

    // the exact location and code, for the status line's details; an unknown error keeps all its fields
    problemDetail(p) {
        const e = p.error;
        return S.problemReasons[e.reason] || S.reasons[e.reason] ? S.detailsText(e.location, e.code) : this.errorText(e);
    }

    // where a problem lives, for "click the status to locate it"
    problemTarget(p) {
        const e = p.error;
        const id = e.ref ?? e.anchor ?? e.second;
        if (p.stage === "geometry" && id) return { type: e.ref ? "ref" : "anchor", id };
        return p.segment != null ? { type: "segment", index: p.segment } : null;
    }

    // Nodes 2.0 keeps the widget component of a node id across a graph reload (undo / redo, switching workflow),
    // so the new editor's root is never mounted and the destroyed editor's root stays on screen. Only one graph
    // is shown, so a destroyed editor's root still on screen for this node id is that stale mount: take its
    // place. A live editor is never displaced (a clone configured before it is added has the source's id).
    adoptMount() {
        if (this.root.isConnected) return;
        for (const root of document.querySelectorAll(".mf-tl")) {
            const owner = OWNERS.get(root);
            if (owner && owner !== this && owner.destroyed && owner.node.id === this.node.id) {
                root.replaceWith(this.root);
                return;
            }
        }
    }

    select(sel) {
        this.view.sel = sel;
        if (sel?.type === "segment") this.view.active = sel.index;
    }

    // The segment's own time: its new frames in generate (the overlap belongs to the previous segment's output),
    // its source range in cut.
    segmentWindow(index, st = this.settings()) {
        const segs = this.data.segments;
        return st.mode === "cut" ? [segs[index].start, segs[index].end] : emitIntervals(segs)[index];
    }

    // The user picked a segment: what they look at follows, unless the playhead is already inside it. Only explicit
    // picks come here; playback, rebuilds, undo and the view hand-over never move the playhead.
    selectSegment(index) {
        this.video.pause();
        this.materialVideo.pause();
        this.select({ type: "segment", index });
        const [lo, hi] = this.segmentWindow(index);
        if (this.view.playhead < lo || this.view.playhead >= hi) this.seek(lo);
    }

    // keyboard focus selects (Tab through bars and segments); pointer selection already happened in onPointerDown.
    // Focus also comes back when the window is reactivated (no relatedTarget): that must not move the playhead.
    focusSelect(sel, e) {
        if (this.gesture) return;
        if (JSON.stringify(sel) === JSON.stringify(this.view.sel)) {
            if (sel.type === "segment" && e?.relatedTarget) this.selectSegment(sel.index);
            return;
        }
        if (sel.type === "segment") this.selectSegment(sel.index);
        else this.select(sel);
        this.render();
    }

    // Friendlier names on the native widgets. Display only: `name`, the values and what gets saved do not change.
    applyWidgetLabels() {
        for (const [name, label] of Object.entries(S.widgets)) if (this.w[name]) this.w[name].label = label;
        const labeler = (name, text) => {
            const w = this.w[name];
            if (w?.options) w.options.getOptionLabel = (v) => text(v ?? w.value);
        };
        labeler("mode", (v) => S.modes[v] ?? String(v));
        labeler("grid", (v) => S.grids[v] ?? String(v));
        labeler("fps", (v) => {
            const rate = FPS_PRESETS[v];
            return rate ? S.fpsOption(String(v), rate[0] / rate[1]) : String(v);
        });
        // read at call time: the same (none) is fine in generate and missing in cut
        labeler("video", (v) => (v === NO_SOURCE ? (this.w.mode?.value === "cut" ? S.noneCut : S.noneGenerate) : String(v ?? "")));
    }

    setWidget(name, value) {
        const w = this.w[name];
        if (!w) return;
        const tracker = changeTracker();
        tracker?.beforeChange?.();
        try {
            w.value = value;
            w.callback?.(value);
            this.node.setDirtyCanvas?.(true, true);
        } finally {
            tracker?.afterChange?.();
        }
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
                    const issue = ok ? probeIssue(body, m.kind) : null;
                    if (ok && !issue) Object.assign(entry, { status: "ready", info: body });
                    else Object.assign(entry, { status: "error", error: ok ? S.probeIssues[issue] : body?.error?.message ?? S.previewError });
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

    // Snapshot in the same shape Python's describe_material builds (frames at the current fps). It
    // names the file / kind it was probed for, so an edit that changes them counts as unverified.
    snapshot(id) {
        const m = this.data.materials.find((x) => x.id === id);
        const entry = this.media.get(id);
        if (!m || entry?.status !== "ready") return null;
        const info = entry.info;
        const [num, den] = this.settings().fps;
        const frames = info.duration_us == null ? null
            : Number((BigInt(info.duration_us) * BigInt(num)) / (BigInt(den) * 1000000n));
        return { ...info, id: m.id, kind: entry.kind, name: m.name, ref: entry.ref, frames };
    }

    // ---------- DOM ----------

    build() {
        this.fileInput = el("input", { type: "file", accept: "video/*", style: { display: "none" },
            onchange: () => this.fileInput.files[0] && this.upload(this.fileInput.files[0]) });
        this.materialInput = el("input", { type: "file", accept: "image/*,video/*,audio/*", multiple: "", style: { display: "none" },
            onchange: () => this.uploadMaterials([...this.materialInput.files]) });
        this.previewLabel = el("span", { class: "mf-tl-label", text: S.sourcePreview });
        this.previewEmpty = el("div", { class: "mf-tl-placeholder" });
        this.video = el("video", { class: "mf-tl-video", muted: "", playsinline: "", preload: "metadata" });
        this.video.addEventListener("error", () => this.onVideoError());
        this.video.addEventListener("timeupdate", () => this.onVideoTime());
        // the selected material shows in the same slot, in its own elements, so the source preview
        // (proxy fallback, playhead sync) keeps its state underneath
        this.materialLabel = el("span", { class: "mf-tl-label", style: { display: "none" } });
        this.materialImage = el("img", { class: "mf-tl-video", alt: "", style: { display: "none" } });
        this.materialVideo = el("video", { class: "mf-tl-video", controls: "", playsinline: "", preload: "metadata", style: { display: "none" } });
        for (const media of [this.materialImage, this.materialVideo]) {
            media.addEventListener("error", () => {
                if (media.getAttribute("src")) this.materialLabel.textContent = S.materialPreviewError;
            });
        }
        for (const media of [this.video, this.materialVideo]) {
            for (const type of ["play", "pause", "ended"]) media.addEventListener(type, () => this.syncPlayButton());
        }
        this.lib = el("div", { class: "mf-tl-lib", title: S.library });
        this.materialButton = button(S.addMaterials, () => this.materialInput.click());
        this.libraryHead = el("div", { class: "mf-tl-head" }, [
            el("span", { class: "mf-tl-label", text: S.libraryTitle }), this.materialButton, this.materialInput,
        ]);
        this.cutHint = el("div", { class: "mf-tl-placeholder", text: S.cutHint });
        const top = el("div", { class: "mf-tl-top" }, [
            el("div", { class: "mf-tl-col preview" }, [
                el("div", { class: "mf-tl-head" }, [this.previewLabel, this.materialLabel, button(S.upload, () => this.fileInput.click()), this.fileInput]),
                this.previewEmpty, this.video, this.materialImage, this.materialVideo,
            ]),
            el("div", { class: "mf-tl-col library" }, [this.libraryHead, this.lib, this.cutHint]),
        ]);
        this.frameLabel = el("span", { class: "mf-tl-label" });
        this.playButton = button(S.playSource, () => this.togglePlay());
        this.addButton = button(S.add, () => this.addSegment());
        this.deleteButton = button(S.deleteLast, () => this.deleteLast());
        this.fillButton = button(S.autoFill, () => this.togglePanel("fill"));
        this.importButton = button(S.importScenes, () => this.togglePanel("import"));
        const group = (...items) => el("div", { class: "mf-tl-group" }, items);
        this.bar = el("div", { class: "mf-tl-bar" }, [
            group(this.playButton, this.frameLabel),
            group(this.addButton, this.deleteButton, this.fillButton, this.importButton),
            group(button("−", () => this.zoom(0.5), { title: S.zoomOut }), button("＋", () => this.zoom(2), { title: S.zoomIn }), button(S.fit, () => this.zoom(null))),
            group(button(S.help, () => this.togglePanel("help"), { title: S.helpTitle })),
        ]);
        this.ruler = el("canvas", { class: "mf-tl-ruler", height: "18" });
        this.playhead = el("div", { class: "mf-tl-playhead" });
        this.track = el("div", { class: "mf-tl-track" }, [this.ruler, this.playhead]);
        this.scroller = el("div", { class: "mf-tl-scroller" }, [this.track]);
        this.gutterRows = el("div", { class: "mf-tl-gutter-rows" });
        this.timeline = el("div", { class: "mf-tl-timeline" }, [el("div", { class: "mf-tl-gutter" }, [this.gutterRows]), this.scroller]);
        // follows by offset rather than being a second scroll area: a horizontal scrollbar makes the two scroll ranges differ
        this.scroller.addEventListener("scroll", () => {
            this.gutterRows.style.transform = `translateY(${-this.scroller.scrollTop}px)`;
        });
        this.track.addEventListener("pointerdown", (e) => this.onPointerDown(e));
        this.track.addEventListener("pointermove", (e) => this.onPointerMove(e));
        this.track.addEventListener("pointerup", (e) => this.onPointerUp(e));
        for (const type of ["pointercancel", "lostpointercapture"]) {
            this.track.addEventListener(type, (e) => {
                const g = this.gesture;
                if (g && g.target === this.track && e.pointerId === g.pointerId) this.cancelGesture();
            });
        }
        this.scroller.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
        this.panel = el("div");
        this.form = el("div", { class: "mf-tl-form" });
        // see render(): a field committing on its way out must not rebuild the control focus is moving to
        this.form.addEventListener("change", () => this.noteFieldCommit(), true);
        this.form.addEventListener("focusout", () => setTimeout(() => this.flushHeld()));
        this.statusText = el("span", { class: "mf-tl-status-text" });
        this.statusFixes = el("span", { class: "mf-tl-group" });
        this.statusDetailText = el("span");
        this.statusDetail = el("details", { style: { display: "none" } }, [el("summary", { text: S.details }), " ", this.statusDetailText]);
        this.noticeLine = el("div", { class: "mf-tl-notice" });
        this.status = el("div", { class: "mf-tl-status", role: "status" }, [
            el("div", { class: "mf-tl-status-row" }, [this.statusText, this.statusFixes, this.statusDetail]), this.noticeLine,
        ]);
        this.statusText.addEventListener("click", () => {
            const target = this.statusTarget;
            if (!target || this.gesture) return;
            if (target.type === "segment") this.selectSegment(target.index);
            else this.select(target);
            this.render();
        });
        this.root = el("div", { class: "mf-tl", tabindex: "0", onkeydown: (e) => this.onKey(e) }, [
            top, this.bar, this.panel, this.timeline, this.status, this.form,
        ]);
        this.root.addEventListener("pointerdown", (e) => this.beginPress(e), true);
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

    // While a gesture runs, an inspector field is being typed in or committing, or a control is pressed, only the
    // track and the status line repaint: rebuilding the library would drop the card holding pointer capture,
    // rebuilding the inspector would drop the half-typed value or the very button being clicked (mousedown blurs
    // the field, its change commits, and a rebuild here means click never fires). The held rebuild runs when these
    // end; `force` (hydrate) rebuilds anyway because the old inspector belongs to replaced data.
    render(force = false) {
        if (this.destroyed) return;
        if (this.broken) {
            this.endGesture();
            this.status.className = "mf-tl-status bad";
            this.statusText.textContent = `⚠ ${S.jsonBroken}（${this.brokenAt}）`;
            this.statusFixes.replaceChildren();
            this.fixKey = "";
            this.noticeLine.textContent = "";
            this.statusTarget = null;
            this.form.replaceChildren();
            this.panel.replaceChildren();
            this.panelKind = null;
            for (const b of [this.addButton, this.deleteButton, this.fillButton, this.importButton, this.materialButton]) b.disabled = true;
            return;
        }
        const st = this.settings();
        const generate = st.mode === "generate";
        this.ctx = this.editContext(st);
        this.result = validate(this.data, st.mode, st.fpsKey, st.grid, st.sourceFrames, st.hasSource);
        this.check = timelineProblems(this.data, this.ctx);
        this.addButton.disabled = this.fillButton.disabled = this.importButton.disabled = this.materialButton.disabled = false;
        this.addButton.textContent = generate ? S.add : S.addCut;
        this.deleteButton.disabled = this.data.segments.length <= 1;
        this.deleteButton.style.display = this.fillButton.style.display = generate ? "" : "none";
        this.importButton.style.display = generate ? "none" : "";
        this.held = !force && (!!this.gesture || this.typing() || !!this.press || this.fieldCommit);
        if (!this.held) {
            this.renderLibrary(st);
            this.renderForm(st);
            this.syncPreview(st);
        }
        this.renderTrack();
        this.renderStatus(st);
    }

    typing() {
        const a = document.activeElement;
        return !!a && this.form.contains(a) && a.matches("textarea, input[type=text], input[type=number]");
    }

    flushHeld() {
        if (this.held && !this.destroyed && !this.gesture && !this.press && !this.fieldCommit && !this.typing()) this.render();
    }

    noteFieldCommit() {
        this.fieldCommit = true;
        clearTimeout(this.fieldTimer);
        this.fieldTimer = setTimeout(() => {
            this.fieldCommit = false;
            this.flushHeld();
        });
    }

    // A press on an inspector or library control keeps it in the DOM until its click has run. Cards are not covered:
    // their drag is a gesture, which holds the rebuild by itself.
    beginPress(e) {
        const onControl = e.target.closest?.(".mf-tl-form") || (e.target.closest?.("button") && e.target.closest?.(".mf-tl-lib"));
        if (!onControl) return;
        this.endPress();
        const press = { pointerId: e.pointerId, timer: 0 };
        const finish = (ev) => {
            if (ev.pointerId !== undefined && ev.pointerId !== press.pointerId) return;
            press.unbind();
            // click fires right after pointerup, in the same task
            press.timer = setTimeout(() => {
                if (this.press !== press) return;
                this.press = null;
                this.flushHeld();
            });
        };
        // Capture for the pointer events: Nodes 2.0 stops some on their way up, and a press that never ends holds the form
        // forever. Not for blur: the field losing focus to this very press would reach window's capture listeners too.
        const bound = [["pointerup", true], ["pointercancel", true], ["blur", false]];
        for (const [type, capture] of bound) window.addEventListener(type, finish, capture);
        press.unbind = () => bound.forEach(([type, capture]) => window.removeEventListener(type, finish, capture));
        this.press = press;
    }

    endPress() {
        const press = this.press;
        if (!press) return;
        this.press = null;
        press.unbind();
        clearTimeout(press.timer);
    }

    // `toggle` is asynchronous: a rebuild right after the click on a section header runs before it, and the section
    // would close again. The open state is on the element already, so read it before building its replacement.
    keepSections(container) {
        for (const section of container.querySelectorAll("details")) if (section.mfKey) this.view.open[section.mfKey] = section.open;
    }

    // Replaces a panel's controls. Focus only comes back when this rebuild took it away, and only to the same field of
    // the same object: a material finishing its probe must not pull focus out of another node's text box.
    rebuild(container, children) {
        const active = document.activeElement;
        const hadFocus = !!active && container.contains(active);
        const key = hadFocus ? fieldKey(active) : null;
        const scope = JSON.stringify([this.view.sel, this.view.active]);
        const sameObject = container.mfScope === scope;
        container.mfScope = scope;
        container.replaceChildren(...children);
        if (!hadFocus) return;
        const again = sameObject && key ? [...container.querySelectorAll(FOCUSABLE)].find((c) => fieldKey(c) === key) : null;
        (again ?? this.root).focus({ preventScroll: true });
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
        const generate = st.mode === "generate";
        this.libraryHead.style.display = this.lib.style.display = generate ? "" : "none";
        this.cutHint.style.display = generate ? "none" : "";
        const [num, den] = st.fps;
        const cards = this.data.materials.map((m) => {
            const entry = this.media.get(m.id);
            const status = entry?.status ?? "probing";
            const seconds = entry?.info?.duration_us != null ? entry.info.duration_us / 1e6 : null;
            const size = m.kind === "image" && entry?.info?.width ? `${entry.info.width}×${entry.info.height}` : null;
            const state = entry?.error ?? (status !== "ready" ? S.status[status]
                : seconds != null ? S.duration(seconds, Math.floor((seconds * num) / den)) : size ?? S.status.ready);
            const thumb = m.kind === "image"
                ? el("img", { src: viewURL({ ...splitSource(m.ref), type: "input" }), alt: "" })
                : el("div", { class: "icon", text: KIND_ICON[m.kind] });
            const card = el("div", {
                class: `mf-tl-card${this.view.sel?.type === "material" && this.view.sel.id === m.id ? " selected" : ""}`,
                tabindex: "0", title: `${S.kinds[m.kind]}：${m.ref}`, "data-fk": `card:${m.id}`,
            }, [thumb, el("span", { class: "name", text: m.name }),
                el("span", { class: `state${status === "error" ? " error" : ""}`, text: status === "error" ? `⚠ ${state}` : state })]);
            card.dataset.id = m.id;
            card.addEventListener("pointerdown", (e) => this.startCardDrag(e, m, card));
            card.addEventListener("keydown", (e) => {
                if (e.key !== "Enter" && e.key !== " ") return;
                e.preventDefault();
                e.stopPropagation();
                this.select({ type: "material", id: m.id });
                this.render();
            });
            return card;
        });
        // upload items are not in timeline_data; failed ones stay until retried or removed
        const pending = this.uploads.map((u) => {
            const failed = u.status === "failed";
            return el("div", { class: `mf-tl-card pending${failed ? " failed" : ""}`, title: u.file.name }, [
                el("div", { class: "icon", text: KIND_ICON[u.kind] ?? "?" }), el("span", { class: "name", text: u.file.name }),
                el("span", { class: `state${failed ? " error" : ""}`, text: failed ? `⚠ ${u.error}` : S.uploadState[u.status] ?? S.status.uploading }),
                failed ? el("div", { class: "acts" }, [
                    el("button", { text: S.retry, onclick: () => this.retryUpload(u) }),
                    el("button", { text: S.remove, onclick: () => this.dropUpload(u) }),
                ]) : null,
            ]);
        });
        const empty = generate && !cards.length && !pending.length
            ? [el("span", { class: "mf-tl-label", text: S.emptyLibrary })] : [];
        this.rebuild(this.lib, [...cards, ...pending, ...empty]);
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
        const g = this.gesture;
        const data = g?.draft ?? this.data;
        const segs = data.segments;
        const sec = this.secondsOf(st);
        const extent = Math.max(...segs.map((s) => s.end), st.sourceFrames ?? 0, 1);
        this.px = this.pxPerFrame(extent);
        const width = Math.ceil(extent * this.px) + 2;
        const geometry = this.layout(data, st);
        const height = Math.max(geometry.height, 96);
        this.lanes = geometry.lanes;
        this.track.style.width = `${width}px`;
        this.track.style.height = `${height}px`;
        // only as tall as its content: what is left goes to the inspector
        this.timeline.style.height = `${Math.min(height + SCROLLBAR_ROOM, TRACK_MAX_HEIGHT)}px`;
        this.drawRuler(width, extent, st.fps);
        while (this.segEls.length > segs.length) this.segEls.pop().remove();
        while (this.segEls.length < segs.length) {
            const handles = ["left", "right"].map((edge) => el("div", { class: "mf-tl-handle", "data-edge": edge }));
            const label = el("span");
            const overlap = el("div", { class: "overlap" });
            const seg = el("div", { class: "mf-tl-seg", tabindex: "0" }, [overlap, label, ...handles]);
            seg.addEventListener("focus", (e) => this.focusSelect({ type: "segment", index: Number(seg.dataset.index) }, e));
            seg.label = label;
            seg.overlap = overlap;
            this.track.insertBefore(seg, this.playhead);
            this.segEls.push(seg);
        }
        // every segment with a problem gets ⚠ (text, not only colour)
        const bad = new Set((this.check?.list ?? []).map((p) => p.segment).filter((i) => i != null));
        segs.forEach((s, i) => {
            const e = this.segEls[i];
            e.dataset.index = String(i);
            e.label.textContent = `${bad.has(i) ? "⚠ " : ""}${S.barLabel(i + 1, sec(s.end - s.start))}${s.seed != null ? " · 🎲" : ""}`;
            // incoming overlap is hatched: those frames belong to the previous segment's output
            const incoming = st.mode === "generate" && i ? Math.max(0, segs[i - 1].end - s.start) : 0;
            e.title = S.barTitle(i + 1, sec(s.start), sec(s.end), sec(incoming));
            e.overlap.style.width = `${incoming * this.px}px`;
            Object.assign(e.style, {
                left: `${s.start * this.px}px`, width: `${Math.max(6, (s.end - s.start) * this.px)}px`,
                top: `${22 + (st.mode === "generate" ? (i % 2) * 36 : 20)}px`, background: SEGMENT_COLORS[i % SEGMENT_COLORS.length],
            });
            e.classList.toggle("active", i === this.view.active && this.view.sel?.type !== "ref" && this.view.sel?.type !== "anchor");
            e.classList.toggle("invalid", bad.has(i));
            e.classList.toggle("refused", g?.type === "segment" && g.index === i && !!g.refused);
        });
        this.renderGutter(geometry, st);
        this.renderLanes(geometry, width, data);
        this.renderItems(data, geometry, st);
        this.playhead.style.left = `${this.view.playhead * this.px}px`;
        this.frameLabel.textContent = S.position(sec(this.view.playhead), this.view.playhead);
        this.syncPlaceButton();
    }

    secondsOf(st) {
        return (frames) => (frames * st.fps[1]) / st.fps[0];
    }

    renderGutter(geometry, st) {
        const rows = [{ label: st.mode === "cut" ? S.laneRanges : S.laneSegments, top: st.mode === "cut" ? 42 : 22 },
            ...geometry.lanes.filter((l) => l.label)];
        const key = rows.map((r) => `${r.label}@${r.top}`).join();
        if (key === this.gutterKey) return;
        this.gutterKey = key;
        this.gutterRows.replaceChildren(...rows.map((r) => el("span", {
            text: r.label, title: S.laneTitles[r.lane] ?? "", style: { top: `${r.top}px` },
        })));
    }

    laneHint(lane, data) {
        if (lane.lane === "refs") return data.refs.length ? "" : S.hintRefs;
        const used = data.anchors.some((a) => (a.role === "lock") === (lane.lane === "audio"));
        return used ? "" : lane.lane === "audio" ? S.hintAudio : S.hintVisual;
    }

    renderLanes(geometry, width, data) {
        while (this.laneEls.length > geometry.lanes.length) this.laneEls.pop().remove();
        while (this.laneEls.length < geometry.lanes.length) {
            const lane = el("div", { class: "mf-tl-lane" }, [el("span")]);
            this.track.insertBefore(lane, this.track.firstChild.nextSibling);
            this.laneEls.push(lane);
        }
        // while a card is dragged, its usable lanes light up before the pointer gets there; whether the spot is
        // legal is still judged under the pointer
        const g = this.gesture;
        const kind = g?.type === "card" && g.moved ? data.materials.find((m) => m.id === g.id)?.kind : null;
        geometry.lanes.forEach((lane, i) => {
            const e = this.laneEls[i];
            e.style.top = `${lane.top}px`;
            e.style.width = `${width}px`;
            e.firstChild.textContent = this.laneHint(lane, data);
            // lane objects are rebuilt on every layout: match by lane / row
            const target = this.dropLane?.lane === lane.lane && this.dropLane.row === lane.row;
            e.classList.toggle("drop", target && !g?.refused);
            e.classList.toggle("refuse", target && !!g?.refused);
            e.classList.toggle("accept", !!kind && !target && ACCEPTS[kind].includes(lane.lane));
            e.classList.toggle("reject", !!kind && !target && !ACCEPTS[kind].includes(lane.lane));
        });
    }

    renderItems(data, geometry, st) {
        const seen = new Set();
        const names = Object.fromEntries(data.materials.map((m) => [m.id, m]));
        const invalid = new Set((this.check?.list ?? []).filter((p) => p.stage === "geometry")
            .flatMap((p) => [p.error.ref, p.error.anchor, p.error.first, p.error.second]).filter(Boolean));
        const g = this.gesture;
        const place = (kind, item, top) => {
            const key = `${kind}:${item.id}`;
            seen.add(key);
            let e = this.itemEls.get(key);
            if (!e) {
                e = el("div", { class: "mf-tl-item", tabindex: "0" }, [el("span")]);
                e.dataset.kind = kind;
                e.dataset.id = item.id;
                e.addEventListener("focus", () => this.focusSelect({ type: kind, id: item.id }));
                this.track.insertBefore(e, this.playhead);
                this.itemEls.set(key, e);
            }
            const m = names[item.material];
            const keyframe = item.role === "keyframe";
            e.classList.toggle("keyframe", keyframe);
            e.classList.toggle("selected", this.view.sel?.type === kind && this.view.sel.id === item.id);
            e.classList.toggle("invalid", invalid.has(item.id));
            e.classList.toggle("refused", g?.type === "bar" && g.kind === kind && g.id === item.id && !!g.refused);
            if (!keyframe && e.querySelectorAll(".mf-tl-handle").length === 0) {
                e.append(el("div", { class: "mf-tl-handle", "data-edge": "left" }), el("div", { class: "mf-tl-handle", "data-edge": "right" }));
            } else if (keyframe) {
                e.querySelectorAll(".mf-tl-handle").forEach((h) => h.remove());
            }
            const [lo, hi] = barSpan(kind, item);
            const warn = invalid.has(item.id) ? "⚠ " : "";
            e.firstChild.textContent = keyframe ? "" : `${warn}${m?.name ?? "?"}${kind === "ref" ? "" : " · " + S.roles[item.role]}`;
            e.title = `${warn}${m?.name ?? "?"} · ${kind === "ref" ? S.roles.ref : S.roles[item.role]}`;
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
            ctx.fillText(step < perSecond ? `${f} ${S.framesUnit}` : S.seconds0(Math.round(f / perSecond)), x + 2, 9);
        }
    }

    // ---------- inspector ----------

    renderForm(st) {
        this.placeButton = null;
        this.keepSections(this.form);
        const sel = this.view.sel;
        const generate = st.mode === "generate";
        const rows = this.isFresh() ? [el("div", { class: "full hint", text: S.firstRun })] : [];
        // prompts are only read when segments are generated: in cut the field would change nothing
        if (generate) rows.push(this.sharedPrompt());
        if (generate && sel?.type === "ref" && this.data.refs.some((r) => r.id === sel.id)) rows.push(...this.refForm(st, sel.id));
        else if (generate && sel?.type === "anchor" && this.data.anchors.some((a) => a.id === sel.id)) rows.push(...this.anchorForm(st, sel.id));
        else if (generate && sel?.type === "material" && this.data.materials.some((m) => m.id === sel.id)) rows.push(...this.materialForm(st, sel.id));
        else rows.push(...this.segmentForm(st));
        this.rebuild(this.form, rows);
    }

    isFresh() {
        const d = this.data;
        return d.segments.length === 1 && !d.materials.length && !d.prompt && !d.segments[0].prompt;
    }

    number(value, onchange, attrs = {}) {
        return el("input", { type: "number", step: "1", value: String(value), onchange: (e) => onchange(Number(e.target.value)), ...attrs });
    }

    // A collapsible section. Its state lives in the view, so rebuilding the form never closes what the user opened;
    // `toggle` only records it, and a section already replaced by a rebuild has no say.
    details(key, summary, rows, open = false) {
        const box = el("details", { class: "full" }, [
            el("summary", { text: summary, "data-fk": `summary:${key}` }), el("div", { class: "body" }, rows),
        ]);
        // a property: el() would set the attribute, and open="false" still means open
        box.open = this.view.open[key] ?? open;
        box.mfKey = key;
        box.addEventListener("toggle", () => {
            if (box.isConnected) this.view.open[key] = box.open;
        });
        return box;
    }

    sharedPrompt() {
        const text = this.data.prompt;
        // decided once: writing the first words must not close the section under the user
        this.view.open.shared ??= this.isFresh();
        return this.details("shared", `${S.globalPrompt}：${text || S.sharedEmpty}`, [
            el("textarea", { class: "full", "data-fk": "shared", onchange: (e) => this.propose({ ...this.data, prompt: e.target.value }) }, [text]),
        ]);
    }

    segmentForm(st) {
        const i = Math.min(this.view.active, this.data.segments.length - 1);
        return st.mode === "cut" ? this.cutForm(st, i) : this.generateForm(st, i);
    }

    generateForm(st, i) {
        const sec = this.secondsOf(st);
        const segs = this.data.segments;
        const s = segs[i];
        const overlap = i ? segs[i - 1].end - s.start : 0;
        return [
            el("div", { class: "full" }, [el("b", { text: S.segment(i + 1) }),
                ` · ${S.range(sec(s.start), sec(s.end))}${overlap ? ` · ${S.overlapPrev(sec(overlap), i)}` : ""}`]),
            el("span", { text: S.prompt }),
            el("textarea", { class: "main", "data-fk": "prompt", placeholder: S.inheritGlobal,
                onchange: (e) => this.propose(this.withSegment(i, { prompt: e.target.value })) }, [s.prompt]),
            ...this.promptSource(i),
            ...this.tagRow(i),
            el("span", { text: S.lengthLabel }), this.lengthStepper(st, i),
            ...this.shotRow(st, i),
            ...this.segmentWarnings(i),
            this.advancedBox(st, i),
            ...this.modelInputs(st, i),
        ];
    }

    // Replacing, not merging, is the part people miss: it stays on screen instead of flashing by in a notice.
    promptSource(i) {
        const own = !!this.data.segments[i].prompt;
        if (!this.data.prompt) return [];
        return [el("span"), el("div", { class: "mf-tl-chips hint" }, own
            ? [S.usingOwn, button(S.backToShared, () => this.propose(this.withSegment(i, { prompt: "" })))] : [S.usingShared])];
    }

    // One click makes a material this segment's reference and writes its tag: uploading, placing and naming are not
    // three separate steps here, and a button can no longer write a tag the segment cannot resolve.
    tagRow(i) {
        const buttons = tagButtons(this.data, i, this.ctx);
        if (buttons.hidden || !this.data.materials.length || buttons.blocked === "manifest") return [];
        if (buttons.blocked) return [el("span", { text: S.chipRow }), el("span", { class: "hint", text: S.chipsBlocked })];
        return [el("span", { text: S.chipRow }), el("div", { class: "mf-tl-chips" }, this.data.materials.map((m) => {
            const item = buttons.items[m.id];
            const chip = el("button", {
                class: item.state === "add" ? "add" : "", "data-fk": `tag:${m.id}`,
                text: item.state === "blocked" ? m.name : `${item.state === "add" ? "＋" : "✓"} ${m.name}`,
                title: item.state === "insert" ? S.chipInsert(m.name) : item.state === "add" ? S.chipAdd(m.name) : this.refusalText(item),
                onclick: () => this.apply(useRefTag(this.data, i, m.id, this.editContext())),
            });
            chip.disabled = item.state === "blocked";
            return chip;
        }))];
    }

    stepper(text, key, down, up) {
        const press = (edit) => () => {
            const r = edit();
            if (r) this.propose({ ...this.data, segments: r.segments });
        };
        const minus = button("−", press(down), { "data-fk": `${key}-` });
        const plus = button("＋", press(up), { "data-fk": `${key}+` });
        minus.disabled = !down();
        plus.disabled = !up();
        return el("div", { class: "mf-tl-stepper" }, [minus, el("span", { text }), plus]);
    }

    lengthStepper(st, i) {
        const sec = this.secondsOf(st);
        const edit = (dir) => () => {
            const segs = this.data.segments;
            const s = segs[i];
            const target = stepLength(st.grid, s.end - s.start, dir, st.fps[0] / st.fps[1]);
            const r = target === null ? null : editGenerate(segs, i, "right", s.start + target, st.grid);
            return r?.ok && r.segments[i].end !== s.end ? r : null;
        };
        const length = this.data.segments[i].end - this.data.segments[i].start;
        return this.stepper(S.duration(sec(length), length), "length", edit(-1), edit(1));
    }

    overlapStepper(st, i) {
        const sec = this.secondsOf(st);
        const edit = (dir) => () => {
            const segs = this.data.segments;
            const before = segs[i - 1];
            const target = stepOverlap(st.grid, before.end - segs[i].start, dir, st.fps[0] / st.fps[1]);
            const r = target === null ? null : editGenerate(segs, i, "left", before.end - target, st.grid);
            return r?.ok && r.segments[i].start !== segs[i].start ? r : null;
        };
        const overlap = this.data.segments[i - 1].end - this.data.segments[i].start;
        return this.stepper(overlap ? S.duration(sec(overlap), overlap) : S.noOverlap, "overlap", edit(-1), edit(1));
    }

    shotRow(st, i) {
        if (i === 0) return [];
        const room = () => {
            const segs = this.data.segments;
            return overlapAtMost(st.grid, Math.min(DEFAULT_OVERLAP[st.grid] ?? 0, segs[i].end - segs[i].start - 1, segs[i - 1].end - segs[i - 1].start - 1));
        };
        const overlapping = () => this.data.segments[i - 1].end - this.data.segments[i].start > 0;
        const cut = overlapping();
        // no default overlap to go back to (free grid): the button would do nothing
        if (!cut && !room()) return [];
        return [
            el("span"),
            button(cut ? S.shotCut : S.shotContinue, () => this.apply(setShotBoundary(this.data, i, overlapping(), room(), this.editContext()))),
            el("span"), el("span", { class: "hint", text: cut ? S.shotCutHint : S.shotContinueHint }),
        ];
    }

    // what must not disappear with a collapsed section
    segmentWarnings(i) {
        const view = segmentInputs(this.data, i, this.ctx);
        if (!view) return [el("div", { class: "full warn", text: S.unverified })];
        if (!view.ok) return [el("div", { class: "full bad", text: this.problemText({ stage: "manifest", error: view.error, segment: i }) })];
        const out = [];
        if (!view.prompt.ok) out.push(el("div", { class: "full bad", text: this.problemText({ stage: "prompt", error: view.prompt.error, segment: i }) }));
        if (view.manifest.carry_conflicts?.length) out.push(el("div", { class: "full warn", text: S.lockCarry }));
        return out;
    }

    advancedBox(st, i) {
        const segs = this.data.segments;
        const s = segs[i];
        const overlap = i ? segs[i - 1].end - s.start : 0;
        const rows = [];
        if (i) rows.push(el("span", { text: S.overlap }), this.overlapStepper(st, i));
        rows.push(el("span", { text: S.start }), this.number(s.start, (v) => this.editEdge(i, "left", v), { "data-fk": "start" }),
            el("span", { text: S.end }), this.number(s.end, (v) => this.editEdge(i, "right", v), { "data-fk": "end" }));
        if (i && overlap > 0) {
            const kind = el("select", { "data-fk": "seam", onchange: (e) => this.propose(this.withSegment(i, {
                seam_in: e.target.value === "crossfade"
                    ? { kind: "crossfade", frames: Math.min(6, crossfadeLimit(this.data.segments, i)), audio_curve: "auto" } : { kind: "cut" },
            })) }, [el("option", { value: "cut", text: S.seamCut }), el("option", { value: "crossfade", text: S.seamCrossfade })]);
            kind.value = s.seam_in.kind;
            rows.push(el("span", { text: S.seam }), kind);
            if (s.seam_in.kind === "crossfade") {
                const seam = () => this.data.segments[i].seam_in;
                const curve = el("select", { "data-fk": "curve", onchange: (e) => this.propose(this.withSegment(i, { seam_in: { ...seam(), audio_curve: e.target.value } })) },
                    Object.entries(S.curves).map(([value, text]) => el("option", { value, text })));
                curve.value = s.seam_in.audio_curve;
                const limit = crossfadeLimit(segs, i);
                rows.push(el("span", { text: S.crossfadeFrames(limit) }),
                    this.number(s.seam_in.frames, (v) => this.propose(this.withSegment(i, { seam_in: { ...seam(), frames: v } })), { min: "1", max: String(limit), "data-fk": "fade" }),
                    el("span", { text: S.audioCurve }), curve);
            }
        }
        const seed = el("input", { type: "number", min: "0", step: "1", class: "seed", "data-fk": "seed", placeholder: S.seedAuto, value: s.seed ?? "",
            onchange: (e) => this.propose(this.withSegment(i, { seed: e.target.value === "" ? null : Math.max(0, Math.floor(Number(e.target.value))) })) });
        rows.push(el("span", { text: S.seed }), el("div", { class: "mf-tl-chips" }, [seed,
            button(S.reroll, () => this.propose(this.withSegment(i, { seed: Math.floor(Math.random() * 2 ** 31) }))),
            el("span", { class: "hint", text: S.seedNote })]));
        return this.details("advanced", S.advanced, rows);
    }

    // What actually reaches the model for this segment — same rules as the Python compiler, shown once every material
    // it may use is verified. Problems are in segmentWarnings(), outside this section, so collapsing hides no warning.
    modelInputs(st, i) {
        const view = segmentInputs(this.data, i, this.ctx);
        if (!view?.ok) return [];
        const sec = this.secondsOf(st);
        const segs = this.data.segments;
        const s = segs[i];
        const m = view.manifest;
        const at = (frame) => sec(s.start + frame);
        const duration = (frames) => S.duration(sec(frames), frames);
        const line = (text, cls = "") => el("div", { class: `full ${cls}`, text });
        const rows = [];
        for (const p of m.pictures) rows.push(line(S.tagTable(p.tag ?? S.kinds.image, p.name)));
        for (const v of m.videos) {
            const use = v.effective_frames === v.requested_frames ? S.refUse(duration(v.effective_frames))
                : S.refCropped(duration(v.effective_frames), duration(v.requested_frames), duration(v.requested_frames - v.effective_frames));
            rows.push(line(`${S.tagTable(v.tag ?? S.kinds.video, v.name)} · ${use}`));
            if (v.audio_tag) rows.push(line(S.soundtrackOf(v.audio_tag, v.name)));
        }
        for (const a of m.audios) rows.push(line(S.tagTable(a.tag ?? S.kinds.audio, a.name)));
        for (const k of m.keyframes) rows.push(line(S.keyframeLine(k.name, at(k.frame_idx))));
        for (const g of m.guides) rows.push(line(S.guideLine(g.name, S.range(at(g.frame_idx), at(g.frame_idx + g.frames)))));
        for (const l of m.locks) rows.push(line(S.lockLine(l.name, S.range(at(l.frame_idx), at(l.frame_idx + l.frames)))));
        if (view.prompt.ok) rows.push(line(S.resolvedPrompt, "hint"), line(view.prompt.prompt));
        if (RAW_TAG.test(view.template)) {
            // offered only when every raw tag maps to exactly one material and the resolved prompt stays the same
            const convert = aliasRawTags(this.data, i, this.ctx).ok
                ? button(S.convertTags, () => this.apply(aliasRawTags(this.data, i, this.editContext()))) : null;
            rows.push(el("div", { class: "full warn" }, [S.rawTagHint, " ", convert]));
        }
        const overlap = i ? segs[i - 1].end - s.start : 0;
        const phase = st.h3 && overlap > 0 ? phaseMs(carrySpec(H3_GRID, segs[i - 1].end - segs[i - 1].start, overlap)) : null;
        if (st.h3 && (m.locks.length || overlap > 0)) {
            rows.push(line(S.techIntro, "hint"), line(phase ? S.phase(phase) : S.phaseZero, "hint"));
            for (const l of m.locks) if (l.tokens) rows.push(line(S.lockCoverage(l.name, l.tokens[0], l.tokens[1]), "hint"));
            if (m.locks.length) rows.push(...this.coverageMap(i, m), line(S.techOutro, "hint"));
        }
        const parts = ["pictures", "videos", "audios", "keyframes", "guides"].filter((k) => m[k].length).map((k) => S.inputCounts[k](m[k].length));
        const locked = m.locks.reduce((n, l) => n + l.frames, 0);
        if (locked) parts.push(S.inputLock(sec(locked)));
        const box = this.details("inputs", `${S.modelInputs}：${parts.join("、") || S.inputsNone}`, rows);
        box.classList.add("box");
        return [box];
    }

    // §8.1 lock coverage (H3, verified only): the segment's audio tokens [0, Ta) with the carried-in
    // head (red when a lock cancels the audio carry), each lock's master span and its fully locked tokens.
    coverageMap(i, m) {
        const segs = this.data.segments;
        const s = segs[i];
        const prev = segs[i - 1];
        const total = h3AudioTokens(s.end - s.start);
        const carried = prev ? carrySpec(H3_GRID, prev.end - prev.start, prev.end - s.start)?.audio_tokens ?? 0 : 0;
        const lost = m.carry_conflicts.length > 0;
        const pct = (t) => `${(100 * Math.max(0, Math.min(t, total))) / total}%`;
        const span = (cls, lo, hi) => el("div", { class: cls, style: { left: pct(lo), width: `calc(${pct(hi)} - ${pct(lo)})` } });
        const strip = el("div", { class: "mf-tl-cov full" });
        for (const l of m.locks) {
            strip.append(span("master", (5 * l.frame_idx) / 3, (5 * (l.frame_idx + l.frames)) / 3));
            if (l.tokens[1] > l.tokens[0]) strip.append(span("locked", l.tokens[0], l.tokens[1]));
        }
        const out = [el("div", { class: "full hint", text: `${S.coverage.title} [0, ${total})。${S.coverage.legend}` }), strip];
        if (carried) {
            strip.prepend(span(lost ? "lost" : "carry", 0, carried));
            out.push(el("div", { class: `full ${lost ? "warn" : "hint"}`, text: lost ? S.coverage.lost(carried) : S.coverage.carry(carried) }));
        }
        return out;
    }

    cutForm(st, i) {
        const sec = this.secondsOf(st);
        const segs = this.data.segments;
        const s = segs[i];
        const origin = this.data.source.origin_frame;
        const remove = button(S.deleteSegment, () => this.removeCutSegment(i));
        remove.disabled = segs.length <= 1;
        return [
            el("div", { class: "full" }, [el("b", { text: S.cutTitle(i + 1, sec(s.start - origin), sec(s.end - origin), sec(s.end - s.start)) })]),
            el("span"), el("div", { class: "mf-tl-chips" }, [
                button(S.markIn, () => this.markPoint("left")), button(S.markOut, () => this.markPoint("right")), remove,
            ]),
            el("span", { text: S.start }), this.number(s.start, (v) => this.editEdge(i, "left", v), { "data-fk": "start" }),
            el("span", { text: S.end }), this.number(s.end, (v) => this.editEdge(i, "right", v), { "data-fk": "end" }),
        ];
    }

    errorText(error) {
        const fields = Object.entries(error).filter(([k]) => k !== "code")
            .map(([k, v]) => (k === "entity" ? `${v.kind} ${v.id}` : `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)).join("，");
        return `${error.code}${fields ? "：" + fields : ""}`;
    }

    refForm(st, id) {
        const sec = this.secondsOf(st);
        const r = this.data.refs.find((x) => x.id === id);
        const m = this.data.materials.find((x) => x.id === r.material);
        const patch = (p) => this.apply(patchBar(this.data, "ref", id, p, this.editContext()));
        const range = S.range(sec(r.from), sec(r.to));
        const touched = emitIntervals(this.data.segments).flatMap(([lo, hi], i) => (r.from < hi && r.to > lo ? [i + 1] : []));
        const rows = [
            el("div", { class: "full" }, [el("b", { text: `${S.roles.ref}：${m?.name ?? "?"}` }), ` · ${S.kinds[m?.kind] ?? ""}`]),
            el("div", { class: "full hint", text: touched.length ? S.refSummary(touched, range) : S.refNoSegment(range) }),
        ];
        if (m?.kind === "video") {
            const mode = el("select", { "data-fk": "ref-mode", onchange: (e) => {
                const now = this.data.refs.find((x) => x.id === id);
                patch(e.target.value === "fixed" ? { mode: "fixed", length: Math.max(1, now.to - now.from) } : { mode: "follow", length: undefined });
            } }, [el("option", { value: "follow", text: S.modeFollow }), el("option", { value: "fixed", text: S.modeFixed })]);
            mode.value = r.mode;
            rows.push(el("span", { text: S.mode }), mode);
            const audio = el("input", { type: "checkbox", "data-fk": "ref-audio", onchange: (e) => patch({ audio: e.target.checked }) });
            audio.checked = !!r.audio;
            rows.push(el("span", { text: S.withAudioRef }), audio);
        }
        rows.push(el("span", { text: S.from }), this.number(r.from, (v) => patch({ from: v }), { "data-fk": "from" }),
            el("span", { text: S.to }), this.number(r.to, (v) => patch({ to: v }), { "data-fk": "to" }));
        if (r.in !== undefined) rows.push(el("span", { text: S.contentIn }), this.number(r.in, (v) => patch({ in: v }), { "data-fk": "in" }));
        if (r.length !== undefined) rows.push(el("span", { text: S.contentLength }), this.number(r.length, (v) => patch({ length: v }), { "data-fk": "len" }));
        rows.push(el("span"), el("div", { class: "mf-tl-chips" }, [
            button(S.split, () => this.splitSelected()), button(S.remove, () => this.removeSelected()),
            button(S.moveUp, () => this.apply(moveRefInOrder(this.data, id, -1, this.editContext()))),
            button(S.moveDown, () => this.apply(moveRefInOrder(this.data, id, 1, this.editContext()))),
            el("span", { class: "hint", text: S.orderHint }),
        ]));
        return rows;
    }

    anchorForm(st, id) {
        const sec = this.secondsOf(st);
        const a = this.data.anchors.find((x) => x.id === id);
        const m = this.data.materials.find((x) => x.id === a.material);
        const patch = (p) => this.apply(patchBar(this.data, "anchor", id, p, this.editContext()));
        const end = a.role === "keyframe" ? a.at : Math.min(a.at + a.length, this.data.segments.at(-1).end);
        const used = Math.max(0, end - a.at);
        const range = S.range(sec(a.at), sec(end));
        const summary = a.role === "keyframe" ? S.keyframeSummary(sec(a.at))
            : a.role === "guide" ? S.guideSummary(range) : S.lockSummary(range, sec(used));
        const rows = [
            el("div", { class: "full" }, [el("b", { text: `${S.roles[a.role]}：${m?.name ?? "?"}` })]),
            el("div", { class: "full hint", text: summary }),
        ];
        if (a.role === "lock") {
            const total = this.snapshot(a.material)?.frames;
            if (total != null && used < total) rows.push(el("div", { class: "full warn", text: S.lockTrimmed(sec(total), sec(used)) }));
            rows.push(el("div", { class: "full hint", text: S.lockCondition }));
        }
        rows.push(el("span", { text: S.at }), this.number(a.at, (v) => patch({ at: v }), { "data-fk": "at" }));
        if (a.role !== "keyframe") {
            rows.push(el("span", { text: S.contentIn }), this.number(a.in, (v) => patch({ in: v }), { "data-fk": "in" }),
                el("span", { text: S.contentLength }), this.number(a.length, (v) => patch({ length: v }), { "data-fk": "len" }));
        }
        if (a.role === "guide") {
            const audio = el("input", { type: "checkbox", "data-fk": "guide-audio", onchange: (e) => patch({ audio: e.target.checked }) });
            audio.checked = !!a.audio;
            rows.push(el("span", { text: S.withAudioGuide }), audio);
        }
        // a keyframe has no length, an H3 guide cannot be split losslessly: no button that can only refuse
        const canSplit = a.role === "lock" || (a.role === "guide" && !st.h3);
        rows.push(el("span"), el("div", { class: "mf-tl-chips" }, [
            canSplit ? button(S.split, () => this.splitSelected()) : null, button(S.remove, () => this.removeSelected()),
        ]));
        return rows;
    }

    materialForm(st, id) {
        const sec = this.secondsOf(st);
        const m = this.data.materials.find((x) => x.id === id);
        const entry = this.media.get(id);
        const info = entry?.info;
        const describe = !info ? (entry?.error ?? S.status[entry?.status ?? "probing"])
            : m.kind === "image" ? `${info.width}×${info.height}`
                : `${S.seconds((info.duration_us ?? 0) / 1e6)}${m.kind === "video" ? ` · ${info.display_size?.join("×")}${info.has_audio ? ` · ${S.hasAudio}` : ""}` : ` · ${info.audio_sample_rate} Hz`}`;
        const i = Math.min(this.view.active, this.data.segments.length - 1);
        const place = (lane) => () => this.apply(createFromCard(this.data, id, lane, this.view.playhead, this.editContext()));
        const uses = [];
        if (st.h3) {
            const item = tagButtons(this.data, i, this.ctx).items?.[id];
            const use = button(item?.state === "insert" ? S.insertTag(m.name, i + 1) : S.useInSegment(i + 1),
                () => this.apply(useRefTag(this.data, i, id, this.editContext())),
                { title: item?.state === "blocked" ? this.refusalText(item) : S.useInSegmentTitle, "data-fk": "use" });
            use.disabled = !item || item.state === "blocked";
            uses.push(use);
        } else {
            uses.push(button(S.addRefPlain(i + 1), () => this.apply(createFromCard(this.data, id, "refs", emitIntervals(this.data.segments)[i][0], this.editContext())), { "data-fk": "use" }));
        }
        const lane = m.kind === "audio" ? "audio" : "visual";
        this.placeButton = { element: button("", place(lane), { "data-fk": "use-lane" }), id, lane };
        this.syncPlaceButton();
        uses.push(this.placeButton.element);
        return [
            el("div", { class: "full" }, [el("b", { text: `${KIND_ICON[m.kind]} ${S.kinds[m.kind]}：${m.name}` }), ` · ${describe}`]),
            el("span", { text: S.rename }),
            el("input", { type: "text", "data-fk": "name", value: m.name, onchange: (e) => this.apply(renameMaterial(this.data, id, e.target.value.normalize("NFC"), this.editContext())) }),
            el("span", { text: S.materialRef }),
            el("input", { type: "text", "data-fk": "file", value: m.ref, onchange: (e) => {
                // a bad path would make the saved JSON fail strict parsing on the next load
                if (!refOk(e.target.value)) {
                    this.notice = S.badRef;
                    return this.render();
                }
                this.propose({ ...this.data, materials: this.data.materials.map((x) => (x.id === id ? { ...x, ref: e.target.value } : x)) });
            } }),
            el("span", { text: S.upTo }), el("div", { class: "mf-tl-chips" }, uses),
            el("span"), el("div", { class: "mf-tl-chips" }, [button(S.removeMaterial, () => {
                const r = deleteMaterial(this.data, id, this.editContext());
                if (r.ok) this.select(null);
                this.apply(r);
            })]),
        ];
    }

    // A button naming a time must name where the click really lands: createFromCard clips to the timeline and moves a
    // guide back inside its segment, and the playhead or a probe can change that without the form being rebuilt. So it
    // is worked out from the same command on every track repaint, and only this one button changes.
    syncPlaceButton() {
        const place = this.placeButton;
        if (!place) return;
        const r = createFromCard(this.data, place.id, place.lane, this.view.playhead, this.editContext());
        const at = r.ok ? r.next.anchors[r.next.anchors.length - 1].at : Math.min(this.view.playhead, this.data.segments.at(-1).end - 1);
        const seconds = this.secondsOf(this.settings())(at);
        place.element.textContent = place.lane === "audio" ? S.addLock(seconds) : S.addVisual(seconds);
        place.element.disabled = !r.ok;
        place.element.title = r.ok ? "" : this.refusalText(r);
    }

    // Green only after the full check (structure, segments, geometry, every manifest and prompt)
    // passed with every material and the source verified; otherwise the first problem, with its
    // segment, and a click on the line selects where it is. This is a check of the timeline: it says nothing about
    // the rest of the workflow, the models or the wiring.
    renderStatus(st) {
        const r = this.result;
        const [first] = this.check.list;
        const failed = this.data.materials.find((m) => this.media.get(m.id)?.status === "error");
        // until the source is verified its length is unknown, so cut ranges went unchecked
        const source = st.hasSource && !this.source
            ? S.sourceUnverified(this.sourceError ?? S.sourceProbing) + (st.mode === "cut" ? `：${S.sourceRangeUnknown}` : "") : null;
        const summary = () => {
            const sec = this.secondsOf(st);
            return `${S.segmentCount(this.data.segments.length)} · ${S.total(r.totalFrames, sec(r.totalFrames))}`;
        };
        let className = "bad";
        let text;
        let problem = null;
        this.statusTarget = null;
        if (first && (first.stage === "schema" || first.stage === "grid" || !failed)) {
            problem = first;
            text = this.problemText(first);
            this.statusTarget = this.problemTarget(first);
        } else if (failed) {
            text = `⚠ ${S.materialFailed(failed.name, this.media.get(failed.id).error)}`;
            this.statusTarget = { type: "material", id: failed.id };
        } else if (source) {
            className = this.sourceError ? "bad" : "warn";
            text = `${source} · ${summary()}`;
        } else {
            className = this.check.verified ? "good" : "warn";
            text = `${this.check.verified ? S.valid : S.unverified} · ${summary()}`;
        }
        if (className === "bad" && !text.startsWith("⚠")) text = `⚠ ${text}`;
        if (this.sourceError && source && !text.includes(source)) text += ` · ${source}`;
        this.statusBase = { className: `mf-tl-status ${className}${this.statusTarget ? " locate" : ""}`, text };
        this.statusText.title = this.statusTarget ? S.locate : "";
        this.renderFixes(problem);
        const detail = problem ? this.problemDetail(problem) : "";
        this.statusDetail.style.display = detail ? "" : "none";
        this.statusDetailText.textContent = detail;
        this.noticeLine.textContent = [this.notice, className === "good" ? this.nextStep(st) : ""].filter(Boolean).join(" · ");
        this.gestureStatus();
    }

    // The few problems with one obvious way out get a button. Rebuilt only when the set changes: a probe finishing
    // must not replace the button under the pointer.
    renderFixes(problem) {
        const reason = problem?.stage === "grid" ? problem.error.reason : null;
        const fixes = {
            cut_no_materials: { removeMaterials: () => this.clearMaterials(), backToGenerate: () => this.setWidget("mode", "generate") },
            h3_fps: { setFps24: () => this.fixFps() },
            illegal_length: { conform: () => this.fixLengths() },
            illegal_overlap: { conform: () => this.fixLengths() },
            needs_source: { uploadSource: () => this.fileInput.click() },
        }[reason] ?? {};
        const key = Object.keys(fixes).join();
        if (key === this.fixKey) return;
        this.fixKey = key;
        this.statusFixes.replaceChildren(...Object.entries(fixes).map(([name, run]) => button(S.fixes[name], run)));
    }

    clearMaterials() {
        const count = this.data.materials.length;
        this.apply(removeAllMaterials(this.data, this.editContext()), S.removedMaterials(count));
    }

    fixFps() {
        const total = () => this.secondsOf(this.settings())(this.data.segments.at(-1).end);
        const before = total();
        this.setWidget("fps", "24");
        this.notice = S.fpsChanged(before, total());
        this.render();
    }

    fixLengths() {
        const r = conformSegments(this.data, this.editContext());
        if (!r.ok) return this.apply(r);
        const sec = this.secondsOf(this.settings());
        const notes = [S.conformed(sec(this.data.segments.at(-1).end), sec(r.next.segments.at(-1).end))];
        if (r.fades.length) notes.push(S.fadeShortened(r.fades.map((i) => i + 1)));
        this.apply(r, notes.join(" · "));
    }

    // Only looks at this node's own outputs; whether the rest of the workflow is complete is not for this node to judge.
    nextStep(st) {
        const linked = (name) => (this.node.outputs?.find((o) => o.name === name)?.links?.length ?? 0) > 0;
        if (linked("segment_plan") || linked("ranges")) return "";
        if (st.mode === "cut") return S.next.cut;
        return st.h3 ? S.next.h3 : S.next.loop;
    }

    // during a gesture the status line says why the current position cannot be dropped / committed
    gestureStatus() {
        const refused = this.gesture?.refused;
        this.status.className = refused ? "mf-tl-status bad" : this.statusBase.className;
        this.statusText.textContent = refused ? `✕ ${S.cannotDrop}：${refused}` : this.statusBase.text;
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
        this.propose({ ...this.data, segments: r.segments }, landed === frame ? "" : S.snapNote);
    }

    addSegment() {
        const st = this.settings();
        const segs = this.data.segments;
        const last = segs[segs.length - 1];
        const length = last.end - last.start;
        if (st.mode === "cut") {
            const r = insertCut(this.data, this.view.playhead, length, this.editContext());
            if (!this.apply(r)) return;
            this.selectSegment(r.index);
            return this.render();
        }
        const prev = segs[segs.length - 2];
        const overlap = overlapAtMost(st.grid, Math.min(prev ? prev.end - last.start : DEFAULT_OVERLAP[st.grid] ?? 0, length - 1));
        const added = { start: last.end - overlap, end: last.end - overlap + length, prompt: "", seam_in: { kind: "cut" }, seed: null };
        if (!this.propose({ ...this.data, segments: [...segs, added] })) return;
        this.selectSegment(segs.length);
        this.render();
    }

    deleteLast() {
        const segs = this.data.segments;
        if (segs.length <= 1) return;
        if (!this.propose({ ...this.data, segments: segs.slice(0, -1) })) return;
        this.select({ type: "segment", index: Math.min(this.view.active, segs.length - 2) });
        this.render();
    }

    markPoint(edge) {
        const st = this.settings();
        const sec = this.secondsOf(st);
        const i = Math.min(this.view.active, this.data.segments.length - 1);
        const r = markCut(this.data, i, edge, this.view.playhead, this.editContext());
        if (!r.ok) return this.apply(r);
        const s = r.next.segments[i];
        this.apply(r, r.shortened ? S.movedShortened(sec(s.end - s.start)) : r.moved ? S.movedWhole : "");
    }

    removeCutSegment(index = Math.min(this.view.active, this.data.segments.length - 1)) {
        if (!this.apply(deleteCut(this.data, index, this.editContext()))) return;
        this.selectSegment(Math.min(index, this.data.segments.length - 1));
        this.render();
    }

    splitSelected() {
        const sel = this.view.sel;
        if (sel?.type !== "ref" && sel?.type !== "anchor") return;
        this.apply(splitBar(this.data, sel.type, sel.id, this.view.playhead, this.editContext()));
    }

    removeSelected() {
        const sel = this.view.sel;
        if (sel?.type !== "ref" && sel?.type !== "anchor") return;
        const r = deleteBar(this.data, sel.type, sel.id, this.editContext());
        if (r.ok) this.select(null);
        this.apply(r);
    }

    togglePanel(kind) {
        const close = () => {
            this.panelKind = null;
            this.panel.replaceChildren();
        };
        if (this.panelKind === kind) return close();
        this.panelKind = kind;
        if (kind === "fill") this.fillPanel(close);
        else if (kind === "import") this.importPanel(close);
        else this.panel.replaceChildren(el("div", { class: "mf-tl-panel" }, [
            ...S.helpLines.map((text) => el("div", { text })), button(S.close, close),
        ]));
    }

    // A dropdown of values that work, with a way out for exact numbers. Free grids have no list to offer.
    valuePicker(options, initial, onChange) {
        const input = el("input", { type: "number", min: "0", step: "1", value: String(initial) });
        input.addEventListener("input", onChange);
        if (!options.length) return { nodes: [input, el("span", { text: S.framesUnit })], value: () => Number(input.value) };
        const select = el("select", {}, [...options.map((o) => el("option", { value: String(o.value), text: o.text })),
            el("option", { value: "custom", text: S.customValue })]);
        select.value = options.some((o) => o.value === initial) ? String(initial) : "custom";
        const sync = () => {
            input.style.display = select.value === "custom" ? "" : "none";
        };
        select.addEventListener("change", () => {
            sync();
            onChange();
        });
        sync();
        return { nodes: [select, input], value: () => Number(select.value === "custom" ? input.value : select.value) };
    }

    fillPanel(close) {
        const st = this.settings();
        const sec = this.secondsOf(st);
        const perSecond = st.fps[0] / st.fps[1];
        const segs = this.data.segments;
        const first = segs[0].end - segs[0].start;
        const lengths = commonLengths(st.grid, perSecond, first);
        // after a grid change the current value may be illegal: start from the nearest one that works
        const startLength = lengthOk(st.grid, first) || !lengths.length ? first
            : lengths.reduce((best, f) => (Math.abs(f - first) < Math.abs(best - first) ? f : best));
        const firstOverlap = segs.length > 1 ? segs[0].end - segs[1].start : DEFAULT_OVERLAP[st.grid] ?? 0;
        const startOverlap = overlapOk(st.grid, firstOverlap) ? firstOverlap : DEFAULT_OVERLAP[st.grid] ?? 0;
        const count = el("input", { type: "number", value: String(segs.length > 1 ? segs.length : 3), min: "1", max: "256" });
        const preview = el("div", { class: "mf-tl-label" });
        const plan = () => refillSegments(this.data, Math.floor(Number(count.value)), length.value(), overlap.value(), this.editContext());
        const apply = button(S.apply, () => {
            const r = plan();
            if (!r.ok) return;
            this.apply(r);
            close();
            this.select({ type: "segment", index: 0 });
            this.render();
        });
        const update = () => {
            const r = plan();
            apply.disabled = !r.ok;
            preview.className = `mf-tl-label${r.ok ? "" : " bad"}`;
            if (!r.ok) {
                preview.textContent = this.refusalText(r);
                return;
            }
            const ctx = this.editContext();
            const end = r.next.segments.at(-1).end;
            const parts = [S.fillSummary(r.next.segments.length, sec(end), end)];
            if (r.removed.length) parts.push(S.fillRemoves(r.removed[0] + 1, r.removed.at(-1) + 1));
            if (r.seamReset.length) parts.push(S.fillSeamReset(r.seamReset.map((i) => i + 1)));
            parts.push(...refChanges(this.data, r.next, ctx).map((c) => this.refChangeText(c)));
            const [problem] = timelineProblems(r.next, ctx).list;
            if (problem) parts.push(S.fillAfter(this.problemText(problem)));
            preview.textContent = parts.join("；");
        };
        const length = this.valuePicker(lengths.map((f) => ({ value: f, text: S.duration(sec(f), f) })), startLength, update);
        const overlap = this.valuePicker(commonOverlaps(st.grid, perSecond).map((f) => ({ value: f, text: f ? S.duration(sec(f), f) : S.noOverlap })), startOverlap, update);
        count.addEventListener("input", update);
        update();
        this.panel.replaceChildren(el("div", { class: "mf-tl-panel" }, [
            el("span", { text: S.segments }), count, el("span", { text: S.length }), ...length.nodes, el("span", { text: S.overlap }), ...overlap.nodes,
            apply, button(S.cancel, close), preview,
        ]));
    }

    importPanel(close) {
        const text = el("textarea", { placeholder: S.importHint });
        this.panel.replaceChildren(el("div", { class: "mf-tl-panel" }, [
            text,
            button(S.apply, () => {
                const segs = this.scenesToSegments(text.value);
                if (!segs) {
                    this.notice = S.importFailed;
                    return this.render();
                }
                if (!this.propose({ ...this.data, segments: segs })) return;
                close();
                this.select({ type: "segment", index: 0 });
                this.render();
            }),
            button(S.cancel, close),
        ]));
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

    // 上傳影片: only the latest pick lands, and only while its source is still the one it replaces
    // (undo / configure / another choice all reload the source and bump `revision`), the editor
    // lives (removing the node or loading another workflow destroys it) and the same workflow is
    // active — a late answer never overwrites a restored value or writes another workflow's undo.
    async upload(file) {
        const lease = { rev: this.revision, tracker: changeTracker() };
        this.sourceUpload = lease;
        const current = () => !this.destroyed && this.sourceUpload === lease && lease.rev === this.revision && lease.tracker === changeTracker();
        this.previewLabel.textContent = S.uploading;
        let value;
        try {
            value = await this.uploadFile(file);
        } catch (e) {
            if (current()) this.previewLabel.textContent = `${S.uploadFailed}（${e.message}）`;
            return;
        } finally {
            this.fileInput.value = "";
        }
        if (!current()) {
            // a newer pick owns the label and lands by itself; otherwise say why nothing changed
            if (!this.destroyed && this.sourceUpload === lease) {
                this.notice = S.sourceUploadDropped;
                this.render();
            }
            return;
        }
        const values = this.w.video.options?.values;
        if (Array.isArray(values) && !values.includes(value)) values.push(value);
        lease.tracker?.beforeChange?.();
        try {
            this.w.video.value = value;
            this.w.video.callback?.(value);
        } finally {
            lease.tracker?.afterChange?.();
        }
    }

    // §8.3: every batch (picker or OS drop) goes through one editor-wide queue with at most
    // UPLOAD_CONCURRENCY uploads in flight across batches. A batch lands as one commit / one undo,
    // merged into the timeline as it is when the batch finishes — after the current gesture, never
    // after a hydrate (undo / reload: the batch is dropped) or destroy. Failed items stay as cards.
    uploadMaterials(files) {
        this.materialInput.value = "";
        if (this.broken || this.destroyed) return;
        if (this.settings().mode !== "generate") {
            if (files.length) {
                this.notice = S.cutNoDrop;
                this.render();
            }
            return;
        }
        const items = files.map((file) => ({ file, kind: kindOf(file) }));
        const unsupported = items.filter((p) => !p.kind).map((p) => p.file.name);
        const accepted = items.filter((p) => p.kind);
        const room = MAX_MATERIALS - this.data.materials.length - this.uploads.filter((u) => u.status !== "failed").length;
        const over = accepted.splice(Math.max(0, room)).map((p) => p.file.name);
        this.notice = [unsupported.length ? S.unsupportedNames(unsupported) : "", over.length ? S.libraryFull(MAX_MATERIALS, over) : ""]
            .filter(Boolean).join("；");
        if (accepted.length) {
            const batch = { gen: this.loadGen, left: accepted.length };
            this.uploads.push(...accepted.map((p) => ({ ...p, batch, status: "queued", ref: null, error: null })));
            this.pumpUploads();
        }
        this.render();
    }

    pumpUploads() {
        while (!this.destroyed && this.uploading < UPLOAD_CONCURRENCY) {
            const u = this.uploads.find((x) => x.status === "queued");
            if (!u) return;
            u.status = "uploading";
            this.uploading++;
            this.uploadFile(u.file)
                .then((ref) => Object.assign(u, { status: "done", ref }),
                    (e) => Object.assign(u, { status: "failed", error: `${S.uploadFailed}（${e.message}）` }))
                .finally(() => this.settleUpload(u));
        }
    }

    settleUpload(u) {
        this.uploading--;
        this.pumpUploads();
        if (this.destroyed) return;
        if (u.batch.gen !== this.loadGen) {
            // the timeline was replaced meanwhile (hydrate already emptied the queue)
            this.notice = S.uploadDropped;
            return this.render();
        }
        if (--u.batch.left) return this.render();
        this.finishBatch(u.batch);
    }

    finishBatch(batch) {
        if (this.destroyed || batch.gen !== this.loadGen) return;
        if (this.gesture) {
            this.deferredBatches.push(batch);  // a commit now would end the drag as stale
            return this.render();
        }
        const items = this.uploads.filter((u) => u.batch === batch);
        const done = items.filter((u) => u.status === "done");
        const failed = items.filter((u) => u.status === "failed").map((u) => u.file.name);
        const note = failed.length ? S.uploadFailedNames(failed) : "";
        if (!done.length) {
            this.notice = note;
            return this.render();
        }
        const r = addMaterials(this.data, done.map((u) => ({ kind: u.kind, ref: u.ref, filename: u.file.name })), this.editContext());
        if (r.ok) this.uploads = this.uploads.filter((u) => !done.includes(u));
        else for (const u of done) Object.assign(u, { status: "failed", error: this.refusalText(r) });
        this.apply(r, note);
    }

    flushBatches() {
        if (!this.gesture) for (const batch of this.deferredBatches.splice(0)) this.finishBatch(batch);
    }

    retryUpload(u) {
        Object.assign(u, { status: "queued", error: null, batch: { gen: this.loadGen, left: 1 } });
        this.pumpUploads();
        this.render();
    }

    dropUpload(u) {
        this.uploads = this.uploads.filter((x) => x !== u);
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

    // ---------- gestures ----------

    // One pointer gesture at a time: a segment edge, a bar or a material card. Whatever ends it —
    // pointerup, Escape, pointercancel, lost capture, window blur, hydrate, destroy — goes through
    // endGesture(), which removes its listeners, ghost and capture. Only pointerup commits, and only
    // while the data generation is still the one the gesture started from (§8.2 pointer transaction).
    beginGesture(g, target, e, listeners = {}) {
        const cancel = () => this.cancelGesture();
        const key = (ev) => {
            if (ev.key !== "Escape") return;
            ev.preventDefault();
            ev.stopPropagation();
            cancel();
        };
        const bound = [[window, "keydown", key, true], [window, "blur", cancel, false],
            ...Object.entries(listeners).map(([type, fn]) => [target, type, fn, false])];
        for (const [t, type, fn, capture] of bound) t.addEventListener(type, fn, capture);
        Object.assign(g, {
            target, pointerId: e.pointerId, gen: this.editGen, ctx: this.editContext(), draft: null, refused: null,
            unbind: () => bound.forEach(([t, type, fn, capture]) => t.removeEventListener(type, fn, capture)),
        });
        this.gesture = g;
        try {
            target.setPointerCapture(e.pointerId);
        } catch {
            // the pointer may already be gone (very fast click) or be synthetic; the gesture works without capture
        }
    }

    endGesture() {
        const g = this.gesture;
        if (!g) return null;
        this.gesture = null;  // first: the lostpointercapture released below must find nothing to cancel
        g.unbind();
        g.ghost?.remove();
        if (g.target.hasPointerCapture?.(g.pointerId)) g.target.releasePointerCapture(g.pointerId);
        this.dropLane = null;
        // upload batches that finished meanwhile land after whatever this gesture commits
        if (this.deferredBatches.length) queueMicrotask(() => this.flushBatches());
        return g;
    }

    cancelGesture() {
        if (this.endGesture()) this.render();
    }

    startCardDrag(e, m, card) {
        if (this.broken || e.button !== 0) return;
        e.stopPropagation();
        // a new press means the old pointer is gone (its pointerup missed us): discard that gesture
        this.endGesture();
        // focus first, as for bars: a pending inspector change commits here, before the gesture records
        // its generation. That commit rebuilds the library, so the material and its card are looked up again.
        const id = m.id;
        this.root.focus({ preventScroll: true });
        m = this.data.materials.find((x) => x.id === id);
        card = [...this.lib.children].find((c) => c.dataset.id === id);
        if (!m || !card) return;
        const label = `${KIND_ICON[m.kind]} ${m.name}`;
        const ghost = el("div", { class: "mf-tl-ghost", text: label, style: { left: `${e.clientX}px`, top: `${e.clientY}px` } });
        document.body.append(ghost);
        const g = { type: "card", id: m.id, ghost, label, startX: e.clientX, startY: e.clientY, moved: false };
        const mine = (ev) => this.gesture === g && ev.pointerId === g.pointerId;
        this.beginGesture(g, card, e, {
            pointermove: (ev) => mine(ev) && this.onCardMove(g, ev),
            pointerup: (ev) => mine(ev) && this.onCardUp(g, ev),
            pointercancel: (ev) => mine(ev) && this.cancelGesture(),
            lostpointercapture: (ev) => mine(ev) && this.cancelGesture(),
        });
    }

    cardLane(g, ev) {
        return g.moved ? this.laneAt(ev.clientX, ev.clientY) : null;
    }

    // §8.2: a spot where the card cannot go shows 不可放置 with the reason and is never committed
    onCardMove(g, ev) {
        const wasMoved = g.moved;
        g.moved ||= Math.hypot(ev.clientX - g.startX, ev.clientY - g.startY) > 4;
        Object.assign(g.ghost.style, { left: `${ev.clientX}px`, top: `${ev.clientY}px` });
        const lane = this.cardLane(g, ev);
        const r = lane ? createFromCard(this.data, g.id, lane.lane, this.frameAt(ev.clientX), g.ctx) : null;
        const refused = r && !r.ok ? this.refusalText(r) : null;
        g.ghost.textContent = refused ? `✕ ${S.cannotDrop}` : g.label;
        if (wasMoved === g.moved && lane?.lane === this.dropLane?.lane && lane?.row === this.dropLane?.row && refused === g.refused) return;
        this.dropLane = lane;
        g.refused = refused;
        this.renderTrack();
        this.gestureStatus();
    }

    onCardUp(g, ev) {
        const lane = this.cardLane(g, ev);
        this.endGesture();
        // a press without movement is a click; selecting here (not in `click`) also works when the press
        // committed a pending inspector edit and rebuilt the library under the pointer
        if (!g.moved) this.select({ type: "material", id: g.id });
        if (!lane) return this.render();
        if (g.gen !== this.editGen) {
            this.notice = S.dragStale;
            return this.render();
        }
        this.apply(createFromCard(this.data, g.id, lane.lane, this.frameAt(ev.clientX), this.editContext()));
    }

    onPointerDown(e) {
        if (this.broken || e.button !== 0) return;
        e.stopPropagation();
        this.endGesture();  // a press with a gesture still open: its pointerup never reached us; discard it
        // focus first: a pending inspector change commits here, before the gesture records its generation
        this.root.focus({ preventScroll: true });
        this.notice = "";
        const item = e.target.closest(".mf-tl-item");
        const segEl = e.target.closest(".mf-tl-seg");
        const handle = e.target.closest(".mf-tl-handle");
        if (item) {
            const kind = item.dataset.kind;
            const id = item.dataset.id;
            const source = (kind === "ref" ? this.data.refs : this.data.anchors).find((x) => x.id === id);
            this.select({ type: kind, id });
            this.render();
            if (!source) return;
            const op = handle ? handle.dataset.edge : "move";
            const [lo, hi] = barSpan(kind, source);
            e.preventDefault();
            this.beginGesture({ type: "bar", kind, id, op, origin: this.frameAt(e.clientX), edge: op === "right" ? hi : lo }, this.track, e);
        } else if (segEl) {
            const index = Number(segEl.dataset.index);
            this.selectSegment(index);
            this.render();
            if (handle) {
                e.preventDefault();
                this.beginGesture({ type: "segment", index, edge: handle.dataset.edge }, this.track, e);
            }
        } else {
            // the ruler and empty track: press to jump, keep holding to scrub
            this.seek(this.frameAt(e.clientX));
            e.preventDefault();
            this.beginGesture({ type: "scrub" }, this.track, e);
            this.render();
        }
    }

    onPointerMove(e) {
        const g = this.gesture;
        if (!g || g.target !== this.track || e.pointerId !== g.pointerId) return;
        if (g.type === "scrub") return this.seek(this.frameAt(e.clientX));
        let r;
        if (g.type === "segment") {
            const edit = this.applyEdit(this.data.segments, g.index, g.edge, this.frameAt(e.clientX));
            const next = edit.ok ? { ...this.data, segments: edit.segments } : null;
            const problem = next && checkEdit(this.data, next, g.ctx);
            r = !edit.ok ? edit : problem ? { ok: false, reason: "breaks", problem } : { ok: true, next };
        } else {
            // §8.2 snapping: the best legal target within SNAP_PX screen pixels, else the raw position
            const reach = SNAP_PX / (this.px * this.trackScale().scale);
            r = pickSnap(snapTargets(this.data, this.view.playhead, `${g.kind}:${g.id}`), g.edge + this.frameAt(e.clientX) - g.origin,
                reach, (f) => editBar(this.data, g.kind, g.id, g.op, f - g.edge, g.ctx));
        }
        // an illegal position shows the refusal and keeps no draft, so pointerup commits nothing
        g.draft = r.ok ? r.next : null;
        g.refused = r.ok ? null : this.refusalText(r);
        this.scheduleTrack();
        this.gestureStatus();
    }

    onPointerUp(e) {
        const g = this.gesture;
        if (!g || g.target !== this.track || e.pointerId !== g.pointerId) return;
        this.endGesture();
        if (g.type === "scrub") return this.render();
        if (g.gen !== this.editGen) {
            this.notice = S.dragStale;
            return this.render();
        }
        if (g.draft) return this.commitWithImpact(g.draft, []);
        if (g.refused) this.notice = g.refused;
        this.render();
    }

    onKey(e) {
        // never steal keys from text fields or an IME composition; during a gesture only Escape
        // (handled by the gesture's own window listener) does anything
        if (this.broken || this.gesture || e.isComposing || e.target.closest("input, textarea, select, [contenteditable]")) return;
        // a focused button or section header activates itself with Space
        if (e.key === " " && e.target.closest("button, summary")) return;
        const sel = this.view.sel;
        const bar = sel?.type === "ref" || sel?.type === "anchor";
        // Ctrl+S is the workflow's save, Ctrl+O its open: never a timeline edit
        const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
        const range = this.settings().mode === "cut" && sel?.type !== "ref" && sel?.type !== "anchor";
        const remove = e.key === "Delete" || e.key === "Backspace";
        let handled = true;
        if (e.key === " ") this.togglePlay();
        else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && e.altKey && bar) {
            const step = (e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 10 : 1);
            this.apply(editBar(this.data, sel.type, sel.id, "move", step, this.editContext()));
        } else if (e.key === "ArrowLeft") this.seek(this.view.playhead - (e.shiftKey ? 10 : 1));
        else if (e.key === "ArrowRight") this.seek(this.view.playhead + (e.shiftKey ? 10 : 1));
        else if ((e.key === "s" || e.key === "S") && bar && plain) this.splitSelected();
        else if (remove && bar) this.removeSelected();
        else if (remove && range && plain && this.data.segments.length > 1) this.removeCutSegment();
        else if ((e.key === "i" || e.key === "I") && range && plain) this.markPoint("left");
        else if ((e.key === "o" || e.key === "O") && range && plain) this.markPoint("right");
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
        if (this.video.getAttribute("src")) {
            this.video.currentTime = Math.max(0, ((this.view.playhead - this.data.source.origin_frame) * fps[1]) / fps[0]);
        }
        this.renderTrack();
    }

    togglePlay() {
        const video = this.previewKey ? this.materialVideo : this.video;
        if (!video.getAttribute("src")) return;
        if (video.paused) video.play().catch(() => {});
        else video.pause();
        this.syncPlayButton();
    }

    // Play what the preview shows; nothing to play is a disabled button with a reason, not a silent click.
    syncPlayButton() {
        const video = this.previewKey ? this.materialVideo : this.video;
        const playable = !!video.getAttribute("src");
        this.playButton.disabled = !playable;
        this.playButton.title = playable ? "" : S.noPlayable;
        this.playButton.textContent = video.paused === false ? S.pause : this.previewKey ? S.playMaterial : S.playSource;
    }

    // §8.1: a selected material previews in the preview slot (image, or video / audio with
    // controls); any other selection shows the source again, which kept its own state meanwhile.
    syncPreview(st = this.settings()) {
        const sel = this.view.sel;
        const m = st.mode === "generate" && sel?.type === "material" ? this.data.materials.find((x) => x.id === sel.id) : null;
        const hasSource = !!this.video.getAttribute("src");
        const show = (e, on) => {
            e.style.display = on ? "" : "none";
        };
        this.previewEmpty.textContent = st.mode === "cut" ? S.noSourceCut : S.noSource;
        show(this.previewEmpty, !m && !hasSource);
        show(this.video, !m && hasSource);
        show(this.previewLabel, !m);
        show(this.materialLabel, !!m);
        show(this.materialImage, m?.kind === "image");
        show(this.materialVideo, !!m && m.kind !== "image");
        const key = m ? `${m.kind}:${m.ref}:${m.name}` : null;
        if (key === this.previewKey) return this.syncPlayButton();
        this.previewKey = key;
        this.video.pause();
        this.materialVideo.pause();
        if (!m || m.kind === "image") {
            this.materialVideo.removeAttribute("src");
            this.materialVideo.load();
        }
        if (!m) {
            this.materialImage.removeAttribute("src");
        } else {
            const url = viewURL({ ...splitSource(m.ref), type: "input" });
            this.materialLabel.textContent = S.materialPreview(S.kinds[m.kind], m.name);
            if (m.kind === "image") this.materialImage.src = url;
            else if (this.materialVideo.getAttribute("src") !== url) this.materialVideo.src = url;
        }
        this.syncPlayButton();
    }

    onVideoTime() {
        if (this.video.paused || this.gesture) return;
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
            this.previewLabel.textContent = S.sourcePreview;
            return this.render();
        }
        this.sourceRef = splitSource(value);
        this.video.src = viewURL({ ...this.sourceRef, type: "input" });
        this.previewLabel.textContent = S.sourcePreview;
        this.request("/mediaforge/timeline/probe", { version: 1, source: { type: "input", ...this.sourceRef } })
            .then(({ ok, body }) => {
                if (rev !== this.revision) return;  // the source was switched / reloaded meanwhile
                // the same v2 check as materials: version, kind, a known length, the fingerprint the proxy needs
                const issue = ok ? probeIssue(body, "video") : null;
                this.source = ok && !issue ? body : null;
                this.sourceError = !ok ? body?.error?.message ?? S.sourceProbeFailed : issue ? S.probeIssues[issue] : null;
                this.render();
                // the <video> may have failed before the probe answered; the proxy needs its fingerprint
                if (this.source && this.proxyWanted) this.makeProxy();
            })
            .catch((e) => {
                if (e.name !== "AbortError" && rev === this.revision) {
                    this.sourceError = S.sourceProbeFailed;
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
        this.endGesture();
        this.endPress();
        clearTimeout(this.fieldTimer);
        for (const video of [this.video, this.materialVideo]) {
            video.pause();
            video.removeAttribute("src");
            video.load();
        }
        // graph.configure() clears the old nodes (graph id still the old one) and builds the new ones in the same
        // task; only a reload of the same graph gets the view back
        const id = this.node.id;
        const entry = { graphId: this.node.graph?.id, view: { ...this.view } };
        HANDOVER.set(id, entry);
        setTimeout(() => HANDOVER.get(id) === entry && HANDOVER.delete(id), 0);
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
                const handover = HANDOVER.get(this.id);
                this.mfTimeline.view = { ...freshView(), ...(handover && handover.graphId === this.graph?.id ? handover.view : {}) };
                this.mfTimeline.form.replaceChildren();  // sections built before this view must not write over it (keepSections)
                this.mfTimeline.applyWidgetLabels();  // configure may restore the inputs' saved labels
                this.mfTimeline.adoptMount();
                this.mfTimeline.hydrate();
                this.mfTimeline.loadSource();
            } catch (e) {
                fallback(this, e);
            }
        });
        // the "next step" hint depends on this node's own output links
        chainCallback(nodeType.prototype, "onConnectionsChange", function () {
            this.mfTimeline?.render();
        });
        chainCallback(nodeType.prototype, "onRemoved", function () {
            this.mfTimeline?.destroy();
        });
    },
});
