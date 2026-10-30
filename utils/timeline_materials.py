"""Timeline v2 素材層：materials / refs / anchors 解析、幾何驗證、逐段 manifest、H3 標籤與別名。

純函式（不碰檔案）；web/timeline_materials.js 鏡射同一套規則給 GUI，兩邊以
tests/golden/timeline_materials_vectors.json 對拍。兩種物件：refs 是「參考綁定」（內容視窗與適用
範圍分開、陣列順序就是同類標籤順序），anchors 是與時間軸 1:1 對齊的錨定。歸屬一律看 emit
interval——generate 模式各段 emit 不重疊，重疊區屬於前段——只有 lock 例外取整個時間窗。
"""
import re
import unicodedata

from .timeline_messages import TimelineError

NODE_LABEL = "Edit Timeline"
KINDS = ("image", "video", "audio")
ANCHOR_ROLE = {"image": "keyframe", "video": "guide", "audio": "lock"}
REF_MODES = ("follow", "fixed")
MAX_MATERIALS = 64
MAX_REFS = 256
MAX_ANCHORS = 512
MAX_FRAME = 2 ** 31 - 1  # 任兩個相加仍是 JS-safe 整數
MAX_NAME_CODEPOINTS = 32
# 明列區間而非 \w / \p{L}：Python 與 JS 對 Unicode 類別的判定不完全相同
_NAME_RANGES = ((0x30, 0x39), (0x41, 0x5A), (0x61, 0x7A), (0x5F, 0x5F), (0x2D, 0x2D),
                (0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF))
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _fail(reason, location):
    raise TimelineError(NODE_LABEL, "E_TIMELINE_JSON", reason=reason, location=location)


def _frame(value, location, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("必須是整數", location)
    if not minimum <= value <= MAX_FRAME:
        _fail(f"必須在 {minimum}…{MAX_FRAME} 之間", location)
    return value


def _keys(obj, allowed, location):
    if not isinstance(obj, dict):
        _fail("必須是物件", location)
    extra = sorted(set(obj) - set(allowed))
    if extra:
        _fail(f"不認得的欄位 {extra}", location)


def _id(value, seen, location):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value) or value in seen:
        _fail("id 必須是 1–32 個英數 / _ / - 且不重複", location)
    seen.add(value)
    return value


def _bool(value, location):
    if not isinstance(value, bool):
        _fail("必須是 true / false", location)
    return value


def name_ok(name):
    return (isinstance(name, str) and 1 <= len(name) <= MAX_NAME_CODEPOINTS
            and unicodedata.is_normalized("NFC", name)
            and all(any(lo <= ord(c) <= hi for lo, hi in _NAME_RANGES) for c in name))


def parse_materials(raw, check_ref):
    """check_ref(rel) 驗證 input/ 相對路徑的字面規則（timeline_source.check_relative_name），不碰檔案。"""
    if not isinstance(raw, list):
        _fail("必須是陣列", "materials")
    if len(raw) > MAX_MATERIALS:
        _fail(f"最多 {MAX_MATERIALS} 個素材", "materials")
    out, ids, names = [], set(), set()
    for i, m in enumerate(raw):
        loc = f"materials[{i}]"
        _keys(m, ("id", "kind", "ref", "name"), loc)
        mid = _id(m.get("id"), ids, f"{loc}.id")
        if m.get("kind") not in KINDS:
            _fail(f"kind 必須是 {list(KINDS)}", f"{loc}.kind")
        try:
            check_ref(m.get("ref"))
        except ValueError as e:
            _fail(str(e), f"{loc}.ref")
        name = m.get("name")
        if not name_ok(name) or name in names:
            _fail("name 只能用英數、_、-、中日韓文字（1–32 字、NFC）且不重複", f"{loc}.name")
        names.add(name)
        out.append({"id": mid, "kind": m["kind"], "ref": m["ref"].replace("\\", "/"), "name": name})
    return out


def _material(by_id, value, location):
    m = by_id.get(value) if isinstance(value, str) else None
    if m is None:
        _fail("material 必須是已存在的素材 id", location)
    return m


