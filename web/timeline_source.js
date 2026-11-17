import { h3Crop, emitIntervals, MAX_MATERIALS } from "./timeline_materials.js";
import { autoName } from "./timeline_edit.js";

function id(prefix, entries) {
    let n = 1;
    while (entries.some((e) => e.id === `${prefix}${n}`)) n++;
    return `${prefix}${n}`;
}

// A user command, never run on hydration. Reuses the uploaded file and an existing
// card, and replaces only this source card's placements after an explicit choice.
export function useSource(data, ref, frames, role = "reference", audio = "none", hasAudio = false) {
    const minimum = data.remake_tail_policy === "pad_trim" ? 1 : 5;
    if (!Number.isSafeInteger(frames) || frames < minimum) throw new Error(`來源影片不足 ${minimum} 幀或尚未讀取完成。`);
    if (!["reference", "guide", "preview"].includes(role) || !["none", "reference", "original"].includes(audio)) throw new Error("來源用途無效。");
    if (audio !== "none" && !hasAudio) throw new Error("來源影片沒有音軌。");
    let material = data.materials.find((m) => m.kind === "video" && m.ref === ref);
    const materials = [...data.materials];
    if (!material) {
        if (materials.length >= MAX_MATERIALS) throw new Error("素材庫已滿，請先移除不使用的素材。");
        material = { id: id("m", materials), kind: "video", ref, name: autoName("video", materials.map((m) => m.name)) };
        materials.push(material);
    }
    const refs = data.refs.filter((r) => r.material !== material.id);
    const anchors = data.anchors.filter((a) => a.material !== material.id);
    const origin = data.source.origin_frame;
    const start = Math.max(0, -origin);
    const end = Math.min(data.segments.at(-1).end, frames - origin);
    if (role !== "preview" && end - start < minimum) throw new Error(`來源與目前時間軸沒有至少 ${minimum} 幀的交集，請調整來源起點。`);
    if (role === "reference" || audio === "reference") refs.push({ id: id("r", refs), material: material.id,
        from: start, to: end, mode: "follow", in: start + origin, audio: audio === "reference" });
    if (role === "guide") {
        for (const [a, b] of emitIntervals(data.segments)) {
            const at = Math.max(start, a), length = h3Crop(Math.min(b, end) - at);
            if (length >= 5) anchors.push({ id: id("a", anchors), material: material.id, role: "guide", at, in: at + origin, length, audio: false });
        }
    }
    if (audio === "original" && end > start) anchors.push({ id: id("a", anchors), material: material.id, role: "lock",
        at: start, in: start + origin, length: end - start });
    return { ...data, materials, refs, anchors };
}
