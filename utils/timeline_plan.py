"""Timeline 計畫合約：時基、grid profile、timeline_data 驗證、MF_SEGMENT 規格。

Python 是唯一權威；web/timeline_grid.js 鏡射同一套規則給 GUI 即時回饋，兩邊用
tests/golden/timeline_vectors.json 對拍。所有邊界都是整數幀與有理數 fps，秒只
用來顯示——浮點 fps（例如 23.976）換算 NTSC 會挑錯分母，所以連輸入都不收浮點。
"""
import copy
import hashlib
import json
from fractions import Fraction

from . import timeline_materials
from .timeline_messages import TimelineError
from .timeline_source import check_relative_name

NODE_LABEL = "Edit Timeline"

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


def _string(value, location):
    # a lone surrogate parses fine but can't be UTF-8 encoded for the plan hash
    if not isinstance(value, str) or any("\ud800" <= ch <= "\udfff" for ch in value):
        _fail("必須是字串", location)
    return value


def parse_timeline_data(text):
    """嚴格解析 timeline_data（v1 / v2）並補齊 canonical 預設；一律回傳 v2 形式的語意 dict。"""
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as e:
        _fail(str(e), "timeline_data")
    if not isinstance(data, dict):
        _fail("必須是物件", "timeline_data")
    v2 = data.get("version") == 2
    _keys(data, ("version", "source", "prompt", "segments", "materials", "refs", "anchors") if v2
          else ("version", "source", "segments"), "timeline_data")
    if _int(data.get("version"), "version") not in (1, 2):
        _fail("只支援 version 1 / 2", "version")

    source = data.get("source", {})
    _keys(source, ("origin_frame",), "source")
    origin = _int(source.get("origin_frame", 0), "source.origin_frame")
    prompt = _string(data.get("prompt", ""), "prompt")

    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        _fail("至少需要一段", "segments")
    if len(segments) > MAX_SEGMENTS:
        _fail(f"最多 {MAX_SEGMENTS} 段", "segments")
    out = []
    for i, seg in enumerate(segments):
        loc = f"segments[{i}]"
        _keys(seg, ("start", "end", "prompt", "seam_in", "seed") if v2 else ("start", "end", "prompt", "seam_in"), loc)
        start = _int(seg.get("start"), f"{loc}.start", minimum=0)
        end = _int(seg.get("end"), f"{loc}.end", minimum=0)
        seg_prompt = _string(seg.get("prompt", ""), f"{loc}.prompt")
        seed = seg.get("seed")
        if seed is not None:
            seed = _int(seed, f"{loc}.seed", minimum=0)
        out.append({"start": start, "end": end, "prompt": seg_prompt, "seed": seed,
                    "seam_in": _parse_seam(seg.get("seam_in", {"kind": "cut"}), f"{loc}.seam_in")})
    materials = timeline_materials.parse_materials(data.get("materials", []), check_relative_name)
    return {"version": 2, "source": {"origin_frame": origin}, "prompt": prompt, "segments": out,
            "materials": materials,
            "refs": timeline_materials.parse_refs(data.get("refs", []), materials),
            "anchors": timeline_materials.parse_anchors(data.get("anchors", []), materials)}


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


def compile_plan(data, mode, fps_key, grid, source=None, snapshots=None):
    """data 是 parse_timeline_data() 的結果；source 是 timeline_source.describe_source() 或 None；
    snapshots 是 {material_id: timeline_media.describe_material()}（沒有素材時可省略）。"""
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
        if data["materials"]:
            _fail("cut 模式不使用素材", "materials")
        diagnostics = _validate_cut(segments, source_timeline_frames(source, fps), origin)
        total = sum(s["end"] - s["start"] for s in segments)
    snapshots = snapshots or {}
    h3 = effective_grid == H3_GRID
    if data["materials"]:
        timeline_materials.validate_geometry(data["refs"], data["anchors"], snapshots, segments, h3)

    semantic = {"version": 2, "mode": mode, "fps": list(fps), "effective_grid": effective_grid,
                **{k: data[k] for k in ("source", "prompt", "segments", "materials", "refs", "anchors")}}
    plan_id = _digest(canonical_json(semantic))
    material_fps = sorted([m["id"], snapshots[m["id"]]["fingerprint"]] for m in data["materials"])
    fingerprint = source["fingerprint"] if source else ""
    plan_source = None
    if source is not None:
        plan_source = {k: source.get(k) for k in ("source_ref", "fingerprint", "fps", "frame_count", "source_start_us", "duration_us")}
        plan_source["origin_frame"] = origin
    plan = {
        "type": "MF_SEGMENT_PLAN", "version": 2,
        "plan_id": plan_id, "execution_id": _digest(canonical_json([plan_id, fingerprint, material_fps])),
        "mode": mode, "fps": list(fps), "effective_grid": effective_grid,
        "source": plan_source, "prompt": data["prompt"],
        "materials": [snapshots[m["id"]] for m in data["materials"]],
        "refs": data["refs"], "anchors": data["anchors"],
        "segments": [{"index": i, **s} for i, s in enumerate(segments)],
        "total_frames": total, "diagnostics": diagnostics,
    }
    plan["specs"] = _build_specs(plan, data, snapshots, h3)
    return plan


