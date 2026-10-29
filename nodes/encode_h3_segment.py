"""MF_EncodeH3Segment — 把一段的 prompt 與素材（參考 / 錨定 / 鎖音）編成 H3 conditioning + 空 AV latent。

conditioning 只透過 core 的 MiniMaxH3ReferenceToVideo / MiniMaxH3AddGuide（utils/h3_adapter.py）；
參考素材只在這裡短暫解碼，不成為快取中間值。loop 路徑接 MF_SelectSegment，單節點路徑由
MF_SampleSegments 在 expansion 裡自動使用。
"""
from ..utils.h3_adapter import encode_segment


class MF_EncodeH3Segment:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "segment": ("MF_SEGMENT",),
                "clip": ("CLIP",),
                "vae": ("VAE", {"tooltip": "H3 video VAE：編碼參考圖片 / 影片與錨定畫面。"}),
                "width": ("INT", {"default": 1344, "min": 32, "max": 8192, "step": 32}),
                "height": ("INT", {"default": 768, "min": 32, "max": 8192, "step": 32}),
                "ref_image_size": (["match", "max"], {
                    "default": "match",
                    "tooltip": "match：參考圖縮到生成畫面的面積（較快）；max：短邊 2048（較準、慢數倍）。",
                }),
            },
            "optional": {
                "audio_vae": ("VAE", {"tooltip": "H3 audio VAE：參考音訊、配對音軌與鎖定原音需要。"}),
                "ref_video_short_edge": ("INT", {
                    "default": 0, "min": 0, "max": 4096, "step": 32,
                    "tooltip": "0 = 交給 core 決定；否則先把參考影片縮到短邊 ≤ 此值，降低參考 token 與記憶體。",
                }),
            },
        }

    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("positive", "latent")
    FUNCTION = "encode"
    CATEGORY = "MediaForge/AI"

    def encode(self, segment, clip, vae, width, height, ref_image_size, audio_vae=None, ref_video_short_edge=0):
        return encode_segment(segment, clip, vae, width, height, ref_image_size, audio_vae, ref_video_short_edge)


NODE_CLASS_MAPPINGS = {"MF_EncodeH3Segment": MF_EncodeH3Segment}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_EncodeH3Segment": "🎬 Encode H3 Segment"}