def parse_refs(raw, materials):
    if not isinstance(raw, list):
        _fail("必須是陣列", "refs")
    if len(raw) > MAX_REFS:
        _fail(f"最多 {MAX_REFS} 個參考", "refs")
    by_id = {m["id"]: m for m in materials}
    out, ids = [], set()
    for i, r in enumerate(raw):
        loc = f"refs[{i}]"
        if not isinstance(r, dict):
            _fail("必須是物件", loc)
        m = _material(by_id, r.get("material"), f"{loc}.material")
        kind = m["kind"]
        mode = r.get("mode", "follow") if kind == "video" else None
        if kind == "video" and mode not in REF_MODES:
            _fail(f"mode 必須是 {list(REF_MODES)}", f"{loc}.mode")
        allowed = {"image": ("id", "material", "from", "to"),
                   "video": ("id", "material", "from", "to", "mode", "in", "audio") + (("length",) if mode == "fixed" else ()),
                   "audio": ("id", "material", "from", "to", "in", "length")}[kind]
        _keys(r, allowed, loc)
        ref = {"id": _id(r.get("id"), ids, f"{loc}.id"), "material": m["id"],
               "from": _frame(r.get("from"), f"{loc}.from")}
        ref["to"] = _frame(r.get("to"), f"{loc}.to", minimum=ref["from"] + 1)
        if kind == "video":
            ref.update(mode=mode, **{"in": _frame(r.get("in", 0), f"{loc}.in")},
                       audio=_bool(r.get("audio", False), f"{loc}.audio"))
        if kind == "audio":
            ref["in"] = _frame(r.get("in", 0), f"{loc}.in")
        if kind == "audio" or mode == "fixed":
            ref["length"] = _frame(r.get("length"), f"{loc}.length", minimum=1)
        out.append(ref)
    return out


def parse_anchors(raw, materials):
    if not isinstance(raw, list):
        _fail("必須是陣列", "anchors")
    if len(raw) > MAX_ANCHORS:
        _fail(f"最多 {MAX_ANCHORS} 個錨定", "anchors")
    by_id = {m["id"]: m for m in materials}
    out, ids = [], set()
    for i, a in enumerate(raw):
        loc = f"anchors[{i}]"
        if not isinstance(a, dict):
            _fail("必須是物件", loc)
        m = _material(by_id, a.get("material"), f"{loc}.material")
        role = ANCHOR_ROLE[m["kind"]]
        if a.get("role") != role:
            _fail(f"{m['kind']} 素材的錨定角色必須是 {role}", f"{loc}.role")
        allowed = {"keyframe": ("id", "material", "role", "at"),
                   "guide": ("id", "material", "role", "at", "in", "length", "audio"),
                   "lock": ("id", "material", "role", "at", "in", "length")}[role]
        _keys(a, allowed, loc)
        anchor = {"id": _id(a.get("id"), ids, f"{loc}.id"), "material": m["id"], "role": role,
                  "at": _frame(a.get("at"), f"{loc}.at")}
        if role != "keyframe":
            anchor["in"] = _frame(a.get("in", 0), f"{loc}.in")
            anchor["length"] = _frame(a.get("length"), f"{loc}.length", minimum=1)
        if role == "guide":
            anchor["audio"] = _bool(a.get("audio", False), f"{loc}.audio")
        out.append(anchor)
    return out


# ---- 幾何（需要素材快照；snapshots[id] 由 timeline_media.describe_material 產生）----

# 鎖音編碼政策版本（上下文長度等常數在 utils/h3_adapter.py）；進 encode view，常數改了快取自然失效
LOCK_POLICY = "ctx_v1"
H3_LIMITS = {"pictures": 9, "videos": 3, "audios": 3}
H3_MIN_REF_FRAMES = 5
H3_AUDIO_RATE, H3_SAMPLES_PER_TOKEN = 32000, 800  # H3 audio VAE; core VAE.encode crops to whole tokens


def h3_crop(frames):
    """core 對 ref video / guide 片段的實際用量：向下裁到 17k+5。"""
    return frames - (frames - 5) % 17


def emit_intervals(segments):
    return [(segments[i - 1]["end"] if i else 0, s["end"]) for i, s in enumerate(segments)]


def _span(a):
    return a["at"], a["at"] + a.get("length", 1)


def _ref_error(ref, reason):
    raise TimelineError(NODE_LABEL, "E_REF", ref=ref["id"], reason=reason)


def _anchor_error(anchor, reason):
    raise TimelineError(NODE_LABEL, "E_ANCHOR", anchor=anchor["id"], reason=reason)


