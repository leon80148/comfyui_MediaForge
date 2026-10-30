"""MF_SampleSegments — H3 低記憶體分段取樣器（graph expansion）。

展開成逐段的 EncodeH3 → Extend → Guider → SamplerCustomAdvanced → Append。每段規格以 JSON 常數
（utils/timeline_plan.segment_view）進子圖，模型等物件一律連回原上游節點——core 快取依祖先簽章
判定，所以只有有效輸入真的改變的節點會重跑，中斷後重跑也會重用已完成的段。取樣鏈以
previous_latent 串接、在鏡頭切換（overlap = 0）處斷開；解碼鏈以 state 串接。整段 RGB 只在
Append 內解碼後落盤，不進快取。
"""
import json

from ..utils.h3_adapter import lock_context, material_decode_bytes, needs_audio_vae
from ..utils.segment_spill import FFV1_BYTES_PER_PIXEL
from ..utils.timeline_materials import H3_AUDIO_RATE, LOCK_POLICY
from ..utils.timeline_plan import H3_FPS, H3_GRID, sample_index, segment_view

LABEL = "Sample Segments"
MANIFEST_KINDS = ("pictures", "videos", "audios", "keyframes", "guides", "locks")


def _effective(spec, carry_audio, mask, tapers):
    """本段的 planned 生效報告；鎖音是否真的保住由 Append 的漂移檢查（verified）決定。"""
    m, carried = spec["materials"], spec["overlap_in"] > 0
    out = [{"feature": "seed", "requested": spec["seed"], "applied": True,
            "reason": "segment_seed" if spec["seed"] is not None else "noise_seed"},
           {"feature": "audio_carry", "requested": carry_audio, "applied": carry_audio and carried and not spec["carry_conflicts"],
            "reason": None if not carry_audio else "shot_root" if not carried else "lock_conflict" if spec["carry_conflicts"] else None},
           {"feature": "taper", "requested": None if mask == "hard" else {"mask": mask, "tokens": tapers},
            "applied": mask != "hard" and carried, "reason": None if mask == "hard" or carried else "shot_root"}]
    if spec["latent_locks"]:
        out.append({"feature": "lock", "requested": spec["latent_locks"], "applied": "planned", "reason": "verified_by_append"})
    for v in m["videos"]:
        if v["effective_frames"] != v["requested_frames"]:
            out.append({"feature": "ref_crop", "requested": v["requested_frames"], "applied": v["effective_frames"],
                        "reason": "h3_17k_plus_5"})
    return out


def _estimates(spec, rate, has_audio, width, height, ref_video_short_edge):
    """估算（bytes），不是保證：Append 解碼的整段 RGB float32（E_RAM 預檢同一量級）、本段 emit 區間的 spill
    （FFV1 + float32 雙聲道 WAV；Append 的 E_DISK 預檢要求可用空間 2 倍）、Encode 每次素材解碼的大小。"""
    fps = tuple(spec["fps"])
    a, e = spec["emit_interval"]
    samples = sample_index(e, rate, fps) - sample_index(a, rate, fps) if has_audio else 0
    return {"rgb_decode_bytes": spec["length"] * width * height * 3 * 4,
            "spill_bytes": {"video": round((e - a) * width * height * FFV1_BYTES_PER_PIXEL), "audio": samples * 2 * 4},
            "material_decode_bytes": material_decode_bytes(spec["materials"], fps, rate, width, height,
                                                           ref_video_short_edge)}


