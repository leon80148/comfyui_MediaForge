// MediaForge frontend extension: widget visibility manager
//
// Two complementary mechanisms, both about hiding dead UI surface:
//
// 1) Dual-input lock (DUAL_INPUT_NODES) — input-connection-driven
//    Hide + collapse the path widget (video_path / media_path / audio_path /
//    audio_source) on a dual-input node when its `trigger_input` socket (an
//    IMAGE `frames` pin or an AUDIO `audio` pin) gets connected — restore when
//    disconnected. Trigger source: onConnectionsChange.
//
//    Why: file-consumer nodes accept EITHER a path string widget OR a tensor pin.
//    At execution time the Python side picks tensor over path. Without this
//    frontend hint the path widget still looks editable, which confuses users
//    into filling both and wondering which wins. Hiding the widget when frames
//    is wired makes the "tensor wins" semantics visible.
//
//    Skips MF_ConcatVideos intentionally — its prepend semantics require BOTH
//    the tensor AND a non-empty video_paths list (≥2 inputs total), so hiding
//    video_paths would break the use case.
//
// 2) Widget value lock (WIDGET_VALUE_LOCKS) — widget-value-driven
//    Hide widgets when another widget's value matches a condition. Trigger
//    source: wrapping that widget's instance-level callback.
//
//    Why: same dead-UI-surface principle. e.g. MF_ComposeOverlayText's
//    `effect_duration` is silently ignored when `effect == "none"`, so the
//    widget shouldn't appear editable in that mode.

import { app } from "../../scripts/app.js";

// node type → which widgets to flip when the input named `trigger_input`
// (IMAGE `frames` or AUDIO `audio`) connects/disconnects
//   lock_widget           : reverse direction — visible only when trigger is DISCONNECTED (path mode)
//   linked_widgets        : same direction    — visible only when trigger is CONNECTED   (tensor mode)
//   hidden_when_connected : reverse direction — extra widgets to hide alongside lock_widget when
//                           trigger is wired. Use for path-mode-only widgets that aren't the path
//                           itself (e.g. keep_source_audio only matters when source comes from
//                           video_path; tensor frames already pre-mux audio in encode_tensor_to_tempfile).
//
// Dead UI surface taxonomy on dual-input nodes:
//   - lock_widget / hidden_when_connected  → path-mode-only, hide in tensor mode
//   - linked_widgets                       → tensor-mode-only, hide in path mode
//   - everything else                      → both-mode, always show
const DUAL_INPUT_NODES = {
    "MF_BurnSubtitle":  { trigger_input: "frames", lock_widget: "video_path", linked_widgets: ["tensor_fps"], hidden_when_connected: ["keep_source_audio"] },
    "MF_LoopVideo":     { trigger_input: "frames", lock_widget: "video_path", linked_widgets: ["tensor_fps"] },
    "MF_TrimByRanges":  { trigger_input: "frames", lock_widget: "video_path", linked_widgets: ["tensor_fps"] },
    "MF_ProbeMedia":    { trigger_input: "frames", lock_widget: "media_path", linked_widgets: ["tensor_fps"] },
    "MF_ComposeVideo":  { trigger_input: "frames", lock_widget: "video_path", linked_widgets: ["tensor_fps"] },
    // Audio-input dual mode: AUDIO socket replaces audio_path / audio_source. No tensor_fps
    // analog because AUDIO dict carries its own sample_rate.
    "MF_WhisperTranscribe": { trigger_input: "audio", lock_widget: "audio_path" },
    "MF_ComposeAudioMix":   { trigger_input: "audio", lock_widget: "audio_path" },
    "MF_DetectSilence":     { trigger_input: "audio", lock_widget: "audio_source" },
    "MF_ExtractAudio":      { trigger_input: "audio", lock_widget: "audio_source" },
};

