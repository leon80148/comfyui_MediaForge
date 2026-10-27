"""Timeline 計畫合約：時基、grid profile、timeline_data 驗證、MF_SEGMENT 規格。

Python 是唯一權威；web/timeline_grid.js 鏡射同一套規則給 GUI 即時回饋，兩邊用
tests/golden/timeline_vectors.json 對拍。所有邊界都是整數幀與有理數 fps，秒只
用來顯示——浮點 fps（例如 23.976）換算 NTSC 會挑錯分母，所以連輸入都不收浮點。
"""
import hashlib
import json
from fractions import Fraction

from .timeline_messages import TimelineError

NODE_LABEL = "⏱️ Edit Timeline"

FPS_PRESETS = {
    "16": (16, 1), "24": (24, 1), "25": (25, 1), "30": (30, 1),
    "24000/1001": (24000, 1001), "30000/1001": (30000, 1001),
    "48": (48, 1), "50": (50, 1), "60000/1001": (60000, 1001), "60": (60, 1),
}
GRIDS = ("h3_5+17n", "causal_4n+1", "causal_8n+1", "free")
MODES = ("generate", "cut")
SEAM_KINDS = ("cut", "crossfade")
AUDIO_CURVES = ("auto", "linear", "equal_power")
MAX_SEGMENTS = 256
JS_SAFE_MAX = 2 ** 53 - 1
H3_GRID = "h3_5+17n"
H3_FPS = (24, 1)

# (合法長度 / carry 重疊的起點, 步長)。h3: 5+17n 幀 ↔ 2+5n token（core VAE 的
# (1,4,4,4,4) token 週期）；causal: 1+k·n 幀 ↔ 1+n token。
_GRID_STEPS = {"h3_5+17n": (5, 17), "causal_4n+1": (1, 4), "causal_8n+1": (1, 8)}


def sample_index(frame, sample_rate, fps):
    """時間軸幀 → 絕對 sample 索引（half-up）。所有音訊邊界都從絕對幀算，避免逐段累積捨入。"""
    num, den = fps
    return (2 * frame * sample_rate * den + num) // (2 * num)


def length_ok(grid, frames):
    if grid == "free":
        return frames >= 1
    base, step = _GRID_STEPS[grid]
    return frames >= base and (frames - base) % step == 0


def overlap_ok(grid, frames):
    if frames == 0 or grid == "free":
        return frames >= 0
    base, step = _GRID_STEPS[grid]
    return frames >= base and (frames - base) % step == 0


def latent_tokens(grid, frames):
    if grid == "free":
        return None
    base, step = _GRID_STEPS[grid]
    if grid == H3_GRID:
        return 2 + 5 * ((frames - base) // step)
    return 1 + (frames - base) // step


def h3_audio_tokens(frames):
    # core temporal_shape: round(frames / 24 * 40)；5F/3 的小數只有 0、1/3、2/3，不會碰到 .5
    return (10 * frames + 3) // 6


def h3_audio_carry(prev_length, overlap):
    """前段尾端 audio token 怎麼搬到新段開頭：k0 是最接近重疊起點的 token 邊界。"""
    u = Fraction(5 * (prev_length - overlap), 3)
    k0 = (10 * (prev_length - overlap) + 3) // 6
    phase = Fraction(k0) - u
    return {
        "audio_k0": k0,
        "audio_tokens": h3_audio_tokens(prev_length) - k0,
        "audio_phase": [phase.numerator, phase.denominator],
    }


def carry_spec(grid, prev_length, overlap):
    if overlap == 0 or grid == "free":
        return None
    spec = {"video_tokens": latent_tokens(grid, overlap)}
    if grid == H3_GRID:
        spec.update(h3_audio_carry(prev_length, overlap))
    return spec


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_id(spec):
    return _digest(canonical_json(spec)) if spec is not None else None


def _fail(reason, location):
    raise TimelineError(NODE_LABEL, "E_TIMELINE_JSON", reason=reason, location=location)


def _reject_duplicate_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"重複的鍵 {key!r}")
        seen.add(key)
    return dict(pairs)


def _reject_constant(name):
    raise ValueError(f"不支援的數值 {name}")


