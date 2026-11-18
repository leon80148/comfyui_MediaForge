// What a Sample Remake Segments node refuses in one planned segment: utils/remake_adapter.py validate_segment() and
// reference_pins(), in their order, so the editor can say it while the timeline is being edited instead of after the
// models have loaded. The two are pinned together by tests/golden/remake_rules_vectors.json.
//
// It is a part of what the sampler checks, not all of it: that every segment gives the same canvas size, that the
// official nodes are installed and that the model matches `mode` are known only when it runs. The sampler's message
// stays the authority.

export const REMAKE_SECTIONS = ["subject_definitions", "summary", "retention_analysis", "detailed_description",
    "overall_soundscape", "non_diegetic_music"];

// `length`: the segment's planned frames; `manifest`: timeline_materials.segmentManifest()'s; `prompt`: the resolved
// prompt (aliases already written as <Picture N> / <Video N> / <Audio N>). → the first failing rule as {code, …what its
// message needs}, or null.
export function remakeProblem(length, manifest, prompt) {
    if (length < 90 || length > 345 || (length - 5) % 17) return { code: "length", frames: length };
    const videos = manifest.videos;
    if (videos.length !== 1 || videos[0].effective_frames !== length) {
        return { code: "source", videos: videos.length, covered: videos.length ? Math.max(...videos.map((v) => v.effective_frames)) : 0, frames: length };
    }
    if (manifest.pictures.length < 1 || manifest.pictures.length > 4) return { code: "pictures", count: manifest.pictures.length };
    const kinds = ["audios", "guides", "locks"].filter((k) => manifest[k].length);
    if (kinds.length) return { code: "unsupported", kinds };
    if (/<Audio [0-9]+>/.test(prompt)) return { code: "audio_tag" };
    const missing = REMAKE_SECTIONS.filter((s) => !prompt.includes(s));
    if (missing.length) return { code: "sections", missing };
    // reference_pins(): pictures are told apart by their file, and a keyframe belongs to the picture of the same file
    const pins = new Map(manifest.pictures.map((p) => [p.ref, 0]));
    if (pins.size !== manifest.pictures.length) return { code: "duplicate_picture" };
    for (const k of manifest.keyframes) {
        if (!pins.has(k.ref)) return { code: "keyframe_not_reference" };
        pins.set(k.ref, pins.get(k.ref) + 1);
    }
    const counts = [...pins.values()];
    if (counts.some((n) => n) && !counts.every((n) => n === 1)) return { code: "pins_uneven" };
    return null;
}
