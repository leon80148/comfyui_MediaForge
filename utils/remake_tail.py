"""Pure Remake tail planning. Virtual padding never changes the source file."""
import copy


def prepare_tail(data, snapshots):
    data, snapshots = copy.deepcopy(data), copy.deepcopy(snapshots)
    videos = [r for r in data["refs"] if snapshots[r["material"]]["kind"] == "video"]
    if len(videos) != 1 or videos[0].get("mode") != "follow" or videos[0]["from"] != 0:
        raise ValueError("Remake 尾段補幀需要一支從時間軸起點連續跟隨的來源影片。")
    ref = videos[0]
    snap = snapshots[ref["material"]]
    end = min(data["segments"][-1]["end"], ref["to"], snap["frames"] - ref["in"])
    if end <= 0:
        raise ValueError("Remake 來源沒有可用畫面。")
    # Only retain windows that contribute new output, including with large overlaps.
    kept = []
    for seg in data["segments"]:
        if kept and kept[-1]["end"] >= end:
            break
        kept.append(seg)
    last = kept[-1]
    valid = end - last["start"]
    overlap = kept[-2]["end"] - last["start"] if len(kept) > 1 else 0
    needed = max(90, valid, overlap + 1)
    length = 5 + 17 * ((needed - 5 + 16) // 17)
    if length > 345:
        raise ValueError("Remake 尾段超過 345 幀，請縮短分段。")
    last["end"] = last["start"] + length
    data["segments"] = kept
    ref["to"] = last["end"]
    snap["remake_valid_frames"] = ref["in"] + end
    snap["frames"] = max(snap["frames"], ref["in"] + last["end"])
    if any(a["at"] >= end for a in data["anchors"]):
        raise ValueError("Remake 關鍵幀或錨定超出有效來源結尾，請移回來源範圍。")
    return data, snapshots, end
