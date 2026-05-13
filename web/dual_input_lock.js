// MediaForge frontend extension: dual-input lock UX
//
// Hide + collapse the path widget (video_path / media_path) on a dual-input
// node when its `frames` IMAGE input gets connected — restore when disconnected.
//
// Why: file-consumer nodes accept EITHER a path string widget OR a tensor pin.
// At execution time the Python side picks tensor over path. Without this
// frontend hint the path widget still looks editable, which confuses users
// into filling both and wondering which wins. Hiding the widget when frames
// is wired makes the "tensor wins" semantics visible.
//
// Skips MF_ConcatVideos intentionally — its prepend semantics require BOTH
// the tensor AND a non-empty video_paths list (≥2 inputs total), so hiding
// video_paths would break the use case.

import { app } from "../../scripts/app.js";

// node type → which widgets to flip when the IMAGE input named `trigger_input` connects/disconnects
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
    "MF_LoopVideo":     { trigger_input: "frames", lock_widget: "video_path", linked_widgets: ["fps"] },
    "MF_TrimByRanges":  { trigger_input: "frames", lock_widget: "video_path", linked_widgets: ["fps"] },
    "MF_ProbeMedia":    { trigger_input: "frames", lock_widget: "media_path", linked_widgets: ["fps"] },
    "MF_ComposeStart":  { trigger_input: "frames", lock_widget: "video_path", linked_widgets: ["fps"] },
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

app.registerExtension({
    name: "MediaForge.DualInputLock",
    async beforeRegisterNodeDef(nodeType, nodeData, _app) {
        const config = DUAL_INPUT_NODES[nodeData.name];
        if (!config) return;

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
    },
});
