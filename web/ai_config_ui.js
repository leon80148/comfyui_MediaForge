// MediaForge frontend extension: 服務 / 模型 dropdowns on MF_AIConfig.
//
// Both dropdowns exist only here. They are left out of the saved workflow and the API prompt, and
// write plain values into the node's real widgets, so saved workflows and API clients keep working
// with the same five inputs. On load they are worked out again from those values.
//
// Visibility stays in this file instead of dual_input_lock.js's WIDGET_VALUE_LOCKS: that table would
// show base_url again whenever provider is openai_compatible, which is exactly when a preset hides it.

import { app } from "../../scripts/app.js";
import {
    CUSTOM_MODEL, SERVICES, modelChoice, modelLabel, modelOptions, serviceByName, serviceFromValues, serviceWrites,
    visibleWidgets,
} from "./ai_config_services.js";

const NODE_NAME = "MF_AIConfig";
const MIN_NODE_WIDTH = 340;  // the longest model label is cut off below this

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

function setWidgetHidden(widget, hidden) {
    widget.hidden = hidden;
    // Without computeSize the row keeps its vertical space even though it is not painted.
    widget.computeSize = hidden ? () => [0, -4] : null;
}

function relayout(node) {
    const min = node.computeSize();
    node.setSize([Math.max(node.size[0], min[0], MIN_NODE_WIDTH), min[1]]);
    node.setDirtyCanvas?.(true, true);
}

function addDropdown(node, name, label, values, onChange) {
    const widget = node.addWidget("combo", name, values[0], onChange, { values });
    widget.label = label;
    // serialize keeps it out of widgets_values; options.serialize keeps it out of the API prompt.
    widget.serialize = false;
    widget.options.serialize = false;
    return widget;
}

function install(node) {
    const field = (name) => node.widgets.find((w) => w.name === name);
    const real = ["provider", "base_url", "api_key", "model", "device"].map(field);
    if (real.some((w) => !w)) {
        console.warn(`[MediaForge] ai_config_ui: ${NODE_NAME} widgets not found, keeping the plain fields`);
        return;
    }
    let service = SERVICES[0];

    const render = (choice) => {
        modelDropdown.options.values = modelOptions(service);
        modelDropdown.value = choice;
        field("model").label = service.modelHint ?? "模型名稱";
        const visible = visibleWidgets(service, choice, field("provider").value);
        for (const [name, shown] of Object.entries(visible)) setWidgetHidden(field(name), !shown);
        relayout(node);
    };

    const serviceDropdown = addDropdown(node, "mf_service", "服務", SERVICES.map((s) => s.name), (name) => {
        // Picking the current service again must not overwrite the key the user typed.
        if (name === service.name) return;
        service = serviceByName(name);
        for (const [widget, value] of Object.entries(serviceWrites(service))) field(widget).value = value;
        render(modelChoice(service, field("model").value));
    });
    const modelDropdown = addDropdown(node, "mf_model", "模型", [CUSTOM_MODEL], (choice) => {
        if (choice !== CUSTOM_MODEL) field("model").value = choice;
        render(choice);
    });
    modelDropdown.options.getOptionLabel = (value) => modelLabel(service, value ?? modelDropdown.value);

    const others = node.widgets.filter((w) => w !== serviceDropdown && w !== modelDropdown);
    node.widgets.splice(0, node.widgets.length, serviceDropdown, modelDropdown, ...others);

    // Only reachable on the custom service, where provider is visible.
    const providerCallback = field("provider").callback;
    field("provider").callback = function () {
        const r = providerCallback?.apply(this, arguments);
        render(modelDropdown.value);
        return r;
    };

    node.mfAiConfigSync = () => {
        service = serviceFromValues(field("provider").value, field("base_url").value);
        serviceDropdown.value = service.name;
        render(modelChoice(service, field("model").value));
    };
    node.mfAiConfigSync();
}

app.registerExtension({
    name: "MediaForge.AIConfigUI",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;
        chainCallback(nodeType.prototype, "onNodeCreated", function () {
            install(this);
        });
        // Saved-workflow load and undo: widget values are restored before this fires.
        chainCallback(nodeType.prototype, "onConfigure", function () {
            this.mfAiConfigSync?.();
        });
    },
});
