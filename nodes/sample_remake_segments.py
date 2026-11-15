"""Edit Timeline → official Viggle Remake nodes → existing MediaForge assembly.

Keep model/LoRA loading outside this node. Sampling is only graph expansion; the official plugin owns
its conditioning, noise and model wrapper. Standard MF_SampleSegments is untouched.
"""
import json

from ..utils.remake_adapter import LABEL, SIGMAS, canvas, remake_nodes, validate_segment
from ..utils.timeline_plan import H3_FPS, H3_GRID, segment_view


class MF_SampleRemakeSegments:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "fl2va + Viggle LoRA（Bypass Loader），或已合併的 Remake int8／GGUF。mode 必須與模型相符。"}),
                "clip": ("CLIP",), "vae": ("VAE",), "audio_vae": ("VAE",),
                "segment_plan": ("MF_SEGMENT_PLAN",),
                "mode": (["swap", "edit"], {"default": "swap", "tooltip": "選擇官方六步 sigmas；不會自動更換上游模型或 LoRA。"}),
                "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "control_after_generate": True}),
                "short_edge": ([480, 736], {"default": 480, "tooltip": "16:9 影片對應 832×480／1280×736；直式影片對調寬高。"}),
                "keep_source_audio": ("BOOLEAN", {"default": True, "tooltip": "將來源音軌編入乾淨 audio latent，整段不去噪；輸出經 audio VAE 重建。關閉時使用靜音。"}),
                "keyframe_until": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 1.0, "step": 0.01}),
            },
            "hidden": {"dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("MF_SEGMENT_STATE", "STRING")
    RETURN_NAMES = ("state", "report")
    FUNCTION = "sample"
    CATEGORY = "MediaForge/AI"

    def sample(self, model, clip, vae, audio_vae, segment_plan, mode, noise_seed, short_edge,
               keep_source_audio, keyframe_until, dynprompt=None, unique_id=None):
        from comfy_execution.graph_utils import GraphBuilder

        if (not isinstance(segment_plan, dict) or segment_plan.get("type") != "MF_SEGMENT_PLAN"
                or segment_plan.get("version") != 2 or segment_plan.get("mode") != "generate"
                or segment_plan.get("effective_grid") != H3_GRID or tuple(segment_plan.get("fps", ())) != H3_FPS):
            raise ValueError(f"[{LABEL}] 請連接新版 Edit Timeline，設為 generate、24 fps。")
        if mode not in SIGMAS or short_edge not in (480, 736) or not 0 <= keyframe_until <= 1:
            raise ValueError(f"[{LABEL}] mode／short_edge／keyframe_until 不合法。")
        if segment_plan.get("render", {}).get("mode", "direct") != "direct":
            raise ValueError(f"[{LABEL}] 目前僅支援直接生成；請停用 Timeline 的 latent 放大／SelfLift。尺寸由本節點 short_edge 設定。")
        specs = segment_plan["specs"]
        if not specs:
            raise ValueError(f"[{LABEL}] 時間軸至少需要一段。")
        sizes, pins = [], []
        for spec in specs:
            pins.append(validate_segment(segment_view(spec, "encode")))
            sizes.append(canvas(spec["materials"]["videos"][0]["snapshot"], short_edge))
        if len(set(sizes)) != 1:
            raise ValueError(f"[{LABEL}] 各段來源必須產生相同畫布尺寸，請先統一影片方向與比例。")
        remake_nodes()  # Fail before expensive encodes when the optional plugin is absent/incompatible.
        links = dynprompt.get_node(unique_id)["inputs"]
        graph = GraphBuilder()
        wrapped = graph.node("RemakeKeyframeUntil", "remake_model", model=links["model"], keyframe_until=keyframe_until)
        sampler = graph.node("KSamplerSelect", "remake_sampler", sampler_name="euler")
        sigmas = graph.node("ManualSigmas", "remake_sigmas", sigmas=SIGMAS[mode])
        previous = state = None
        for spec in specs:
            i = spec["index"]
            enc = graph.node("MF_EncodeRemakeSegment", f"enc_{i}", segment=segment_view(spec, "encode"),
                             clip=links["clip"], vae=links["vae"], audio_vae=links["audio_vae"],
                             short_edge=short_edge, keep_source_audio=keep_source_audio)
            carry = {"previous_latent": previous.out(0)} if spec["overlap_in"] else {}
            ext = graph.node("MF_ExtendLatent", f"ext_{i}", target_latent=enc.out(1),
                             segment=segment_view(spec, "extend"), mask="hard", carry_audio=False, **carry)
            noise = graph.node("RemakeNoise", f"noise_{i}", positive=enc.out(0),
                               seed=spec["seed"] if spec["seed"] is not None else noise_seed)
            guider = graph.node("BasicGuider", f"gd_{i}", model=wrapped.out(0), conditioning=enc.out(0))
            previous = graph.node("SamplerCustomAdvanced", f"smp_{i}", noise=noise.out(0), guider=guider.out(0),
                                  sampler=sampler.out(0), sigmas=sigmas.out(0), latent_image=ext.out(0))
            # Both windows reconstruct the same source track; use a linear blend for auto audio seams.
            assembly = segment_view(spec, "assemble")
            if assembly["seam_in"]["kind"] == "crossfade" and assembly["seam_in"].get("audio_curve") == "auto":
                assembly["seam_in"] = {**assembly["seam_in"], "audio_curve": "linear"}
            parent = {"state": state.out(0)} if state is not None else {}
            state = graph.node("MF_AppendSegment", f"app_{i}", segment=assembly, latent=previous.out(0),
                               vae=links["vae"], audio_vae=links["audio_vae"], **parent)
        report = {"node": LABEL, "mode": mode, "sigmas": SIGMAS[mode], "sampler": "euler", "cfg": 1.0,
                  "width": sizes[0][0], "height": sizes[0][1], "keyframe_until": keyframe_until,
                  "audio": "source_reconstructed_by_vae" if keep_source_audio else "silence",
                  "continuation": "mediaforge_hard_video_latent_carry", "audio_carry": False,
                  "segments": [{"index": s["index"], "frames": s["length"], "pins": pins[i],
                                "seed": s["seed"] if s["seed"] is not None else noise_seed}
                               for i, s in enumerate(specs)]}
        return {"result": (state.out(0), json.dumps(report, ensure_ascii=False)), "expand": graph.finalize()}


NODE_CLASS_MAPPINGS = {"MF_SampleRemakeSegments": MF_SampleRemakeSegments}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SampleRemakeSegments": "🎭 Sample Remake Segments"}