// node type → array of widget-value conditional rules
//   trigger : the widget whose value drives visibility
//   value   : when trigger.value === this, the listed widgets are hidden
//   hide    : widgets to hide when condition matches
//
// Why separate from DUAL_INPUT_NODES: trigger source is different
// (widget.callback vs onConnectionsChange) and a node may use one or both.
//
// A node's rules array can list more than one rule (possibly keyed off different
// triggers). A widget hidden by ANY matching rule stays hidden — see the two-pass
// OR-then-apply in applyWidgetValueLocks() below.
const WIDGET_VALUE_LOCKS = {
    "MF_ComposeOverlayText": [
        { trigger: "effect", value: "none", hide: ["effect_duration"] },
    ],
    "MF_SaveVideoFrames": [
        // encode_mode is a three-way radio over rate-control widgets — each mode hides
        // the other two's input.
        { trigger: "encode_mode", value: "crf", hide: ["bitrate_kbps", "target_size_mb"] },
        { trigger: "encode_mode", value: "bitrate", hide: ["crf", "target_size_mb"] },
        { trigger: "encode_mode", value: "target_size", hide: ["crf", "bitrate_kbps"] },
        // gif/prores have no rate-control concept at all (palette-based / fixed profile),
        // so they hide the entire encode_mode sub-group regardless of its own value.
        { trigger: "codec", value: "gif (palette)", hide: ["encode_mode", "crf", "bitrate_kbps", "target_size_mb", "preset", "pix_fmt_override"] },
        { trigger: "codec", value: "prores (prores_ks)", hide: ["encode_mode", "crf", "bitrate_kbps", "target_size_mb", "preset"] },
    ],
    "MF_ComposeVideo": [
        // Unlike the other file-producer nodes, this node's codec dropdown holds the raw
        // codec_id ("prores_ks"), not the "name (id)" display string.
        { trigger: "codec", value: "prores_ks", hide: ["crf", "preset"] },
    ],
    "MF_BurnSubtitle": [
        { trigger: "codec", value: "prores (prores_ks)", hide: ["crf", "preset"] },
    ],
    "MF_LoopVideo": [
        { trigger: "codec", value: "prores (prores_ks)", hide: ["crf", "preset"] },
    ],
    "MF_TrimByRanges": [
        { trigger: "codec", value: "prores (prores_ks)", hide: ["crf", "preset"] },
        // Lossless mode stream-copies segments — codec/crf/preset never reach ffmpeg, and
        // crossfade needs re-encoded pixels so it's unusable too.
        { trigger: "precision", value: "lossless (stream copy)", hide: ["codec", "crf", "preset", "crossfade_sec"] },
    ],
    "MF_ConcatVideos": [
        { trigger: "codec", value: "prores (prores_ks)", hide: ["crf", "preset"] },
    ],
};

// Wrap an existing prototype method (or assign if missing) so multiple
// extensions can each add to the same lifecycle hook without clobbering.
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
    return node.widgets?.find(w => w.name === name);
}

function setWidgetHidden(widget, hidden) {
    if (!widget) return;
    widget.hidden = hidden;
    // computeSize override is needed for the node body to actually shrink —
    // without it the widget row still occupies vertical space even though
    // the input itself is not painted.
    widget.computeSize = hidden ? () => [0, -4] : null;
}

function isTriggerConnected(node, trigger_name) {
    const slot_idx = node.inputs?.findIndex(i => i.name === trigger_name);
    if (slot_idx == null || slot_idx < 0) return false;
    return node.inputs[slot_idx].link != null;
}

function applyLockState(node, config) {
    const connected = isTriggerConnected(node, config.trigger_input);
    // lock_widget: reverse direction — hide when trigger is wired
    setWidgetHidden(findWidget(node, config.lock_widget), connected);
    // hidden_when_connected: reverse direction — extra path-mode-only widgets (besides the path itself)
    for (const name of (config.hidden_when_connected || [])) {
        setWidgetHidden(findWidget(node, name), connected);
    }
    // linked_widgets: same direction — hide when trigger is NOT wired (widget only matters in tensor mode)
    for (const name of (config.linked_widgets || [])) {
        setWidgetHidden(findWidget(node, name), !connected);
    }
    // Force node to re-layout (size + redraw) after widget visibility change
    if (node.setSize && node.computeSize) {
        node.setSize(node.computeSize());
    }
    node.setDirtyCanvas?.(true, true);
}

