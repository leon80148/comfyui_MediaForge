import os

from ..utils.color import hex_to_ass_color
from ..utils.ffmpeg import ensure_ffmpeg, escape_filter_path, run_ffmpeg


class MF_BurnSubtitle:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                "srt_path": ("STRING", {"default": "input/sample.srt"}),
                "output_path": ("STRING", {"default": "output/subtitled.mp4"}),

                # 字體與顏色
                "font_name": ("STRING", {"default": "Microsoft JhengHei"}),
                "font_size": ("INT", {"default": 24, "min": 8, "max": 150}),
                "font_color_hex": ("STRING", {"default": "#FFFFFF"}),
                "bold": ("BOOLEAN", {"default": True}),

                # 邊框與陰影
                "outline_color_hex": ("STRING", {"default": "#000000"}),
                "outline_width": ("INT", {"default": 2, "min": 0, "max": 10}),
                "shadow_depth": ("INT", {"default": 1, "min": 0, "max": 10}),
                "border_style": ("INT", {"default": 1, "min": 1, "max": 3, "step": 2}),
                "back_color_hex": ("STRING", {"default": "#000000"}),

                # 排版與寬度控制
                "alignment": ("INT", {"default": 2, "min": 1, "max": 9}),
                "margin_v": ("INT", {"default": 20, "min": 0, "max": 500}),
                "margin_lr": ("INT", {"default": 50, "min": 0, "max": 1000}),

                # 影片輸出設定
                "target_fps": ("INT", {"default": 0, "min": 0, "max": 120, "step": 1}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "burn"
    CATEGORY = "MediaForge/Subtitle"

    def burn(self, video_path, srt_path, output_path, font_name, font_size,
             font_color_hex, bold, outline_color_hex, outline_width, shadow_depth,
             border_style, back_color_hex, alignment, margin_v, margin_lr,
             target_fps):

        if not ensure_ffmpeg():
            raise RuntimeError("[Burn Subtitle] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"[Burn Subtitle] 找不到影片：{video_path}")
        if not os.path.exists(srt_path):
            raise FileNotFoundError(f"[Burn Subtitle] 找不到字幕：{srt_path}")

        primary = hex_to_ass_color(font_color_hex)
        outline = hex_to_ass_color(outline_color_hex)
        # 設定底色半透明(80)，讓畫面不會被色塊完全遮蓋
        back = hex_to_ass_color(back_color_hex, alpha="80" if border_style == 3 else "00")
        bold_val = -1 if bold else 0

        style = (
            f"Fontname={font_name},Fontsize={font_size},"
            f"PrimaryColour={primary},OutlineColour={outline},"
            f"BackColour={back},Bold={bold_val},"
            f"BorderStyle={border_style},Outline={outline_width},Shadow={shadow_depth},"
            f"Alignment={alignment},MarginV={margin_v},MarginL={margin_lr},MarginR={margin_lr}"
        )

        command = ["ffmpeg", "-y", "-i", video_path]
        if target_fps > 0:
            command.extend(["-r", str(target_fps)])
        # R10 P2 fix：`-c:a copy` 在跨容器 (e.g., MKV/WebM 來源 → MP4 output) 會 mux fail。
        # 這個節點本來就重編 video，audio 也統一轉 AAC（mp4/mov/mkv 都吃），安全性 > 一點點 audio quality loss。
        command.extend([
            "-vf", f"subtitles={escape_filter_path(srt_path)}:force_style='{style}'",
            "-c:a", "aac", "-b:a", "192k",
            output_path,
        ])

        if not run_ffmpeg(command, tag="Burn Subtitle"):
            raise RuntimeError("[Burn Subtitle] FFmpeg 燒字幕失敗，請查看上方 stderr 輸出。")
        print(f"[Burn Subtitle] 輸出成功: {output_path}")
        return (output_path,)


NODE_CLASS_MAPPINGS = {"MF_BurnSubtitle": MF_BurnSubtitle}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_BurnSubtitle": "🔥 Burn Subtitle"}