def _emit_intervals(plan):
    segments = plan["segments"]
    if plan["mode"] == "cut":
        out, cursor = [], 0
        for s in segments:
            out.append([cursor, cursor + s["end"] - s["start"]])
            cursor += s["end"] - s["start"]
        return out
    return [[segments[i - 1]["end"] if i else 0, s["end"]] for i, s in enumerate(segments)]


_EMPTY_MANIFEST = {"pictures": [], "videos": [], "audios": [], "keyframes": [], "guides": [], "locks": [],
                   "carry_conflicts": []}
# 只供報告 / GUI 的欄位：改名、換 binding id 不該讓 conditioning 重新編碼
_REPORT_KEYS = {"name", "binding", "anchor", "material", "requested_frames", "discarded_tail", "tag", "audio_tag",
                "soundtrack_samples", "samples"}


def _encode_materials(manifest):
    out = {}
    for kind in ("pictures", "videos", "audios", "keyframes", "guides", "locks"):
        out[kind] = [{**{k: v for k, v in item.items() if k not in _REPORT_KEYS and k != "snapshot"},
                      "snapshot": {k: v for k, v in item["snapshot"].items() if k not in ("id", "name")}}
                     for item in manifest[kind]]
    for lock in out["locks"]:
        lock["policy"] = timeline_materials.LOCK_POLICY
    return out


def _audio_locks(plan, seg):
    """本段觸及的 lock 在母帶上的時間軸區間（以 anchor 為單位，Assemble 依 id 合併）。"""
    by_id = {m["id"]: m for m in plan["materials"]}
    out = []
    for a in plan["anchors"]:
        if a["role"] != "lock":
            continue
        lo, hi = a["at"], min(a["at"] + a["length"], plan["total_frames"])
        if lo < seg["end"] and hi > seg["start"] and lo < hi:
            snap = by_id[a["material"]]
            out.append({"anchor": a["id"], "start": lo, "end": hi, "in": a["in"], "ref": snap["ref"],
                        "fingerprint": snap["fingerprint"],
                        **{k: snap[k] for k in ("audio_sample_rate", "audio_channels", "audio_start_us", "format_start_us")}})
    return out


def _build_specs(plan, data, snapshots, h3):
    segments = plan["segments"]
    count = len(segments)
    grid = plan["effective_grid"]
    generate = plan["mode"] == "generate"
    emits = _emit_intervals(plan)
    source = plan["source"]
    specs, prev_sampling, prev_assembly = [], None, None
    for index, seg in enumerate(segments):
        prev = segments[index - 1] if index else None
        nxt = segments[index + 1] if index + 1 < count else None
        overlap_in = prev["end"] - seg["start"] if (generate and prev) else 0
        overlap_out = seg["end"] - nxt["start"] if (generate and nxt) else 0
        length = seg["end"] - seg["start"]
        carry_in = carry_spec(grid, prev["end"] - prev["start"], overlap_in) if prev else None
        carry_out = carry_spec(grid, length, overlap_out) if nxt else None
        window = None
        if source is not None:
            window = {"source_ref": source["source_ref"], "source_fingerprint": source["fingerprint"],
                      "source_start_us": source["source_start_us"], "origin_frame": source["origin_frame"],
                      "start_frame": seg["start"], "end_frame": seg["end"]}
        manifest = _EMPTY_MANIFEST
        if generate and plan["materials"]:
            manifest = timeline_materials.segment_manifest(segments, index, plan["refs"], plan["anchors"],
                                                           snapshots, h3, carry_in)
        template = seg["prompt"] or plan["prompt"]
        prompt = timeline_materials.resolve_prompt(template, manifest, data["materials"], index, h3) if generate else template
        spec = {
            "type": "MF_SEGMENT", "version": 2, "view": "full",
            "index": index, "is_first": index == 0, "is_last": index == count - 1,
            "start": seg["start"], "end": seg["end"], "length": length,
            "expected_tokens": latent_tokens(grid, length),
            "overlap_in": overlap_in, "overlap_out": overlap_out,
            "emit_interval": emits[index],
            "fps": plan["fps"], "effective_grid": grid,
            "carry_in": carry_in, "carry_out": carry_out,
            "carry_in_id": spec_id(carry_in), "carry_out_id": spec_id(carry_out),
            "carry_conflicts": manifest["carry_conflicts"],
            "seam_in": seg["seam_in"], "seam_out": nxt["seam_in"] if nxt else None,
            "source_window": window,
            "prompt": prompt, "prompt_template": template, "seed": seg["seed"],
            "materials": manifest, "audio_locks": _audio_locks(plan, seg) if generate else [],
            # 本段寫進 latent 的鎖音 token（只有 H3 profile 會鎖 latent）
            "latent_locks": [x["tokens"] for x in manifest["locks"] if "tokens" in x],
        }
        encode = segment_view(spec, "encode")
        spec["encode_id"] = _digest(canonical_json({k: v for k, v in encode.items() if k not in ("type", "version", "view", "index")}))
        # 鏡頭切換（overlap_in = 0）不需要前段 latent：取樣鏈在這裡重新起算，改前鏡不必重抽後鏡
        spec["prev_sampling_id"] = prev_sampling if overlap_in else None
        spec["sampling_id"] = _digest(canonical_json({
            "prev": spec["prev_sampling_id"], "encode": spec["encode_id"],
            "seed": seg["seed"] if seg["seed"] is not None else "global",
            "carry_in": carry_in, "overlap_in": overlap_in}))
        spec["prev_assembly_id"] = prev_assembly
        spec["assembly_id"] = _digest(canonical_json({
            "prev": prev_assembly, "sampling": spec["sampling_id"], "emit": spec["emit_interval"],
            "seam_in": spec["seam_in"], "seam_out": spec["seam_out"], "carry_out": carry_out,
            "is_last": spec["is_last"], "locks": spec["audio_locks"], "source_window": window}))
        prev_sampling, prev_assembly = spec["sampling_id"], spec["assembly_id"]
        specs.append(spec)
    return specs


