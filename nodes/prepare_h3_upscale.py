"""Internal tensor bookkeeping at an H3 resolution transition; no model inference."""
from ..utils.latent_carry import split_streams


class MF_PrepareH3Upscale:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"latent": ("LATENT",), "template": ("LATENT",)},
                "optional": {"previous_latent": ("LATENT",)}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "prepare"
    CATEGORY = "MediaForge/AI"
    DEPRECATED = True

    def prepare(self, latent, template, previous_latent=None):
        import torch
        streams, nested = split_streams(latent["samples"])
        out = {k: v for k, v in latent.items() if k not in ("noise_mask", "mf_carry", "mf_lock")}
        # Native upscaling drops noise_mask. Rebuild the high-grid video mask, retain
        # the original audio mask/lock, then ExtendLatent establishes high-grid carry.
        video_mask = torch.ones((1, 1, streams[0].shape[2], 1, 1), dtype=torch.float32, device=streams[0].device)
        if nested:
            audio_mask = torch.ones_like(streams[1], dtype=torch.float32)
            for a, b in template.get("mf_lock", {}).get("tokens", []):
                audio_mask[..., a:b] = 0
            out["noise_mask"] = type(latent["samples"])((video_mask, audio_mask))
        else:
            out["noise_mask"] = video_mask
        if "mf_lock" in template:
            out["mf_lock"] = template["mf_lock"]
            audio = streams[1].clone()
            for (a, b), value in zip(template["mf_lock"]["tokens"], template["mf_lock"]["target"]):
                audio[..., a:b] = value.to(audio)
            out["samples"] = type(latent["samples"])((streams[0], audio))
        if previous_latent is not None and "selflift_low_resolution_carry" in previous_latent:
            out["selflift_previous_low_resolution_carry"] = previous_latent["selflift_low_resolution_carry"]
        return (out,)


NODE_CLASS_MAPPINGS = {"MF_PrepareH3Upscale": MF_PrepareH3Upscale}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_PrepareH3Upscale": "🧩 Prepare H3 Upscale"}