def validate_geometry(refs, anchors, snapshots, segments, h3):
    total = segments[-1]["end"]
    for r in refs:
        snap = snapshots[r["material"]]
        if r["from"] >= total:
            _ref_error(r, f"起點超出時間軸總長（{total} 幀），不會套用到任何段")
        if snap["kind"] == "image":
            continue
        used = r["in"] + (r["to"] - r["from"] if r.get("mode") == "follow" else r["length"])
        if used > snap["frames"]:
            _ref_error(r, f"需要素材到第 {used} 幀，素材只有 {snap['frames']} 幀")
        if r.get("audio") and not snap["has_audio"]:
            _ref_error(r, "素材沒有音軌，不能開啟 audio")
    emits = emit_intervals(segments)
    for a in anchors:
        snap = snapshots[a["material"]]
        if a["at"] >= total:
            _anchor_error(a, f"位置超出時間軸總長（{total} 幀），不會套用到任何段")
        if a["role"] != "keyframe" and a["in"] + a["length"] > snap["frames"]:
            _anchor_error(a, f"需要素材到第 {a['in'] + a['length']} 幀，素材只有 {snap['frames']} 幀")
        if a.get("audio") and not snap["has_audio"]:
            _anchor_error(a, "素材沒有音軌，不能開啟 audio")
        if a["role"] == "guide":
            lo, hi = _span(a)
            if not any(ea <= lo and hi <= ee for ea, ee in emits):
                _anchor_error(a, "guide 必須完整落在單一段的輸出區間內（不可跨過段界）")
            if h3 and h3_crop(a["length"]) != a["length"]:
                _anchor_error(a, "H3 guide 長度必須是 5、22、39…（17k+5）幀")
    for stream, members in (("video", [a for a in anchors if a["role"] in ("keyframe", "guide")]),
                            ("audio", [a for a in anchors if a["role"] == "lock" or a.get("audio")])):
        spans = sorted((_span(a), a["id"]) for a in members)
        for (first, first_id), (second, second_id) in zip(spans, spans[1:]):
            if second[0] < first[1]:
                raise TimelineError(NODE_LABEL, "E_ANCHOR_OVERLAP", stream=stream, first=first_id, second=second_id)


def _item(snap, extra):
    return {"material": snap["id"], "name": snap["name"], "ref": snap["ref"], "fingerprint": snap["fingerprint"],
            "snapshot": snap, **extra}


