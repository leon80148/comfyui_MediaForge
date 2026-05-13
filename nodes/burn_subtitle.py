import os

from ..utils.color import hex_to_ass_color
from ..utils.encoder import build_encoder_args, get_available_codecs, pick_default_codec
from ..utils.ffmpeg import ensure_ffmpeg, escape_filter_path, probe, run_ffmpeg
from ..utils.output_path import output_path_to_ui_entry, resolve_output_path
from ..utils.video_io import encode_tensor_to_tempfile, write_audio_dict_to_wav


# Plugin root = nodes/.. — used to locate the font/ subdirectory
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FONT_DIR = os.path.join(_PLUGIN_DIR, "font")

# ASS Alignment numpad keys: 1=BL, 2=BC, 3=BR, 4=ML, 5=MC, 6=MR, 7=TL, 8=TC, 9=TR
_ALIGNMENT_MAP = {
    "bottom_left (1)": 1,
    "bottom_center (2)": 2,
    "bottom_right (3)": 3,
    "middle_left (4)": 4,
    "middle_center (5)": 5,
    "middle_right (6)": 6,
    "top_left (7)": 7,
    "top_center (8)": 8,
    "top_right (9)": 9,
}

# 沒任何字型可選時 dropdown 不能空，放個 placeholder + burn() 時 raise 帶友善訊息
_NO_FONTS_PLACEHOLDER = "(把 .ttf / .otf / .ttc 丟進 plugin/font/ 後重新整理)"


def _list_fonts():
    """List .ttf / .otf / .ttc files in plugin's font/ subdir for the dropdown."""
    if not os.path.isdir(_FONT_DIR):
        return []
    return sorted(
        f for f in os.listdir(_FONT_DIR)
        if f.lower().endswith((".ttf", ".otf", ".ttc"))
    )


