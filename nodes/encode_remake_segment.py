"""Internal material decoder for MF_SampleRemakeSegments."""
from ..utils.remake_adapter import encode_remake


class MF_EncodeRemakeSegment:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "segment": ("MF_SEGMENT",), "clip": ("CLIP",), "vae": ("VAE",), "audio_vae": ("VAE",),
            "short_edge": ([480, 736],), "keep_source_audio": ("BOOLEAN", {"default": True}),
        }}

    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("positive", "latent")
    FUNCTION = "encode"
    CATEGORY = "MediaForge/AI"
    DEPRECATED = True

    def encode(self, segment, clip, vae, audio_vae, short_edge, keep_source_audio):
        return encode_remake(segment, clip, vae, audio_vae, short_edge, keep_source_audio)


NODE_CLASS_MAPPINGS = {"MF_EncodeRemakeSegment": MF_EncodeRemakeSegment}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_EncodeRemakeSegment": "🎞️ Encode Remake Segment"}