def _report(plan, noise_seed, audio_vae, negative, cfg, carry_audio, mask, tapers, width, height, ref_video_short_edge):
    rate = getattr(audio_vae, "audio_sample_rate", H3_AUDIO_RATE)  # 與 h3_adapter.write_locks 相同的取樣率
    segments = []
    for spec in plan["specs"]:
        m = spec["materials"]
        segments.append({
            "index": spec["index"], "frames": [spec["start"], spec["end"]], "shot_root": spec["overlap_in"] == 0,
            "seed": spec["seed"] if spec["seed"] is not None else noise_seed,
            "encode_id": spec["encode_id"], "sampling_id": spec["sampling_id"], "assembly_id": spec["assembly_id"],
            "tags": {x["tag"]: x["name"] for kind in ("pictures", "videos", "audios") for x in m[kind]},
            "ref_videos": [{"name": v["name"], "requested": v["requested_frames"], "effective": v["effective_frames"]}
                           for v in m["videos"]],
            "anchors": [{"role": role, "name": x["name"], "frame_idx": x["frame_idx"]}
                        for role in ("keyframes", "guides") for x in m[role]],
            "latent_locks": spec["latent_locks"],
            "audio_carry_skipped_for_lock": carry_audio and bool(spec["carry_conflicts"]),
            "material_fingerprints": {x["name"]: x["fingerprint"] for kind in MANIFEST_KINDS for x in m[kind]},
            # planned：素材實際比 metadata 短時，編碼會再截短右側上下文
            "lock_encoding": [{"anchor": x["anchor"], "name": x["name"], "tokens": x["tokens"], "policy": LOCK_POLICY,
                               "context_tokens": list(lock_context(x, tuple(spec["fps"]), rate)[1:])}
                              for x in m["locks"]],
            "effective": _effective(spec, carry_audio, mask, tapers),
            "estimates": _estimates(spec, rate, audio_vae is not None, width, height, ref_video_short_edge),
        })
    return {"version": 2, "node": LABEL, "plan_id": plan["plan_id"], "segments": segments,
            "effective": [{"feature": "cfg", "requested": cfg, "applied": negative is not None,
                           "reason": None if negative is not None else "no_negative"},
                          {"feature": "audio_decode", "requested": True, "applied": audio_vae is not None,
                           "reason": None if audio_vae is not None else "no_audio_vae"}]}