def segment_manifest(segments, index, refs, anchors, snapshots, h3, carry_in=None):
    """第 index 段的素材清單（generate 模式）；h3 時附標籤、上限檢查與有效裁切。"""
    seg = segments[index]
    s, e = seg["start"], seg["end"]
    a0, e0 = emit_intervals(segments)[index]
    length = e - s
    pictures, videos, audios, keyframes, guides, locks = [], [], [], [], [], []
    seen_pictures, seen_audios = set(), set()
    for r in refs:
        if r["to"] <= a0 or r["from"] >= e0:
            continue
        snap = snapshots[r["material"]]
        if snap["kind"] == "image":
            if r["material"] not in seen_pictures:
                seen_pictures.add(r["material"])
                pictures.append(_item(snap, {"binding": r["id"]}))
        elif snap["kind"] == "video":
            if r["mode"] == "follow":
                lo, hi = max(r["from"], s), min(r["to"], e)
                start, requested = r["in"] + lo - r["from"], hi - lo
            else:
                start, requested = r["in"], r["length"]
            effective = requested
            if h3:
                n = min(requested, length)
                if n < H3_MIN_REF_FRAMES:
                    raise TimelineError(NODE_LABEL, "E_REF_SHORT", segment=index + 1, name=snap["name"], frames=n)
                effective = h3_crop(n)
            videos.append(_item(snap, {"binding": r["id"], "mode": r["mode"], "source_start": start,
                                       "requested_frames": requested, "effective_frames": effective,
                                       "discarded_tail": requested - effective, "soundtrack": r["audio"]}))
        else:
            key = (r["material"], r["in"], r["length"])
            if key not in seen_audios:
                seen_audios.add(key)
                audios.append(_item(snap, {"binding": r["id"], "source_start": r["in"], "frames": r["length"]}))
    for a in anchors:
        lo, hi = _span(a)
        snap = snapshots[a["material"]]
        if a["role"] == "keyframe":
            if a0 <= lo < e0:
                keyframes.append(_item(snap, {"anchor": a["id"], "frame_idx": lo - s}))
        elif a["role"] == "guide":
            if a0 <= lo and hi <= e0:
                guides.append(_item(snap, {"anchor": a["id"], "frame_idx": lo - s, "source_start": a["in"],
                                           "frames": a["length"], "soundtrack": a["audio"]}))
        elif lo < e and hi > s:
            p, q = max(lo, s) - s, min(hi, e) - s
            lock = _item(snap, {"anchor": a["id"], "frame_idx": p, "frames": q - p,
                                "source_start": a["in"] + max(lo, s) - lo})
            if h3:
                # 5/3 token per frame at 24 fps: a lock of ≥ 1 frame always covers a whole token, and q ≤ length keeps K1 ≤ Ta
                lock["tokens"] = [-(-5 * p // 3), 5 * q // 3]
            locks.append(lock)
    for group in (keyframes, guides, locks):
        group.sort(key=lambda x: x["frame_idx"])
    carried = carry_in["audio_tokens"] if h3 and carry_in and "audio_tokens" in carry_in else 0
    manifest = {"pictures": pictures, "videos": videos, "audios": audios,
                "keyframes": keyframes, "guides": guides, "locks": locks,
                "carry_conflicts": [x["anchor"] for x in locks if x["tokens"][0] < carried] if h3 else []}
    if h3:
        _h3_tags(manifest, index)
        _h3_samples(manifest)
    return manifest


def audio_sample_count(dst_start, frames, rate, fps, multiple=1):
    """[dst_start, dst_start + frames) 的音訊 sample 數 = 全域端點差 Q(end) − Q(start)（§4.3），再往下裁到 multiple 的整數倍。

    素材切片與 manifest 共用這一個算式：參考影片 / 音訊以素材軸（source_start）為端點，帶聲 guide 以段內位置
    （frame_idx）為端點。
    """
    num, den = fps
    count = (2 * (dst_start + frames) * rate * den + num) // (2 * num) - (2 * dst_start * rate * den + num) // (2 * num)
    return count - count % multiple


def _h3_samples(manifest):
    """實際送進 H3 audio VAE 的 samples（h3_adapter 解碼時呼叫同一個 audio_sample_count）。"""
    def samples(start, frames):
        return audio_sample_count(start, frames, H3_AUDIO_RATE, (24, 1), H3_SAMPLES_PER_TOKEN)
    for v in manifest["videos"]:
        if v["soundtrack"]:
            v["soundtrack_samples"] = samples(v["source_start"], v["effective_frames"])
    for item in manifest["audios"]:
        item["samples"] = samples(item["source_start"], item["frames"])
    for g in manifest["guides"]:
        if g["soundtrack"]:
            g["samples"] = samples(g["frame_idx"], g["frames"])


def _h3_tags(manifest, index):
    for kind, limit in H3_LIMITS.items():
        if len(manifest[kind]) > limit:
            raise TimelineError(NODE_LABEL, "E_REF_LIMIT", segment=index + 1, kind=kind,
                                count=len(manifest[kind]), limit=limit)
    for n, p in enumerate(manifest["pictures"], 1):
        p["tag"] = f"<Picture {n}>"
    # core 呈現順序：圖 → 影片（配對音軌的 <Audio j> 緊接在 <Video k> 前）→ 獨立音訊
    audio_n = 0
    for n, v in enumerate(manifest["videos"], 1):
        v["tag"] = f"<Video {n}>"
        if v["soundtrack"]:
            audio_n += 1
            v["audio_tag"] = f"<Audio {audio_n}>"
    for a in manifest["audios"]:
        audio_n += 1
        a["tag"] = f"<Audio {audio_n}>"


# ---- prompt 別名 ----

ALIAS_RE = re.compile(r"<@([^<>]*)>")
# core only writes ASCII digits; \d would also take full-width and other Unicode digits
TAG_RE = re.compile(r"<(Picture|Video|Audio) ([0-9]+)>")


def tag_counts(manifest):
    return {"Picture": len(manifest["pictures"]), "Video": len(manifest["videos"]),
            "Audio": sum(1 for v in manifest["videos"] if v["soundtrack"]) + len(manifest["audios"])}


def resolve_prompt(template, manifest, materials, index, h3):
    """`<@名稱>` / `<@名稱:audio>` → 本段標籤；直接寫的 `<Picture N>` 類標籤檢查編號範圍。"""
    def fail(reason):
        raise TimelineError(NODE_LABEL, "E_PROMPT_REF", segment=index + 1, reason=reason)

    if not h3:
        if ALIAS_RE.search(template) or TAG_RE.search(template):
            fail("素材別名與 <Picture N> 類標籤只適用於 H3（grid = h3_5+17n）")
        return template
    counts = tag_counts(manifest)
    for kind, n in TAG_RE.findall(template):
        if len(n) > 9 or not 1 <= int(n) <= counts[kind]:
            fail(f"<{kind} {n}> 超出本段的 {counts[kind]} 個 {kind}")
    by_name = {m["name"]: m for m in materials}

    def alias(match):
        inner = match.group(1)
        want_audio = inner.endswith(":audio")
        name = inner[: -len(":audio")] if want_audio else inner
        m = by_name.get(name)
        if m is None:
            fail(f"沒有名為「{name}」的素材")
        hits = [x for kind in ("pictures", "videos", "audios") for x in manifest[kind] if x["material"] == m["id"]]
        if len(hits) > 1:
            fail(f"「{name}」在本段有 {len(hits)} 個參考，請直接寫標籤")
        if not hits:
            anchored = any(x["material"] == m["id"] for k in ("keyframes", "guides", "locks") for x in manifest[k])
            fail(f"「{name}」在本段只用於錨定，沒有標籤" if anchored else f"「{name}」不在本段")
        if want_audio:
            if "audio_tag" not in hits[0]:
                fail(f"「{name}」在本段沒有配對音軌")
            return hits[0]["audio_tag"]
        return hits[0]["tag"]

    return ALIAS_RE.sub(alias, template)
