"""Internal marker transfer for the native low-grid clean prediction."""


class MF_CopyH3Provenance:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"latent": ("LATENT",), "source": ("LATENT",)}}

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "copy"
    CATEGORY = "MediaForge/AI"
    DEPRECATED = True

    def copy(self, latent, source):
        return ({**latent, "mf_carry": source["mf_carry"]},)


NODE_CLASS_MAPPINGS = {"MF_CopyH3Provenance": MF_CopyH3Provenance}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_CopyH3Provenance": "🧾 Copy H3 Provenance"}