def _int(value, location, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("必須是整數", location)
    if abs(value) > JS_SAFE_MAX:
        _fail("整數超出可安全表示的範圍", location)
    if minimum is not None and value < minimum:
        _fail(f"必須 ≥ {minimum}", location)
    return value


def _keys(obj, allowed, location):
    if not isinstance(obj, dict):
        _fail("必須是物件", location)
    extra = sorted(set(obj) - set(allowed))
    if extra:
        _fail(f"不認得的欄位 {extra}", location)


def parse_timeline_data(text):
    """嚴格解析 timeline_data 並補齊 canonical 預設；回傳只含語意欄位的 dict。"""
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as e:
        _fail(str(e), "timeline_data")
    _keys(data, ("version", "source", "segments"), "timeline_data")
    if _int(data.get("version"), "version") != 1:
        _fail("只支援 version 1", "version")

    source = data.get("source", {})
    _keys(source, ("origin_frame",), "source")
    origin = _int(source.get("origin_frame", 0), "source.origin_frame")

    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        _fail("至少需要一段", "segments")
    if len(segments) > MAX_SEGMENTS:
        _fail(f"最多 {MAX_SEGMENTS} 段", "segments")
    out = []
    for i, seg in enumerate(segments):
        loc = f"segments[{i}]"
        _keys(seg, ("start", "end", "prompt", "seam_in"), loc)
        start = _int(seg.get("start"), f"{loc}.start", minimum=0)
        end = _int(seg.get("end"), f"{loc}.end", minimum=0)
        prompt = seg.get("prompt", "")
        if not isinstance(prompt, str):
            _fail("必須是字串", f"{loc}.prompt")
        out.append({"start": start, "end": end, "prompt": prompt,
                    "seam_in": _parse_seam(seg.get("seam_in", {"kind": "cut"}), f"{loc}.seam_in")})
    return {"version": 1, "source": {"origin_frame": origin}, "segments": out}


def _parse_seam(seam, location):
    _keys(seam, ("kind", "frames", "audio_curve"), location)
    kind = seam.get("kind")
    if kind not in SEAM_KINDS:
        _fail(f"kind 必須是 {list(SEAM_KINDS)}", f"{location}.kind")
    if kind == "cut":
        if set(seam) != {"kind"}:
            _fail("cut 不接受 frames / audio_curve", location)
        return {"kind": "cut"}
    curve = seam.get("audio_curve", "auto")
    if curve not in AUDIO_CURVES:
        _fail(f"audio_curve 必須是 {list(AUDIO_CURVES)}", f"{location}.audio_curve")
    return {"kind": "crossfade", "frames": _int(seam.get("frames"), f"{location}.frames", minimum=1),
            "audio_curve": curve}


def _validate_generate(segments, grid):
    if segments[0]["start"] != 0:
        _fail("generate 模式首段必須從 0 開始", "segments[0].start")
    for i, seg in enumerate(segments):
        loc = f"segments[{i}]"
        length = seg["end"] - seg["start"]
        if length <= 0:
            _fail("end 必須大於 start", f"{loc}.end")
        if not length_ok(grid, length):
            _fail(f"段長 {length} 幀不在 {grid} 的合法長度上", loc)
        if i == 0:
            if seg["seam_in"]["kind"] != "cut":
                _fail("首段沒有前一段，只能用 cut", f"{loc}.seam_in")
            continue
        prev = segments[i - 1]
        if seg["start"] <= prev["start"] or seg["end"] <= prev["end"]:
            _fail("start 與 end 都必須比前一段嚴格遞增", loc)
        if seg["start"] > prev["end"]:
            _fail("generate 模式段與段之間不能留白", f"{loc}.start")
        # start、end 都嚴格遞增已蘊含 overlap < 相鄰兩段長度
        overlap = prev["end"] - seg["start"]
        if not overlap_ok(grid, overlap):
            _fail(f"重疊 {overlap} 幀不在 {grid} 的合法重疊上", f"{loc}.start")
        seam = seg["seam_in"]
        if seam["kind"] == "crossfade":
            # pending 只能從前段自己的輸出區間 [end_{i-2}, end_{i-1}) 裡扣留
            prev_emit = prev["end"] - (segments[i - 2]["end"] if i >= 2 else 0)
            limit = min(overlap, prev_emit)
            if overlap == 0 or seam["frames"] > limit:
                _fail(f"crossfade 幀數必須在 1…{limit}（不超過重疊與前段輸出長度）", f"{loc}.seam_in.frames")
    diagnostics = []
    for i in range(1, len(segments) - 1):
        seg = segments[i]
        incoming = segments[i - 1]["end"] - seg["start"]
        outgoing = seg["end"] - segments[i + 1]["start"]
        if incoming + outgoing > seg["end"] - seg["start"]:
            diagnostics.append({"code": "carry_reuses_incoming", "segment": i})
    return diagnostics


def _validate_cut(segments, source_frames, origin):
    for i, seg in enumerate(segments):
        loc = f"segments[{i}]"
        if seg["end"] <= seg["start"]:
            _fail("end 必須大於 start", f"{loc}.end")
        if seg["seam_in"]["kind"] != "cut":
            _fail("cut 模式只能用 cut 接縫", f"{loc}.seam_in")
        if i and seg["start"] < segments[i - 1]["end"]:
            _fail("cut 模式各段必須依序排列且不重疊", f"{loc}.start")
        if seg["start"] < origin or (source_frames is not None and seg["end"] > origin + source_frames):
            _fail("超出來源影片範圍", loc)
    return []


def source_timeline_frames(source, fps):
    """來源在時間軸 fps 下涵蓋多少幀（未知 duration 回 None）。"""
    if source is None or source.get("duration_us") is None:
        return None
    num, den = fps
    return (source["duration_us"] * num) // (den * 1_000_000)


def compile_plan(data, mode, fps_key, grid, source=None):
    """data 是 parse_timeline_data() 的結果；source 是 timeline_source.describe_source() 或 None。"""
    if mode not in MODES:
        _fail(f"mode 必須是 {list(MODES)}", "mode")
    if fps_key not in FPS_PRESETS:
        _fail(f"fps 必須是 {list(FPS_PRESETS)}", "fps")
    if grid not in GRIDS:
        _fail(f"grid 必須是 {list(GRIDS)}", "grid")
    fps = FPS_PRESETS[fps_key]
    effective_grid = "free" if mode == "cut" else grid
    if effective_grid == H3_GRID and fps != H3_FPS:
        raise TimelineError(NODE_LABEL, "E_H3_FPS")
    segments = data["segments"]
    origin = data["source"]["origin_frame"]
    if mode == "generate":
        diagnostics = _validate_generate(segments, effective_grid)
        total = segments[-1]["end"]
    else:
        if source is None:
            _fail("cut 模式需要來源影片", "video")
        diagnostics = _validate_cut(segments, source_timeline_frames(source, fps), origin)
        total = sum(s["end"] - s["start"] for s in segments)

    semantic = {"version": 1, "mode": mode, "fps": list(fps), "effective_grid": effective_grid,
                "source": data["source"], "segments": segments}
    plan_id = _digest(canonical_json(semantic))
    fingerprint = source["fingerprint"] if source else ""
    plan_source = None
    if source is not None:
        plan_source = {k: source.get(k) for k in ("source_ref", "fingerprint", "fps", "frame_count", "source_start_us", "duration_us")}
        plan_source["origin_frame"] = origin
    return {
        "type": "MF_SEGMENT_PLAN", "version": 1,
        "plan_id": plan_id, "execution_id": _digest(plan_id + "|" + fingerprint),
        "mode": mode, "fps": list(fps), "effective_grid": effective_grid,
        "source": plan_source,
        "segments": [{"index": i, **s} for i, s in enumerate(segments)],
        "total_frames": total, "diagnostics": diagnostics,
    }


def _emit_intervals(plan):
    segments = plan["segments"]
    if plan["mode"] == "cut":
        out, cursor = [], 0
        for s in segments:
            out.append([cursor, cursor + s["end"] - s["start"]])
            cursor += s["end"] - s["start"]
        return out
    return [[segments[i - 1]["end"] if i else 0, s["end"]] for i, s in enumerate(segments)]


def segment_spec(plan, index):
    """MF_SEGMENT：Extend / Append / Load 共用的不可變衍生快照。"""
    segments = plan["segments"]
    count = len(segments)
    if not 0 <= index < count:
        raise TimelineError("🎯 Select Segment", "E_SEGMENT_INDEX", count=count, index=index)
    seg = segments[index]
    grid = plan["effective_grid"]
    generate = plan["mode"] == "generate"
    prev = segments[index - 1] if index else None
    nxt = segments[index + 1] if index + 1 < count else None
    overlap_in = prev["end"] - seg["start"] if (generate and prev) else 0
    overlap_out = seg["end"] - nxt["start"] if (generate and nxt) else 0
    length = seg["end"] - seg["start"]
    carry_in = carry_spec(grid, prev["end"] - prev["start"], overlap_in) if prev else None
    carry_out = carry_spec(grid, length, overlap_out) if nxt else None
    source = plan["source"]
    window = None
    if source is not None:
        window = {"source_ref": source["source_ref"], "source_fingerprint": source["fingerprint"],
                  "source_start_us": source["source_start_us"], "origin_frame": source["origin_frame"],
                  "start_frame": seg["start"], "end_frame": seg["end"]}
    return {
        "type": "MF_SEGMENT", "version": 1,
        "plan_id": plan["plan_id"], "execution_id": plan["execution_id"],
        "index": index, "count": count, "is_first": index == 0, "is_last": index == count - 1,
        "total_frames": plan["total_frames"],
        "start": seg["start"], "end": seg["end"], "length": length,
        "expected_tokens": latent_tokens(grid, length),
        "overlap_in": overlap_in, "overlap_out": overlap_out,
        "emit_interval": _emit_intervals(plan)[index],
        "fps": plan["fps"], "effective_grid": grid,
        "carry_in": carry_in, "carry_out": carry_out,
        "carry_in_id": spec_id(carry_in), "carry_out_id": spec_id(carry_out),
        "seam_in": seg["seam_in"], "seam_out": nxt["seam_in"] if nxt else None,
        "source_window": window,
        "prompt": seg["prompt"],
    }


def cut_ranges_seconds(plan):
    """cut 模式各段的來源秒區間（相對來源第一幀，與 MF_DetectScenes / MF_TrimByRanges 同慣例）。"""
    if plan["mode"] != "cut":
        return []
    num, den = plan["fps"]
    origin = plan["source"]["origin_frame"]
    return [[float(Fraction((s["start"] - origin) * den, num)), float(Fraction((s["end"] - origin) * den, num))]
            for s in plan["segments"]]
