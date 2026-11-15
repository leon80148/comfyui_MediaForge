// Timeline-owned resolution: presentation only. Saved values, sockets and API inputs stay intact.
import { app } from "../../scripts/app.js";
import { schemaError } from "./timeline_grid.js";
import { strictParse } from "./timeline_materials.js";
import { DEFAULT_RENDER, renderOk } from "./timeline_render.js";

export const RESOLUTION_WIDGETS = {
    MF_SampleSegments: ["width", "height", "ref_video_short_edge"],
    MF_SampleRemakeSegments: ["short_edge"],
};
const REROUTES = new Set(["Reroute", "Reroute (rgthree)"]);
const savedVisibility = new WeakMap();
const watchedGraphs = new WeakSet();
const pending = new WeakSet();
const profiles = new WeakMap();
const nodesOf = (g) => g?.nodes ?? g?._nodes ?? [];
const rootOf = (g) => g?.rootGraph ?? g;
const linksOf = (g) => g?.links?.values ? [...g.links.values()] : Object.values(g?.links ?? {});

function graphsOf(graph) {
    const found = new Set();
    const visit = (g) => {
        if (!g || found.has(g)) return;
        found.add(g);
        for (const n of nodesOf(g)) if (n.subgraph) visit(n.subgraph);
    };
    visit(rootOf(graph));
    return [...found];
}

function inputLink(node, slot) {
    const id = node.inputs?.[slot]?.link;
    if (id == null && !node.subgraph) return null;
    // Subgraph nodes expose virtual slots: their getInputLink() can throw while
    // conversion restores concrete slots. Graph endpoints remain the authority.
    return node.graph?.links?.get?.(id) ?? node.graph?.links?.[id]
        ?? linksOf(node.graph).find((l) => String(l.target_id) === String(node.id) && l.target_slot === slot);
}

// Resolve only known transparent wiring. Shared subgraph definitions can have different outer
// inputs per instance; an ambiguous boundary deliberately leaves the fallback controls visible.
export function timelineSource(sampler) {
    if (!sampler?.graph) return null;
    const seen = new Set();
    const follow = (graph, link) => {
        if (!link || seen.has(link)) return null;
        seen.add(link);
        if (String(link.origin_id) === String(graph.inputNode?.id)) {
            const hosts = graphsOf(graph).flatMap(nodesOf).filter((n) => n.subgraph === graph);
            return hosts.length === 1 ? follow(hosts[0].graph, inputLink(hosts[0], link.origin_slot)) : null;
        }
        const node = graph.getNodeById?.(link.origin_id);
        if (!node || node.mode === 2 || node.mode === 4) return null;
        if (node.type === "MF_EditTimeline") {
            return (node.outputs?.[link.origin_slot]?.name === "segment_plan" || link.origin_slot === 0) ? node : null;
        }
        if (REROUTES.has(node.type)) return follow(graph, inputLink(node, 0));
        if (node.subgraph) {
            const inner = node.subgraph;
            const outputs = linksOf(inner).filter((l) => String(l.target_id) === String(inner.outputNode?.id)
                && l.target_slot === link.origin_slot);
            return outputs.length === 1 ? follow(inner, outputs[0]) : null;
        }
        return null;
    };
    const slot = sampler.inputs?.findIndex((i) => i.name === "segment_plan");
    return slot >= 0 ? follow(sampler.graph, inputLink(sampler, slot)) : null;
}

export function timelineConsumers(timeline) {
    if (!timeline?.graph) return [];
    return graphsOf(timeline.graph).flatMap(nodesOf)
        .filter((n) => RESOLUTION_WIDGETS[n.type] && timelineSource(n) === timeline);
}

export function managedResolution(sampler) {
    const source = timelineSource(sampler);
    if (!source || source.inputs?.some((i) => ["mode", "timeline_data"].includes(i.name) && i.link != null)) return null;
    const widgets = source.widgets ?? [];
    if (widgets.find((w) => w.name === "mode")?.value !== "generate") return null;
    try {
        const data = strictParse(widgets.find((w) => w.name === "timeline_data")?.value ?? "");
        return schemaError(data) === null && renderOk(data.render) ? { ...DEFAULT_RENDER, ...data.render } : null;
    } catch {
        return null;
    }
}

