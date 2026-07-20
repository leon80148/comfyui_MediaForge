// MediaForge frontend extension: mask MF_AIConfig's api_key widget on canvas.
//
// Why custom draw instead of a schema flag: ComfyUI frontend 1.43 has no
// "password" input spec — any STRING widget renders its raw value on the node
// body, in full view during screen-share / screenshots. There's no backend
// hook to intercept LiteGraph's widget painting, so the only lever is a
// frontend extension that swaps the widget's `type` to something LiteGraph's
// default widget-drawing branch doesn't recognize (LGraphCanvas.drawNodeWidgets
// calls `widget.draw(ctx, node, width, y, H)` for any widget carrying a `draw`
// method when its `type` isn't one of the built-ins) and supplies that method
// ourselves. `widget.value` is left untouched — serialization and FUNCTION
// execution both read it directly, so masking must be presentation-only.
//
// Defensive by design: every LiteGraph API surface touched here (widget
// internals, canvas.prompt, ctx drawing primitives) is feature-detected and
// wrapped in try/catch. If anything is missing or throws, we bail out and
// leave the widget in its original plaintext form — a broken mask must never
// break the node itself.

import { app } from "../../scripts/app.js";

const NODE_NAME = "MF_AIConfig";
const WIDGET_NAME = "api_key";
const MASK_TYPE = "mf_password";
const MASK_CHAR = "•";

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

// Best-effort mimic of LiteGraph's built-in text widget chrome (rounded box +
// label + value) so the masked widget doesn't look out of place next to
// unmodified widgets on the same node.
function drawMaskedWidget(ctx, node, widget_width, y, H) {
    const margin = 15;
    const outline_color = LiteGraph.WIDGET_OUTLINE_COLOR || "#666";
    const bg_color = LiteGraph.WIDGET_BGCOLOR || "#222";
    const text_color = LiteGraph.WIDGET_TEXT_COLOR || "#ddd";
    const secondary_text_color = LiteGraph.WIDGET_SECONDARY_TEXT_COLOR || "#999";

    ctx.textAlign = "left";
    ctx.strokeStyle = outline_color;
    ctx.fillStyle = bg_color;
    ctx.beginPath();
    if (ctx.roundRect) {
        ctx.roundRect(margin, y, widget_width - margin * 2, H, H * 0.5);
    } else {
        ctx.rect(margin, y, widget_width - margin * 2, H);
    }
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = secondary_text_color;
    ctx.fillText(this.label || this.name, margin + 5, y + H * 0.7);

    const masked = this.value ? MASK_CHAR.repeat(Math.min(String(this.value).length, 24)) : "";
    ctx.fillStyle = text_color;
    ctx.textAlign = "right";
    ctx.fillText(masked, widget_width - margin - 5, y + H * 0.7);
}

// Mirrors LiteGraph's text-widget mouse handling: click opens the same
// canvas.prompt() edit box other text widgets use, pre-filled with the real
// value (reveal-on-edit, same UX tradeoff as a native password field).
function maskedWidgetMouse(event, pos, node) {
    if (event.type !== "pointerdown" && event.type !== "mousedown") return false;
    const canvas = app.canvas;
    if (!canvas || typeof canvas.prompt !== "function") return false;
    const widget = this;
    try {
        canvas.prompt("api_key", widget.value ?? "", (v) => {
            widget.value = v;
            widget.callback?.(widget.value, canvas, node, pos, event);
        }, event);
    } catch (err) {
        console.warn("[MediaForge] ai_config_mask: canvas.prompt failed, falling back to no-op click", err);
    }
    return true;
}

app.registerExtension({
    name: "MediaForge.AIConfigMask",
    async beforeRegisterNodeDef(nodeType, nodeData, _app) {
        if (nodeData.name !== NODE_NAME) return;

        chainCallback(nodeType.prototype, "onNodeCreated", function () {
            try {
                const widget = this.widgets?.find((w) => w.name === WIDGET_NAME);
                if (!widget) {
                    console.warn(`[MediaForge] ai_config_mask: ${WIDGET_NAME} widget not found on ${NODE_NAME}, skipping mask`);
                    return;
                }
                if (typeof LiteGraph === "undefined") {
                    console.warn("[MediaForge] ai_config_mask: LiteGraph global not available, skipping mask");
                    return;
                }
                // Feature-detect the exact hook LGraphCanvas.drawNodeWidgets relies on
                // for unknown widget types (custom draw + mouse). If a future frontend
                // version drops this contract, leave the widget as plain text.
                if (typeof widget.value === "undefined") {
                    console.warn("[MediaForge] ai_config_mask: widget has no .value, skipping mask");
                    return;
                }
                widget.type = MASK_TYPE;
                widget.draw = drawMaskedWidget;
                widget.mouse = maskedWidgetMouse;
            } catch (err) {
                console.warn("[MediaForge] ai_config_mask: failed to install masked widget, leaving api_key in plaintext", err);
            }
        });
    },
});