function applyWidgetValueLocks(node, rules) {
    // W4-1: two-pass apply. A single sequential pass (setWidgetHidden per rule, in rule
    // order) breaks once a node has multiple rules touching the same widget from different
    // triggers (e.g. MF_SaveVideoFrames' encode_mode sub-rules and its codec=gif/prores
    // rules both target crf/preset) — a later unmatched rule would re-show a widget an
    // earlier matched rule just hid. Instead: collect the OR of "hidden" across every
    // matched rule first, then apply once.
    const hideSet = new Set();
    const allNames = new Set();
    for (const rule of rules) {
        const trig = findWidget(node, rule.trigger);
        const matched = trig && trig.value === rule.value;
        for (const name of rule.hide) {
            allNames.add(name);
            if (matched) hideSet.add(name);
        }
    }
    // Any widget named by some rule but not hidden by a currently-matched rule is
    // explicitly restored — covers widgets orphaned when their hiding rule stops matching.
    for (const name of allNames) {
        setWidgetHidden(findWidget(node, name), hideSet.has(name));
    }
    if (node.setSize && node.computeSize) {
        node.setSize(node.computeSize());
    }
    node.setDirtyCanvas?.(true, true);
}

// Wrap a widget instance's callback so multiple consumers can react to value changes.
// Distinct from chainCallback() which wraps prototype methods — widgets are per-node
// instances created during onNodeCreated, not on the prototype.
function chainWidgetCallback(widget, extra) {
    if (!widget) return;
    const orig = widget.callback;
    widget.callback = function () {
        const r = orig?.apply(this, arguments);
        extra.apply(this, arguments);
        return r;
    };
}

app.registerExtension({
    name: "MediaForge.DualInputLock",
    async beforeRegisterNodeDef(nodeType, nodeData, _app) {
        const config = DUAL_INPUT_NODES[nodeData.name];
        const valueRules = WIDGET_VALUE_LOCKS[nodeData.name];
        if (!config && !valueRules) return;

        if (config) {
            // Live connect/disconnect events
            chainCallback(nodeType.prototype, "onConnectionsChange", function (type, index, _connected, _link_info) {
                // LiteGraph.INPUT === 1 ; we only care about input-side changes
                if (type !== 1) return;
                const slot = this.inputs?.[index];
                if (!slot || slot.name !== config.trigger_input) return;
                applyLockState(this, config);
            });

            // Fresh node drag-from-menu
            chainCallback(nodeType.prototype, "onNodeCreated", function () {
                applyLockState(this, config);
            });

            // Saved-workflow load — at this point inputs[].link are restored
            chainCallback(nodeType.prototype, "onConfigure", function () {
                applyLockState(this, config);
            });
        }

        if (valueRules) {
            // Initial state + wrap each trigger widget's callback to react to value changes.
            // Wrapping happens in onNodeCreated because widget instances don't exist on the prototype.
            chainCallback(nodeType.prototype, "onNodeCreated", function () {
                const node = this;
                applyWidgetValueLocks(node, valueRules);
                const seen = new Set();
                for (const rule of valueRules) {
                    if (seen.has(rule.trigger)) continue;
                    seen.add(rule.trigger);
                    chainWidgetCallback(findWidget(node, rule.trigger), () => {
                        applyWidgetValueLocks(node, valueRules);
                    });
                }
            });

            // Saved-workflow load — widget values are restored before this fires
            chainCallback(nodeType.prototype, "onConfigure", function () {
                applyWidgetValueLocks(this, valueRules);
            });
        }
    },
});
