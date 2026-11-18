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
from ..utils.timeline_render import parse_render, validate_runtime

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
                "width": ("INT", {"default": 1344, "min": 32, "max": 8192, "step": 32,
                                  "tooltip": "備用生成寬度；Edit Timeline 啟用解析度管理時，以 Timeline 尺寸為準。"}),
                "height": ("INT", {"default": 768, "min": 32, "max": 8192, "step": 32,
                                   "tooltip": "備用生成高度；Edit Timeline 啟用解析度管理時，以 Timeline 尺寸為準。"}),
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
                                          "tooltip": "備用參考影片短邊上限；Timeline 啟用解析度管理時，以其設定為準。0 = 自動短邊 768。"}),
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
        if isinstance(segment_plan, dict) and segment_plan.get("remake_tail_policy") == "pad_trim":
            raise ValueError(f"[{LABEL}] Remake 尾段補幀只適用於 Sample Remake Segments：請到 Edit Timeline 的「解析度與放大」取消勾選「Remake 尾段補幀並裁回來源」，或改接 Sample Remake Segments。")
        if not isinstance(segment_plan, dict) or segment_plan.get("type") != "MF_SEGMENT_PLAN" or segment_plan.get("version") != 2:
            raise ValueError(f"[{LABEL}] segment_plan 必須來自（新版）Edit Timeline，請重新執行 Edit Timeline。")
        if (segment_plan["mode"] != "generate" or segment_plan["effective_grid"] != H3_GRID
                or tuple(segment_plan["fps"]) != H3_FPS):
            raise ValueError(f"[{LABEL}] 只支援 MiniMax H3：請把 Edit Timeline 設為 generate 模式、fps = 24。若要剪現有影片，請改接 Trim by Ranges。")
        if audio_vae is None and any(needs_audio_vae(spec["materials"]) for spec in segment_plan["specs"]):
            raise ValueError(f"[{LABEL}] 時間軸上有音訊類素材（參考音訊 / 配對音軌 / 鎖定原音），請連接 audio_vae。")
        # socket 一律連回原上游：把物件塞成常數會讓 core 的快取 key 變成 Unhashable、永遠重算
        links = dynprompt.get_node(unique_id)["inputs"]
        recipe = parse_render(segment_plan["render"]) if "render" in segment_plan else None
        strategy = recipe["mode"] if recipe else "direct"
        if recipe:
            width, height = recipe["width"], recipe["height"]
            ref_video_short_edge = recipe["ref_video_short_edge"]
            validate_runtime(recipe, sigmas, sampler)
        low_width, low_height = ((recipe["base_width"], recipe["base_height"])
                                 if strategy in ("latent", "two_stage") else (width, height))
        graph = GraphBuilder()
        prev_smp = prev_app = prev_final = None
        upmodel = None
        if strategy in ("latent", "two_stage"):
            upmodel = graph.node("LatentUpscaleModelLoader", "upscale_model", model_name=recipe["model"])
        split = None
        if strategy == "two_stage":
            split = graph.node("SplitSigmas", "split_sigmas", sigmas=links["sigmas"], step=len(sigmas) - 1 - recipe["high_steps"])
        for spec in segment_plan["specs"]:
            i = spec["index"]
            audio = {"audio_vae": links["audio_vae"]} if audio_vae is not None else {}
            enc = graph.node("MF_EncodeH3Segment", f"enc_{i}", segment=segment_view(spec, "encode"),
                             clip=links["clip"], vae=links["vae"], width=low_width, height=low_height,
                             ref_image_size=ref_image_size, ref_video_short_edge=ref_video_short_edge, **audio)
            target = enc.out(1)
            if strategy == "selflift":
                previous = {"previous_latent": prev_final.out(0)} if spec["overlap_in"] else {}
                prepared = graph.node("MF_PrepareH3Upscale", f"selflift_context_{i}", latent=target, template=target, **previous)
                target = prepared.out(0)
            carry = {"previous_latent": prev_smp.out(0)} if spec["overlap_in"] else {}
            ext = graph.node("MF_ExtendLatent", f"ext_{i}", target_latent=target,
                             segment=segment_view(spec, "extend"), mask=mask, carry_audio=carry_audio,
                             video_taper_tokens=video_taper_tokens, audio_taper_tokens=audio_taper_tokens,
                             on_conflict=on_conflict, **carry)
            noise = graph.node("RandomNoise", f"noise_{i}",
                               noise_seed=spec["seed"] if spec["seed"] is not None else noise_seed)
            if negative is not None:
                guider = graph.node("CFGGuider", f"gd_{i}", model=links["model"], positive=enc.out(0),
                                    negative=links["negative"], cfg=cfg)
            else:
                guider = graph.node("BasicGuider", f"gd_{i}", model=links["model"], conditioning=enc.out(0))
            if strategy == "selflift":
                continuation = graph.node("MiniMaxH3FiniteLatentContinuation", f"selflift_cont_{i}",
                                          positive=enc.out(0), target_latent=ext.out(0), iteration=i,
                                          overlap_frames=spec["overlap_in"],
                                          continue_audio_latent=carry_audio and not bool(spec["carry_conflicts"]),
                                          model=links["model"], sigmas=links["sigmas"], **carry)
                smp = graph.node("MiniMaxH3TimelineSelfLiftSampler", f"smp_{i}", model=continuation.out(3),
                                 positive=continuation.out(0), negative=links["negative"] if negative is not None else continuation.out(0),
                                 vae=links["vae"], latent_image=continuation.out(1), sampler=links["sampler"], sigmas=links["sigmas"],
                                 seed=spec["seed"] if spec["seed"] is not None else noise_seed,
                                 cfg=cfg if negative is not None else 1.0,
                                 transition_step=len(sigmas) - 1 - recipe["high_steps"], lowres_scale=0.5,
                                 rho=0.0, w_min=0.5, w_max=1.0, upscaler_model=recipe["model"])
            else:
                smp = graph.node("SamplerCustomAdvanced", f"smp_{i}", noise=noise.out(0), guider=guider.out(0),
                                 sampler=links["sampler"], sigmas=split.out(0) if split else links["sigmas"], latent_image=ext.out(0))
            final = smp
            if strategy == "selflift":
                # Drift-Control uses dynamic prefix masks. Re-establish MediaForge's
                # explicit final-grid seam contract before spilling / passing the tail.
                prepared = graph.node("MF_PrepareH3Upscale", f"selflift_final_{i}", latent=smp.out(0), template=enc.out(1))
                final = graph.node("MF_ExtendLatent", f"selflift_seam_{i}", target_latent=prepared.out(0),
                                   segment=segment_view(spec, "extend"), mask=mask, carry_audio=carry_audio,
                                   video_taper_tokens=video_taper_tokens, audio_taper_tokens=audio_taper_tokens,
                                   on_conflict=on_conflict, **carry)
            if upmodel:
                lifted = graph.node("MiniMaxH3LatentUpscaleWithModel", f"lift_{i}", model=upmodel.out(0),
                                    samples=smp.out(1) if split else smp.out(0), width=width, height=height)
                template = enc
                if split:
                    template = graph.node("MF_EncodeH3Segment", f"hi_enc_{i}", segment=segment_view(spec, "encode"),
                                          clip=links["clip"], vae=links["vae"], width=width, height=height,
                                          ref_image_size=ref_image_size, ref_video_short_edge=ref_video_short_edge, **audio)
                prepared = graph.node("MF_PrepareH3Upscale", f"prepare_hi_{i}", latent=lifted.out(0), template=template.out(1))
                high_carry = {"previous_latent": prev_final.out(0)} if spec["overlap_in"] else {}
                high = graph.node("MF_ExtendLatent", f"hi_ext_{i}", target_latent=prepared.out(0),
                                  segment=segment_view(spec, "extend"), mask=mask, carry_audio=carry_audio,
                                  video_taper_tokens=video_taper_tokens, audio_taper_tokens=audio_taper_tokens,
                                  on_conflict=on_conflict, **high_carry)
                final = high
                if split:
                    if negative is not None:
                        high_guider = graph.node("CFGGuider", f"hi_gd_{i}", model=links["model"], positive=template.out(0),
                                                 negative=links["negative"], cfg=cfg)
                    else:
                        high_guider = graph.node("BasicGuider", f"hi_gd_{i}", model=links["model"], conditioning=template.out(0))
                    final = graph.node("SamplerCustomAdvanced", f"hi_smp_{i}", noise=noise.out(0), guider=high_guider.out(0),
                                       sampler=links["sampler"], sigmas=split.out(1), latent_image=high.out(0))
            state = {"state": prev_app.out(0)} if prev_app is not None else {}
            app = graph.node("MF_AppendSegment", f"app_{i}", segment=segment_view(spec, "assemble"),
                             latent=final.out(0), vae=links["vae"], **audio, **state)
            # For a partial low schedule, output 1 is its clean prediction; use it
            # for native low-grid continuation rather than the residual-noise state.
            if split:
                prev_smp = graph.node("MF_PrepareH3Upscale", f"low_tail_{i}", latent=smp.out(1), template=enc.out(1))
                # Extend's provenance marker belongs to this segment at both grids.
                prev_smp = graph.node("MF_CopyH3Provenance", f"low_marker_{i}", latent=prev_smp.out(0), source=ext.out(0))
            else:
                prev_smp = final if strategy == "selflift" else smp
            prev_final, prev_app = final, app
        report = _report(segment_plan, noise_seed, audio_vae, negative, cfg, carry_audio, mask,
                         {"video": video_taper_tokens, "audio": audio_taper_tokens}, width, height, ref_video_short_edge)
        if recipe:
            report["render"] = {**recipe, "total_steps": len(sigmas) - 1,
                                "base_width": width // 2 if strategy == "selflift" else low_width,
                                "base_height": height // 2 if strategy == "selflift" else low_height}
        return {"result": (prev_app.out(0), json.dumps(report, ensure_ascii=False)), "expand": graph.finalize()}


NODE_CLASS_MAPPINGS = {"MF_SampleSegments": MF_SampleSegments}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SampleSegments": "🎛️ Sample Segments"}