def _read_ttf_family_name(ttf_path):
    """Lazy-import fontTools; extract Family Name from TTF for ASS Fontname.

    Why: ASS subtitles filter resolves font by Family Name (not filename) via
    libass + fontsdir. The TTF filename ≠ internal Family Name in general
    (e.g. NotoSansCJK-Regular.ttf → "Noto Sans CJK TC"). Reading the name table
    gives us the right string without making the user look it up manually.

    Returns family name string, or None if fontTools missing / parse fails —
    caller falls back to the font's filename stem then.
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    try:
        # fontNumber=0 — TTC (font collection) 取第一個 face
        ttf = TTFont(ttf_path, fontNumber=0)
        name_table = ttf["name"]
        # 先試 nameID 16 (Preferred Family，使用者在 OS 字型選單看到的名字)、
        # 再 fallback 到 1 (Family，較舊、會把粗體/斜體拆成不同 family)。
        for name_id in (16, 1):
            for rec in name_table.names:
                if rec.nameID == name_id:
                    try:
                        return rec.toUnicode()
                    except UnicodeDecodeError:
                        continue
    except Exception as e:
        print(f"[Burn Subtitle] 注意：解析 {os.path.basename(ttf_path)} 失敗：{e}")
    return None


class MF_BurnSubtitle:
    @classmethod
    def INPUT_TYPES(s):
        fonts = _list_fonts()
        font_choices = fonts or [_NO_FONTS_PLACEHOLDER]
        # msjh.ttc = Microsoft JhengHei、常用中文字幕字型；有就帶為預設
        default_font = "msjh.ttc" if "msjh.ttc" in fonts else font_choices[0]
        # Encoder catalog 與 SaveVideoFrames/ComposeFinalize 共用，NVENC variants 視 ffmpeg
        # capability 動態加入；prores_ks 雖在 list 內但走 .mp4 容器會 mux fail — caller 自己決定。
        codec_map = get_available_codecs()
        codec_choices = list(codec_map.keys())
        return {
            "required": {
                # === 影片來源（最頂、最先設定）===
                # ComfyUI 渲染順序：required 全部 → optional 全部。video_path 放 required position 1
                # 才會在 node body 最頂端。連 frames pin 時 web/dual_input_lock.js 自動把它 hide。
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                # === 字幕來源 ===
                "srt_path": ("STRING", {"default": "input/sample.srt"}),
                # === 輸出設定 ===
                # filename_prefix 是 ComfyUI SaveImage 慣例：每次跑 workflow 自動接 _00001/_00002...，
                # 不會 silently 覆蓋舊輸出。子目錄 OK，如 "MediaForge/subtitled" → output/MediaForge/subtitled_00001.mp4
                "filename_prefix": ("STRING", {"default": "MediaForge/subtitled"}),
                # === 編碼控制（P1-1）===
                # 跟 SaveVideoFrames / ComposeFinalize 共用 encoder catalog；
                # 預設 libx264 / crf 18 / medium = 視覺無損 (~CRF 18) + 合理速度。
                # NVENC 可用時自動進 dropdown — 對長片可大幅加速。
                "codec": (codec_choices, {"default": pick_default_codec()}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
                "preset": (
                    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"],
                    {"default": "medium"},
                ),

                # === 字型（user-prioritized，移到 styling block 開頭） ===
                "font": (font_choices, {"default": default_font}),
                "font_size": ("INT", {"default": 24, "min": 8, "max": 150}),
                "font_color_hex": ("STRING", {"default": "#FFFFFF"}),
                "bold": ("BOOLEAN", {"default": True}),
                "italic": ("BOOLEAN", {"default": False}),
                "letter_spacing": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 20.0, "step": 0.1}),

                # === 描邊 / 陰影 / 底色 ===
                "outline_color_hex": ("STRING", {"default": "#000000"}),
                "outline_width": ("INT", {"default": 2, "min": 0, "max": 10}),
                "shadow_depth": ("INT", {"default": 1, "min": 0, "max": 10}),
                # ASS BorderStyle 規格：1 = outline+drop shadow，3 = opaque box。
                # 值 2 在 ASS spec 不存在 → step=2 讓 INT widget 只能選 1 或 3，避免
                # 使用者誤填 2 跑出 libass 警告。預設 1 = 透明描邊（最常見字幕樣式）。
                "border_style": ("INT", {"default": 1, "min": 1, "max": 3, "step": 2}),
                "back_color_hex": ("STRING", {"default": "#000000"}),

                # === 字幕位置（寬度 = 播放區寬 − margin_l − margin_r） ===
                "alignment": (list(_ALIGNMENT_MAP.keys()), {"default": "bottom_center (2)"}),
                "margin_v": ("INT", {"default": 20, "min": 0, "max": 500}),
                "margin_l": ("INT", {"default": 50, "min": 0, "max": 1000}),
                "margin_r": ("INT", {"default": 50, "min": 0, "max": 1000}),
            },
            "optional": {
                # Tensor pipeline (in-memory chain)：連線 frames 時 web/dual_input_lock.js 會 hide 上面的 video_path
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),

                # 接 audio pin 時是否保留 source 自帶音軌（path mode + source 有音軌時生效）。
                # True = amix 兩條音軌；False = 外部音訊蓋過 source（舊行為）。
                # frames mode 不適用：encode_tensor_to_tempfile 已把 audio mux 進 temp.mp4、
                # 只有一條音軌。
                "keep_source_audio": ("BOOLEAN", {"default": True}),

                # 進階：輸出畫格率覆寫；0.0 = 沿用 source fps。FLOAT 是為了支援 cinematic
                # fps（23.976 / 29.97 / 59.94）— 廣電 / 手機素材常見,INT 會逼使用者四捨五入。
                "target_fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 240.0, "step": 0.1}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "burn"
    CATEGORY = "MediaForge/Subtitle"

    def burn(self, video_path, srt_path, filename_prefix,
             codec, crf, preset,
             font, font_size,
             font_color_hex, bold, italic, letter_spacing,
             outline_color_hex, outline_width, shadow_depth, border_style, back_color_hex,
             alignment, margin_v, margin_l, margin_r,
             frames=None, tensor_fps=30.0, audio=None,
             keep_source_audio=True,
             target_fps=0.0):
        if not ensure_ffmpeg():
            raise RuntimeError("[Burn Subtitle] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(srt_path):
            raise FileNotFoundError(f"[Burn Subtitle] 找不到字幕：{srt_path}")

        # font dropdown：空目錄 placeholder 命中即 raise 帶友善訊息
        if font == _NO_FONTS_PLACEHOLDER:
            raise FileNotFoundError(
                "[Burn Subtitle] font/ 目錄沒有 .ttf / .otf / .ttc。"
                "請把字型丟到 plugin/font/ 後重新整理瀏覽器讓 dropdown 重新掃描。"
            )
        ttf_path = os.path.join(_FONT_DIR, font)
        if not os.path.exists(ttf_path):
            raise FileNotFoundError(
                f"[Burn Subtitle] font 檔不存在：{ttf_path}"
                "（如剛丟新檔請重新整理瀏覽器讓 dropdown 重新掃描）"
            )

        # 偵測 Family Name；fontTools 沒裝 / parse 失敗就退用檔名 stem
        family_name = _read_ttf_family_name(ttf_path)
        if family_name:
            print(f"[Burn Subtitle] 字型 Family Name (auto): {family_name}")
        else:
            family_name = os.path.splitext(font)[0]
            print(
                f"[Burn Subtitle] 字型 Family Name 退用檔名 stem: {family_name}"
                "（建議 `pip install fontTools` 讓自動偵測 TTF 內部名稱）"
            )

        # 解析輸出路徑 — auto-counter 不會覆蓋舊檔
        output_path = resolve_output_path(filename_prefix, ".mp4")

        # ASS Style 組裝
        primary = hex_to_ass_color(font_color_hex)
        outline = hex_to_ass_color(outline_color_hex)
        # border_style=3 為「box」底色樣式，底色半透明 (alpha=80) 避免完全遮畫面；其他樣式則完全透明
        back = hex_to_ass_color(back_color_hex, alpha="80" if border_style == 3 else "00")
        bold_val = -1 if bold else 0
        italic_val = -1 if italic else 0
        alignment_int = _ALIGNMENT_MAP[alignment]

        style = (
            f"Fontname={family_name},Fontsize={font_size},"
            f"PrimaryColour={primary},OutlineColour={outline},"
            f"BackColour={back},Bold={bold_val},Italic={italic_val},Spacing={letter_spacing},"
            f"BorderStyle={border_style},Outline={outline_width},Shadow={shadow_depth},"
            f"Alignment={alignment_int},MarginV={margin_v},MarginL={margin_l},MarginR={margin_r}"
        )

        cleanup_tmp = None
        cleanup_audio_tmp = None
        try:
            # Dual-input dispatch：frames 接了 → 寫 temp mp4 (encode_tensor_to_tempfile 已
            # 處理 audio mux)；沒接 → 用 video_path，audio dict (若有接) 需在這層額外 mux。
            if frames is not None:
                source_path = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
                cleanup_tmp = source_path
            else:
                if not os.path.exists(video_path):
                    raise FileNotFoundError(
                        f"[Burn Subtitle] 找不到影片：{video_path}"
                        "（連 frames 或在 video_path 填好路徑）"
                    )
                source_path = video_path

            # 組 -vf subtitles=...:fontsdir=...:force_style='...' (FFmpeg 用 : 串選項)
            vf_parts = [
                f"subtitles={escape_filter_path(srt_path)}",
                f"fontsdir={escape_filter_path(_FONT_DIR)}",
                f"force_style='{style}'",
            ]
            vf_arg = ":".join(vf_parts)

            command = ["ffmpeg", "-y", "-i", source_path]

            # Path mode + 使用者明確接了 audio dict → 走 second `-i` mux。為什麼要這個分支：
            # path mode 之前完全沒讀 audio 參數，使用者把 video-only 上游 (e.g. LoopVideo with
            # keep_audio=False) 接 video_path、另接音樂到 audio pin，期待節點 mux — 實際是
            # silently 丟掉 audio。
            #
            # keep_source_audio 決定兩條音軌怎麼處理：
            #   True  + source 有音軌 → amix 兩條（混音）
            #   True  + source 無音軌 → 只用外部音軌（amix 需要兩 input、無 fallback）
            #   False → 外部音軌蓋過 source（舊行為）
            audio_filter_complex = None
            if frames is None and audio is not None:
                audio_wav = write_audio_dict_to_wav(audio)
                cleanup_audio_tmp = audio_wav
                command.extend(["-i", audio_wav])

                # Probe source 自帶音軌 — amix 需要兩條 input 都存在、不然 ffmpeg error
                source_info = probe(source_path)
                source_has_audio = bool(
                    source_info and any(
                        s.get("codec_type") == "audio"
                        for s in source_info.get("streams", [])
                    )
                )

                if keep_source_audio and source_has_audio:
                    # duration=first：輸出長度跟著 input 0 (video 自帶音軌) 走 — 字幕燒錄場景
                    # 影片是 master timeline；外部音訊比影片長就截掉、短就尾端靜音。
                    # dropout_transition=0：關掉 amix 預設 2s 淡入淡出（給「中途某條結束」的
                    # 平滑用，這裡兩條都跑到尾不需要）
                    audio_filter_complex = (
                        "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[aout]"
                    )
                    audio_map = ["-map", "0:v", "-map", "[aout]"]
                else:
                    # replace mode 或 source 沒音軌可混 → 只取 input 1 的音訊
                    audio_map = ["-map", "0:v", "-map", "1:a"]
            else:
                # 沒接 audio dict → 沿用 input 0 自身音軌 (若有)；`?` 讓 source 無音軌不 raise
                audio_map = ["-map", "0:v", "-map", "0:a?"]

            if target_fps > 0:
                command.extend(["-r", str(target_fps)])
            # 顯式 -map 鎖 stream，不依賴 ffmpeg 的 default stream selection — 後者在某些
            # filter graph / 容器組合下會 silently drop 非 selected stream type，使 -c:a aac
            # 變成 no-op、輸出沒有音軌。
            # R10 P2 fix：`-c:a copy` 跨容器 (e.g., MKV/WebM → MP4) mux fail，統一轉 AAC
            # P1-1：codec / crf / preset 共用 encoder builder（含 NVENC 自動切到 -rc vbr -cq）
            codec_map = get_available_codecs()
            codec_id, default_pix_fmt = codec_map.get(codec, codec_map["h264 (libx264)"])
            # -filter_complex 處理 audio amix；同時 -vf 仍跑 video subtitle chain。
            # 兩者共存 OK（label 不衝突）— -vf 是 implicit single-IO video graph，
            # -filter_complex 顯式 graph 處理跨輸入 audio mux。
            if audio_filter_complex:
                command.extend(["-filter_complex", audio_filter_complex])
            command.extend(audio_map)
            command.extend(["-vf", vf_arg])
            command.extend(build_encoder_args(codec_id, crf=crf, preset=preset))
            command.extend([
                "-pix_fmt", default_pix_fmt,
                "-c:a", "aac", "-b:a", "192k",
                output_path,
            ])

            if not run_ffmpeg(command, tag="Burn Subtitle"):
                raise RuntimeError("[Burn Subtitle] FFmpeg 燒字幕失敗，請查看上方 stderr 輸出。")
        finally:
            for tmp in (cleanup_tmp, cleanup_audio_tmp):
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

        print(f"[Burn Subtitle] 輸出成功: {output_path}")
        # UI emit：讓 ComfyUI /history/<prompt_id> 把成品檔案路徑暴露給 API 客戶端，
        # `/view?filename=X&subfolder=Y&type=output` 可直接下載。result 保留 tuple、下游 wire 不變。
        return {"ui": {"images": [output_path_to_ui_entry(output_path)]}, "result": (output_path,)}


NODE_CLASS_MAPPINGS = {"MF_BurnSubtitle": MF_BurnSubtitle}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_BurnSubtitle": "🔥 Burn Subtitle"}
