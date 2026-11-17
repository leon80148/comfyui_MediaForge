// Matches utils/remake_tail.py; snapshots are virtual and never persisted.
export function prepareRemakeTail(input, sourceSnapshots) {
    const data = structuredClone(input), snapshots = structuredClone(sourceSnapshots);
    const videos = data.refs.filter((r) => snapshots[r.material]?.kind === "video");
    if (videos.length !== 1 || videos[0].mode !== "follow" || videos[0].from !== 0) throw new Error("Remake 尾段補幀需要一支從時間軸起點連續跟隨的來源影片。");
    const ref = videos[0], snap = snapshots[ref.material];
    const end = Math.min(data.segments.at(-1).end, ref.to, snap.frames - ref.in);
    if (end <= 0) throw new Error("Remake 來源沒有可用畫面。");
    const kept = [];
    for (const seg of data.segments) {
        if (kept.length && kept.at(-1).end >= end) break;
        kept.push(seg);
    }
    const last = kept.at(-1), valid = end - last.start;
    const overlap = kept.length > 1 ? kept.at(-2).end - last.start : 0;
    const length = 5 + 17 * Math.ceil((Math.max(90, valid, overlap + 1) - 5) / 17);
    if (length > 345) throw new Error("Remake 尾段超過 345 幀，請縮短分段。");
    last.end = last.start + length;
    data.segments = kept;
    ref.to = last.end;
    snap.remake_valid_frames = ref.in + end;
    snap.frames = Math.max(snap.frames, ref.in + last.end);
    if (data.anchors.some((a) => a.at >= end)) throw new Error("Remake 關鍵幀或錨定超出有效來源結尾，請移回來源範圍。");
    return { data, snapshots, outputEnd: end, validFrames: valid, inferenceFrames: length, paddedFrames: length - valid };
}