VIEW_FIELDS = {
    "encode": ("index", "length", "fps", "effective_grid", "prompt", "materials"),
    "extend": ("index", "sampling_id", "prev_sampling_id", "expected_tokens", "length", "overlap_in",
               "carry_in", "carry_in_id", "effective_grid", "carry_conflicts"),
    "assemble": ("index", "assembly_id", "prev_assembly_id", "sampling_id", "start", "end", "length", "fps",
                 "effective_grid", "emit_interval", "overlap_in", "overlap_out", "carry_in", "carry_out",
                 "carry_out_id", "seam_in", "seam_out", "is_last", "audio_locks", "latent_locks", "source_window"),
}


def segment_view(spec, view):
    """expansion 傳給各節點的精簡 MF_SEGMENT（只含該節點會讀、且影響結果的欄位）。"""
    out = {"type": "MF_SEGMENT", "version": 2, "view": view}
    for key in VIEW_FIELDS[view]:
        out[key] = _encode_materials(spec["materials"]) if (view, key) == ("encode", "materials") else spec[key]
    return out


def require_segment(segment, views, label):
    """消費端入口檢查：MF_SEGMENT v2、view 被接受、必要欄位齊全；full 一律接受。"""
    if not isinstance(segment, dict) or segment.get("type") != "MF_SEGMENT":
        raise ValueError(f"[{label}] segment 必須來自 Select Segment，請檢查接線。")
    if segment.get("version") != 2:
        raise TimelineError(label, "E_VIEW", reason="segment 是舊版格式，請重新執行 Edit Timeline")
    view = segment.get("view")
    if view != "full" and view not in views:
        raise TimelineError(label, "E_VIEW", reason=f"不接受 {view} view")
    if view != "full":
        missing = [k for k in VIEW_FIELDS[view] if k not in segment]
        if missing:
            raise TimelineError(label, "E_VIEW", reason=f"缺少欄位 {missing}")
    return segment


def segment_spec(plan, index):
    """MF_SEGMENT（full view）：Extend / Append / Load / Encode 共用的不可變衍生快照。"""
    specs = plan["specs"]
    if not 0 <= index < len(specs):
        raise TimelineError("Select Segment", "E_SEGMENT_INDEX", count=len(specs), index=index)
    return copy.deepcopy(specs[index])


def cut_ranges_seconds(plan):
    """cut 模式各段的來源秒區間（相對來源第一幀，與 MF_DetectScenes / MF_TrimByRanges 同慣例）。"""
    if plan["mode"] != "cut":
        return []
    num, den = plan["fps"]
    origin = plan["source"]["origin_frame"]
    return [[float(Fraction((s["start"] - origin) * den, num)), float(Fraction((s["end"] - origin) * den, num))]
            for s in plan["segments"]]
