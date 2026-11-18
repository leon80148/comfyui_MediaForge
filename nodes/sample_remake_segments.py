"""Edit Timeline → official Viggle Remake nodes → existing MediaForge assembly.

Keep model/LoRA loading outside this node. Sampling is only graph expansion; the official plugin owns
its conditioning, noise and model wrapper. Standard MF_SampleSegments is untouched.
"""
import json
import math

from ..utils.remake_adapter import LABEL, SIGMAS, canvas, remake_nodes, validate_segment
from ..utils.timeline_plan import H3_FPS, H3_GRID, segment_view
from ..utils.timeline_render import parse_render, validate_runtime


class MF_SampleRemakeSegments:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "fl2va + Viggle LoRA（Bypass Loader），或已合併的 Remake int8／GGUF。mode 必須與模型相符。"}),
                "clip": ("CLIP",), "vae": ("VAE",), "audio_vae": ("VAE",),
                "segment_plan": ("MF_SEGMENT_PLAN",),
                "mode": (["swap", "edit"], {"default": "swap", "tooltip": "未接 sigmas 時使用此模式的官方六步排程；不會自動更換模型或 LoRA。"}),
                "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "control_after_generate": True}),
                "short_edge": ([480, 736], {"default": 480, "tooltip": "未啟用 Timeline 解析度時使用：16:9 對應 832×480／1280×736，直式對調。啟用後以 Timeline 的目標／基礎尺寸為準。"}),
                "keep_source_audio": ("BOOLEAN", {"default": True, "tooltip": "將來源音軌編入乾淨 audio latent，整段不去噪；輸出經 audio VAE 重建。關閉時使用靜音。"}),
                "keyframe_until": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 1.0, "step": 0.01}),
            },
            "optional": {
                "sampler": ("SAMPLER", {"tooltip": "可選：接 KSamplerSelect 覆蓋預設 Euler；請配合模型／LoRA 的建議。"}),
                "sigmas": ("SIGMAS", {"tooltip": "可選：接 ManualSigmas 或相容排程。總步數由排程決定；不接使用官方六步。二階段共用此排程，不額外增加步數。"}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("MF_SEGMENT_STATE", "STRING")
    RETURN_NAMES = ("state", "report")
    FUNCTION = "sample"
    CATEGORY = "MediaForge/AI"

    def sample(self, model, clip, vae, audio_vae, segment_plan, mode, noise_seed, short_edge,
               keep_source_audio, keyframe_until, dynprompt=None, unique_id=None, sampler=None, sigmas=None):
        from comfy_execution.graph_utils import GraphBuilder

        if (not isinstance(segment_plan, dict) or segment_plan.get("type") != "MF_SEGMENT_PLAN"
                or segment_plan.get("version") != 2 or segment_plan.get("mode") != "generate"
                or segment_plan.get("effective_grid") != H3_GRID or tuple(segment_plan.get("fps", ())) != H3_FPS):
            raise ValueError(f"[{LABEL}] 請連接新版 Edit Timeline，設為 generate、24 fps。")
        if mode not in SIGMAS or short_edge not in (480, 736) or not 0 <= keyframe_until <= 1:
            raise ValueError(f"[{LABEL}] mode／short_edge／keyframe_until 不合法：mode 必須是 swap 或 edit，short_edge 必須是 480 或 736，keyframe_until 必須介於 0 與 1。")
        recipe = parse_render(segment_plan["render"]) if "render" in segment_plan else None
        strategy = recipe["mode"] if recipe else "direct"
        if strategy == "selflift":
            raise ValueError(f"[{LABEL}] Remake 支援直接生成、Latent 模型放大與二階段生成；尚未支援 SelfLift，請改選其他生成方式。")
        custom_sampler, custom_sigmas = sampler is not None, sigmas is not None
        try:
            if custom_sigmas and getattr(sigmas, "ndim", 1) != 1:
                raise ValueError("expected a one-dimensional schedule")
            schedule = [float(x) for x in sigmas] if custom_sigmas else [float(x) for x in SIGMAS[mode].split(",")]
        except (TypeError, ValueError, RuntimeError) as e:
            raise ValueError(f"[{LABEL}] sigmas 必須是一維數值排程。") from e
        if (len(schedule) < 2 or not all(math.isfinite(x) and x >= 0 for x in schedule)
                or schedule[0] <= 0 or schedule[-1] != 0
                or any(a <= b for a, b in zip(schedule, schedule[1:]))):
            raise ValueError(f"[{LABEL}] sigmas 需至少兩個有限非負值、嚴格遞減並以 0 結束。")
        if recipe:
            validate_runtime(recipe, schedule, sampler, label=LABEL)
        specs = segment_plan["specs"]
        if not specs:
            raise ValueError(f"[{LABEL}] 時間軸至少需要一段。")
        sizes, pins = [], []
        for spec in specs:
            pins.append(validate_segment(segment_view(spec, "encode")))
            sizes.append(canvas(spec["materials"]["videos"][0]["snapshot"], short_edge,
                                recipe["width"] if recipe else 0, recipe["height"] if recipe else 0))
        if len(set(sizes)) != 1:
            raise ValueError(f"[{LABEL}] 各段來源必須產生相同畫布尺寸，請先統一影片方向與比例。")
        remake_nodes()  # Fail before expensive encodes when the optional plugin is absent/incompatible.
        width, height = sizes[0]
        low_width, low_height = ((recipe["base_width"], recipe["base_height"])
                                 if strategy in ("latent", "two_stage") else (width, height))
        links = dynprompt.get_node(unique_id)["inputs"]
        graph = GraphBuilder()
        wrapped = graph.node("RemakeKeyframeUntil", "remake_model", model=links["model"], keyframe_until=keyframe_until)
        sampler_link = links["sampler"] if custom_sampler else graph.node(
            "KSamplerSelect", "remake_sampler", sampler_name="euler").out(0)
        sigmas_link = links["sigmas"] if custom_sigmas else graph.node(
            "ManualSigmas", "remake_sigmas", sigmas=SIGMAS[mode]).out(0)
        upmodel = split = None
        if strategy in ("latent", "two_stage"):
            upmodel = graph.node("LatentUpscaleModelLoader", "upscale_model", model_name=recipe["model"])
        if strategy == "two_stage":
            split = graph.node("SplitSigmas", "split_sigmas", sigmas=sigmas_link, step=len(schedule) - 1 - recipe["high_steps"])
        previous = previous_high = state = None
        for spec in specs:
            i = spec["index"]
            enc = graph.node("MF_EncodeRemakeSegment", f"enc_{i}", segment=segment_view(spec, "encode"),
                             clip=links["clip"], vae=links["vae"], audio_vae=links["audio_vae"],
                             short_edge=short_edge, keep_source_audio=keep_source_audio,
                             **({"width": low_width, "height": low_height} if recipe else {}))
            carry = {"previous_latent": previous.out(0)} if spec["overlap_in"] else {}
            ext = graph.node("MF_ExtendLatent", f"ext_{i}", target_latent=enc.out(1),
                             segment=segment_view(spec, "extend"), mask="hard", carry_audio=False, **carry)
            noise = graph.node("RemakeNoise", f"noise_{i}", positive=enc.out(0),
                               seed=spec["seed"] if spec["seed"] is not None else noise_seed)
            guider = graph.node("BasicGuider", f"gd_{i}", model=wrapped.out(0), conditioning=enc.out(0))
            sampled = graph.node("SamplerCustomAdvanced", f"smp_{i}", noise=noise.out(0), guider=guider.out(0),
                                 sampler=sampler_link, sigmas=split.out(0) if split else sigmas_link, latent_image=ext.out(0))
            final = sampled
            if upmodel:
                lifted = graph.node("MiniMaxH3LatentUpscaleWithModel", f"lift_{i}", model=upmodel.out(0),
                                    samples=sampled.out(1) if split else sampled.out(0), width=width, height=height)
                template = enc
                if split:
                    template = graph.node("MF_EncodeRemakeSegment", f"hi_enc_{i}", segment=segment_view(spec, "encode"),
                                          clip=links["clip"], vae=links["vae"], audio_vae=links["audio_vae"],
                                          short_edge=short_edge, keep_source_audio=keep_source_audio, width=width, height=height)
                prepared = graph.node("MF_PrepareH3Upscale", f"prepare_hi_{i}", latent=lifted.out(0),
                                      template=template.out(1), preserve_audio=True)
                high_carry = {"previous_latent": previous_high.out(0)} if spec["overlap_in"] else {}
                high = graph.node("MF_ExtendLatent", f"hi_ext_{i}", target_latent=prepared.out(0),
                                  segment=segment_view(spec, "extend"), mask="hard", carry_audio=False, **high_carry)
                final = high
                if split:
                    # Remake's RNG skips one draw per reference latent. The high stage
                    # must derive those shapes from its own re-encoded conditioning.
                    high_noise = graph.node("RemakeNoise", f"hi_noise_{i}", positive=template.out(0),
                                            seed=spec["seed"] if spec["seed"] is not None else noise_seed)
                    high_guider = graph.node("BasicGuider", f"hi_gd_{i}", model=wrapped.out(0), conditioning=template.out(0))
                    final = graph.node("SamplerCustomAdvanced", f"hi_smp_{i}", noise=high_noise.out(0), guider=high_guider.out(0),
                                       sampler=sampler_link, sigmas=split.out(1), latent_image=high.out(0))
            # Both windows reconstruct the same source track; use a linear blend for auto audio seams.
            assembly = segment_view(spec, "assemble")
            if assembly["seam_in"]["kind"] == "crossfade" and assembly["seam_in"].get("audio_curve") == "auto":
                assembly["seam_in"] = {**assembly["seam_in"], "audio_curve": "linear"}
            parent = {"state": state.out(0)} if state is not None else {}
            state = graph.node("MF_AppendSegment", f"app_{i}", segment=assembly, latent=final.out(0),
                               vae=links["vae"], audio_vae=links["audio_vae"], **parent)
            if split:
                clean = graph.node("MF_PrepareH3Upscale", f"low_tail_{i}", latent=sampled.out(1), template=enc.out(1), preserve_audio=True)
                previous = graph.node("MF_CopyH3Provenance", f"low_marker_{i}", latent=clean.out(0), source=ext.out(0))
            else:
                previous = sampled
            previous_high = final
        report = {"node": LABEL, "mode": mode,
                  "sigmas": ", ".join(str(x) for x in schedule) if custom_sigmas else SIGMAS[mode],
                  "sampler": getattr(getattr(sampler, "sampler_function", None), "__name__", "external") if custom_sampler else "euler",
                  "schedule_source": "external" if custom_sigmas else "official",
                  "sampler_source": "external" if custom_sampler else "default",
                  "total_steps": len(schedule) - 1, "cfg": 1.0,
                  "stage_steps": {"base": len(schedule) - 1 - (recipe["high_steps"] if split else 0),
                                  "high": recipe["high_steps"] if split else 0},
                  "width": width, "height": height, "keyframe_until": keyframe_until,
                  "audio": "source_reconstructed_by_vae" if keep_source_audio else "silence",
                  "continuation": "mediaforge_hard_video_latent_carry", "audio_carry": False,
                  "tail_policy": segment_plan.get("remake_tail_policy", "strict"),
                  "total_frames": segment_plan["total_frames"],
                  "segments": [{"index": s["index"], "frames": s["length"], "pins": pins[i],
                                "output_frames": s.get("output_length", s["length"]),
                                "padded_frames": s["length"] - s.get("output_length", s["length"]),
                                "seed": s["seed"] if s["seed"] is not None else noise_seed}
                               for i, s in enumerate(specs)]}
        if recipe:
            report["render"] = {**recipe, "base_width": low_width, "base_height": low_height,
                                "total_steps": len(schedule) - 1, "size_source": "timeline", "source_fit": "cover"}
        return {"result": (state.out(0), json.dumps(report, ensure_ascii=False)), "expand": graph.finalize()}


NODE_CLASS_MAPPINGS = {"MF_SampleRemakeSegments": MF_SampleRemakeSegments}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SampleRemakeSegments": "🎭 Sample Remake Segments"}