function chain(object, key, fn) {
    const previous = object[key];
    object[key] = function (...args) {
        const result = previous?.apply(this, args);
        fn.apply(this, args);
        return result;
    };
}

function hide(widget, hidden) {
    if (!widget) return false;
    if (hidden) {
        if (savedVisibility.has(widget)) return false;
        savedVisibility.set(widget, { hidden: widget.hidden, computeSize: widget.computeSize });
        widget.hidden = true;
        widget.computeSize = () => [0, -4];
    } else {
        const saved = savedVisibility.get(widget);
        if (!saved) return false;
        Object.assign(widget, saved);
        savedVisibility.delete(widget);
    }
    return true;
}

function applyResolution(node) {
    const names = RESOLUTION_WIDGETS[node.type];
    if (!names) return;
    const render = managedResolution(node);
    let changed = false;
    for (const name of names) changed = hide(node.widgets?.find((w) => w.name === name), !!render) || changed;
    if (render && !node.mfResolutionHint && node.addDOMWidget) {
        const element = document.createElement("div");
        element.style.cssText = "font:12px sans-serif;color:var(--fg-color,#ddd);padding:2px 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
        const widget = node.addDOMWidget("timeline_resolution_source", "mf_resolution_source", element, {
            serialize: false, hideOnZoom: false, getMinHeight: () => 22, getHeight: () => 22,
        });
        widget.serialize = false;
        node.mfResolutionHint = { widget, element };
        changed = true;
    }
    const hint = node.mfResolutionHint;
    if (hint) {
        changed = hide(hint.widget, !render) || changed;
        hint.element.style.display = render ? "" : "none";
        hint.element.textContent = render ? `解析度由 Edit Timeline 管理 · ${render.width} × ${render.height}` : "";
    }
    if (changed) {
        if (node.computeSize && node.setSize) {
            const min = node.computeSize();
            node.setSize([Math.max(node.size[0], min[0]), min[1]]);
        }
        node.setDirtyCanvas?.(true, true);
    }
}

export function syncTimelineControls(graph) {
    for (const g of graphsOf(graph)) {
        if (!watchedGraphs.has(g)) {
            watchedGraphs.add(g);
            chain(g, "onConnectionChange", () => scheduleTimelineControls(g));
        }
        for (const node of nodesOf(g)) applyResolution(node);
    }
    for (const node of graphsOf(graph).flatMap(nodesOf).filter((n) => n.type === "MF_EditTimeline")) {
        const signature = timelineConsumers(node).map((n) => n.type).sort().join(",");
        if (profiles.get(node) !== signature) {
            profiles.set(node, signature);
            node.mfTimeline?.render();
        }
    }
}

export function scheduleTimelineControls(graph) {
    const root = rootOf(graph);
    if (!root || pending.has(root)) return;
    pending.add(root);
    queueMicrotask(() => {
        pending.delete(root);
        syncTimelineControls(root);
    });
}

app.registerExtension({
    name: "MediaForge.TimelineResolutionControls",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "MF_EditTimeline" && !RESOLUTION_WIDGETS[nodeData.name]) return;
        const refresh = function () { scheduleTimelineControls(this.graph); };
        for (const event of ["onAdded", "onConfigure", "onConnectionsChange", "onRemoved"]) chain(nodeType.prototype, event, refresh);
        if (nodeData.name === "MF_EditTimeline") {
            chain(nodeType.prototype, "onNodeCreated", function () {
                for (const w of this.widgets ?? []) if (["mode", "timeline_data"].includes(w.name)) chain(w, "callback", refresh.bind(this));
            });
        }
    },
    afterConfigureGraph() { scheduleTimelineControls(app.graph); },
});