class MF_SampleSegments:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "vae": ("VAE", {"tooltip": "H3 video VAE：編碼參考素材、逐段解碼畫面。"}),
                "segment_plan": ("MF_SEGMENT_PLAN",),
                "sampler": ("SAMPLER",),
                "sigmas": ("SIGMAS",),
                "noise_seed": ("INT", {
                    "default": 0, "min": 0, "max": 0xffffffffffffffff, "control_after_generate": True,
                    "tooltip": "所有段共用；時間軸上有指定 seed 的段改用自己的 seed。",
                }),
                "width": ("INT", {"default": 1344, "min": 32, "max": 8192, "step": 32}),
                "height": ("INT", {"default": 768, "min": 32, "max": 8192, "step": 32}),
                "mask": (["hard", "taper", "linear"], {
                    "default": "hard",
                    "tooltip": "續接區保留程度：hard 全保留；taper 放寬尾端；linear 逐步放寬。",
                }),
                "carry_audio": ("BOOLEAN", {"default": True, "tooltip": "H3 音訊續接；有鎖定原音落在續接區的段會自動略過。"}),
            },
            "optional": {
                "audio_vae": ("VAE", {"tooltip": "H3 audio VAE：解碼各段音訊、編碼參考音訊與鎖定原音。"}),
                "negative": ("CONDITIONING", {"tooltip": "接上才用 CFG（cfg 才生效）；不接時以 Basic Guider 取樣（turbo 模型）。"}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step": 0.1, "round": 0.01}),
                "ref_image_size": (["match", "max"], {"default": "match"}),
                "ref_video_short_edge": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 32,
                                          "tooltip": "同 Encode H3 Segment：0 = 自動縮到短邊 768；否則縮到短邊 ≤ 此值。"}),
                "video_taper_tokens": ("INT", {"default": 2, "min": 0, "max": 1000000, "step": 1}),
                "audio_taper_tokens": ("INT", {"default": 2, "min": 0, "max": 1000000, "step": 1}),
                "on_conflict": (["error", "carry_wins"], {"default": "error"}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("MF_SEGMENT_STATE", "STRING")
    RETURN_NAMES = ("state", "report")
    FUNCTION = "sample"
    CATEGORY = "MediaForge/AI"

    def sample(self, model, clip, vae, segment_plan, sampler, sigmas, noise_seed, width, height, mask, carry_audio,
               dynprompt=None, unique_id=None, audio_vae=None, negative=None, cfg=1.0, ref_image_size="match",
               ref_video_short_edge=0, video_taper_tokens=2, audio_taper_tokens=2, on_conflict="error"):
        from comfy_execution.graph_utils import GraphBuilder
        if not isinstance(segment_plan, dict) or segment_plan.get("type") != "MF_SEGMENT_PLAN" or segment_plan.get("version") != 2:
            raise ValueError(f"[{LABEL}] segment_plan 必須來自（新版）Edit Timeline，請重新執行 Edit Timeline。")
        if (segment_plan["mode"] != "generate" or segment_plan["effective_grid"] != H3_GRID
                or tuple(segment_plan["fps"]) != H3_FPS):
            raise ValueError(f"[{LABEL}] 只支援 H3：Edit Timeline 需為 generate 模式、grid = {H3_GRID}、fps = 24。"
                             "其他模型請用 loop 範例。")
        if audio_vae is None and any(needs_audio_vae(spec["materials"]) for spec in segment_plan["specs"]):
            raise ValueError(f"[{LABEL}] 時間軸上有音訊類素材（參考音訊 / 配對音軌 / 鎖定原音），請連接 audio_vae。")
        # socket 一律連回原上游：把物件塞成常數會讓 core 的快取 key 變成 Unhashable、永遠重算
        links = dynprompt.get_node(unique_id)["inputs"]
        graph = GraphBuilder()
        prev_smp = prev_app = None
        for spec in segment_plan["specs"]:
            i = spec["index"]
            audio = {"audio_vae": links["audio_vae"]} if audio_vae is not None else {}
            enc = graph.node("MF_EncodeH3Segment", f"enc_{i}", segment=segment_view(spec, "encode"),
                             clip=links["clip"], vae=links["vae"], width=width, height=height,
                             ref_image_size=ref_image_size, ref_video_short_edge=ref_video_short_edge, **audio)
            carry = {"previous_latent": prev_smp.out(0)} if spec["overlap_in"] else {}
            ext = graph.node("MF_ExtendLatent", f"ext_{i}", target_latent=enc.out(1),
                             segment=segment_view(spec, "extend"), mask=mask, carry_audio=carry_audio,
                             video_taper_tokens=video_taper_tokens, audio_taper_tokens=audio_taper_tokens,
                             experimental_causal=False, on_conflict=on_conflict, **carry)
            noise = graph.node("RandomNoise", f"noise_{i}",
                               noise_seed=spec["seed"] if spec["seed"] is not None else noise_seed)
            if negative is not None:
                guider = graph.node("CFGGuider", f"gd_{i}", model=links["model"], positive=enc.out(0),
                                    negative=links["negative"], cfg=cfg)
            else:
                guider = graph.node("BasicGuider", f"gd_{i}", model=links["model"], conditioning=enc.out(0))
            smp = graph.node("SamplerCustomAdvanced", f"smp_{i}", noise=noise.out(0), guider=guider.out(0),
                             sampler=links["sampler"], sigmas=links["sigmas"], latent_image=ext.out(0))
            state = {"state": prev_app.out(0)} if prev_app is not None else {}
            app = graph.node("MF_AppendSegment", f"app_{i}", segment=segment_view(spec, "assemble"),
                             latent=smp.out(0), vae=links["vae"], **audio, **state)
            prev_smp, prev_app = smp, app
        report = _report(segment_plan, noise_seed, audio_vae, negative, cfg, carry_audio, mask,
                         {"video": video_taper_tokens, "audio": audio_taper_tokens}, width, height, ref_video_short_edge)
        return {"result": (prev_app.out(0), json.dumps(report, ensure_ascii=False)), "expand": graph.finalize()}


NODE_CLASS_MAPPINGS = {"MF_SampleSegments": MF_SampleSegments}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SampleSegments": "🎛️ Sample Segments"}
