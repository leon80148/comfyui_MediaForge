import os

from ..utils.ass_style import (
    ALIGNMENT_MAP,
    NO_FONTS_PLACEHOLDER,
    build_ass_style_string,
    build_subtitles_filter,
    list_fonts,
    resolve_font,
)
from ..utils.audio_mix import build_amix_filter
from ..utils.cache_key import path_fingerprint
from ..utils.encoder import build_encoder_args, get_available_codecs, pick_default_codec
from ..utils.ffmpeg import ensure_ffmpeg, probe_has_audio_stream, run_ffmpeg
from ..utils.output_path import output_path_to_ui_entry, resolve_output_path
from ..utils.video_io import encode_tensor_to_tempfile, write_audio_dict_to_wav


class MF_BurnSubtitle:
    @classmethod
    def INPUT_TYPES(s):
        fonts = list_fonts()
        font_choices = fonts or [NO_FONTS_PLACEHOLDER]
        # msjh.ttc = Microsoft JhengHei、常用中文字幕字型;有就帶為預設
        default_font = "msjh.ttc" if "msjh.ttc" in fonts else font_choices[0]
        # Encoder catalog 與 SaveVideoFrames/ComposeVideo 共用,NVENC variants 視 ffmpeg
        # capability 動態加入;prores_ks 雖在 list 內但走 .mp4 容器會 mux fail — caller 自己決定。
        codec_map = get_available_codecs()
        codec_choices = list(codec_map.keys())
        return {
            "required": {
                # === 影片來源(最頂、最先設定)===
                # ComfyUI 渲染順序:required 全部 → optional 全部。video_path 放 required position 1
                # 才會在 node body 最頂端。連 frames pin 時 web/dual_input_lock.js 自動把它 hide。
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                # === 字幕來源 ===
                "srt_path": ("STRING", {"default": "input/sample.srt"}),
                # === 輸出設定 ===
                # filename_prefix 是 ComfyUI SaveImage 慣例:每次跑 workflow 自動接 _00001/_00002...,
                # 不會 silently 覆蓋舊輸出。子目錄 OK,如 "MediaForge/subtitled" → output/MediaForge/subtitled_00001.mp4
                "filename_prefix": ("STRING", {"default": "MediaForge/subtitled"}),
                # === 編碼控制 ===
                # 跟 SaveVideoFrames / ComposeVideo 共用 encoder catalog;
                # 預設 codec smart pick(GPU NVENC > libx264) / crf 18 / medium = 視覺無損 + 合理速度。
                "codec": (codec_choices, {"default": pick_default_codec()}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
                "preset": (
                    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"],
                    {"default": "medium"},
                ),

                # === 字型(user-prioritized,移到 styling block 開頭) ===
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
                # ASS BorderStyle 規格:1 = outline+drop shadow,3 = opaque box。
                # 值 2 在 ASS spec 不存在 → step=2 讓 INT widget 只能選 1 或 3,避免
                # 使用者誤填 2 跑出 libass 警告。預設 1 = 透明描邊(最常見字幕樣式)。
                "border_style": ("INT", {"default": 1, "min": 1, "max": 3, "step": 2}),
                "back_color_hex": ("STRING", {"default": "#000000"}),

                # === 字幕位置(寬度 = 播放區寬 − margin_l − margin_r) ===
                "alignment": (list(ALIGNMENT_MAP.keys()), {"default": "bottom_center (2)"}),
                "margin_v": ("INT", {"default": 20, "min": 0, "max": 500}),
                "margin_l": ("INT", {"default": 50, "min": 0, "max": 1000}),
                "margin_r": ("INT", {"default": 50, "min": 0, "max": 1000}),
            },
            "optional": {
                # Tensor pipeline (in-memory chain):連線 frames 時 web/dual_input_lock.js 會 hide 上面的 video_path
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),

                # 接 audio pin 時是否保留 source 自帶音軌(path mode + source 有音軌時生效)。
                # True = amix 兩條音軌;False = 外部音訊蓋過 source(舊行為)。
                # frames mode 不適用:encode_tensor_to_tempfile 已把 audio mux 進 temp.mp4、
                # 只有一條音軌。
                "keep_source_audio": ("BOOLEAN", {"default": True}),

                # 進階:輸出畫格率覆寫;0.0 = 沿用 source fps。FLOAT 是為了支援 cinematic
                # fps(23.976 / 29.97 / 59.94)— 廣電 / 手機素材常見,INT 會逼使用者四捨五入。
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
            raise RuntimeError("[Burn Subtitle] FFmpeg / FFprobe 未在 PATH 中,請先安裝。")
        if not os.path.exists(srt_path):
            raise FileNotFoundError(f"[Burn Subtitle] 找不到字幕:{srt_path}")

        # 字型解析 — placeholder / 不存在 / family name 偵測都走 helper、共用 ComposeBurnSubtitle
        _ttf_path, family_name = resolve_font(font, tag="Burn Subtitle")

        # 解析輸出路徑 — auto-counter 不會覆蓋舊檔；ProRes 需要 .mov 容器才能 mux 成功 (W1-6)
        codec_map = get_available_codecs()
        codec_id, default_pix_fmt = codec_map.get(codec, codec_map["h264 (libx264)"])
        ext = ".mov" if codec_id == "prores_ks" else ".mp4"
        output_path = resolve_output_path(filename_prefix, ext)

        # ASS style string 組裝 — 同 helper、跟 ComposeBurnSubtitle 結果完全一致
        style = build_ass_style_string(
            family_name, font_size,
            font_color_hex, outline_color_hex, back_color_hex,
            bold, italic, letter_spacing,
            outline_width, shadow_depth, border_style,
            alignment, margin_v, margin_l, margin_r,
        )

        cleanup_tmp = None
        cleanup_audio_tmp = None
        try:
            # Dual-input dispatch:frames 接了 → 寫 temp mp4 (encode_tensor_to_tempfile 已
            # 處理 audio mux);沒接 → 用 video_path,audio dict (若有接) 需在這層額外 mux。
            if frames is not None:
                source_path = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
                cleanup_tmp = source_path
            else:
                if not os.path.exists(video_path):
                    raise FileNotFoundError(
                        f"[Burn Subtitle] 找不到影片:{video_path}"
                        "(連 frames 或在 video_path 填好路徑)"
                    )
                source_path = video_path

            # 組 -vf subtitles=...:fontsdir=...:force_style='...' (FFmpeg 用 : 串選項)
            vf_arg = build_subtitles_filter(srt_path, style)

            command = ["ffmpeg", "-y", "-i", source_path]

            # Path mode + 使用者明確接了 audio dict → 走 second `-i` mux。為什麼要這個分支:
            # path mode 之前完全沒讀 audio 參數,使用者把 video-only 上游 (e.g. LoopVideo with
            # keep_audio=False) 接 video_path、另接音樂到 audio pin,期待節點 mux — 實際是
            # silently 丟掉 audio。
            #
            # keep_source_audio 決定兩條音軌怎麼處理:
            #   True  + source 有音軌 → amix 兩條(混音)
            #   True  + source 無音軌 → 只用外部音軌(amix 需要兩 input、無 fallback)
            #   False → 外部音軌蓋過 source(舊行為)
            audio_filter_complex = None
            if frames is None and audio is not None:
                audio_wav = write_audio_dict_to_wav(audio)
                cleanup_audio_tmp = audio_wav
                command.extend(["-i", audio_wav])

                # Probe source 自帶音軌 — amix 需要兩條 input 都存在、不然 ffmpeg error
                if keep_source_audio and probe_has_audio_stream(source_path):
                    # duration=first:輸出長度跟著 input 0 (video 自帶音軌) 走 — 字幕燒錄場景
                    # 影片是 master timeline;外部音訊比影片長就截掉、短就尾端靜音。
                    # dropout_transition=0:關掉 amix 預設 2s 淡入淡出(給「中途某條結束」的
                    # 平滑用,這裡兩條都跑到尾不需要)
                    audio_filter_complex = build_amix_filter(
                        ["0:a", "1:a"], duration="first", dropout_transition=0, out_label="aout"
                    )
                    audio_map = ["-map", "0:v", "-map", "[aout]"]
                else:
                    # replace mode 或 source 沒音軌可混 → 只取 input 1 的音訊
                    audio_map = ["-map", "0:v", "-map", "1:a"]
            else:
                # 沒接 audio dict → 沿用 input 0 自身音軌 (若有);`?` 讓 source 無音軌不 raise
                audio_map = ["-map", "0:v", "-map", "0:a?"]

            if target_fps > 0:
                command.extend(["-r", str(target_fps)])
            # 顯式 -map 鎖 stream,不依賴 ffmpeg 的 default stream selection — 後者在某些
            # filter graph / 容器組合下會 silently drop 非 selected stream type,使 -c:a aac
            # 變成 no-op、輸出沒有音軌。
            # R10 P2 fix:`-c:a copy` 跨容器 (e.g., MKV/WebM → MP4) mux fail,統一轉 AAC
            # P1-1:codec / crf / preset 共用 encoder builder(含 NVENC 自動切到 -rc vbr -cq)
            # codec_id / default_pix_fmt 已在輸出路徑解析時算過 (W1-6)，這裡直接沿用
            # -filter_complex 處理 audio amix;同時 -vf 仍跑 video subtitle chain。
            # 兩者共存 OK(label 不衝突)— -vf 是 implicit single-IO video graph,
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
                raise RuntimeError("[Burn Subtitle] FFmpeg 燒字幕失敗,請查看上方 stderr 輸出。")
        finally:
            for tmp in (cleanup_tmp, cleanup_audio_tmp):
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

        print(f"[Burn Subtitle] 輸出成功: {output_path}")
        # UI emit:讓 ComfyUI /history/<prompt_id> 把成品檔案路徑暴露給 API 客戶端,
        # `/view?filename=X&subfolder=Y&type=output` 可直接下載。result 保留 tuple、下游 wire 不變。
        # R9-1：filename_prefix 逃出 output_dir 時 entry 為 None，ui.images 留空陣列。
        entry = output_path_to_ui_entry(output_path)
        return {"ui": {"images": [entry] if entry else []}, "result": (output_path,)}

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # C1 fix：ComfyUI IS_CHANGED 收到的 linked 輸入（frames/audio）永遠是
        # None，用 `kwargs.get("frames") is not None` 判斷現在是不是 tensor 模式
        # 是 dead code（那個條件恆假）。無條件 fingerprint video_path + srt_path：
        # tensor 模式下 video_path 事實上沒被讀，fingerprint 它只是無害的多餘敏感
        # 度；srt_path 兩種 mode 都會讀，本來就該無條件納入。
        return path_fingerprint(kwargs.get("video_path", ""), kwargs.get("srt_path", ""))


NODE_CLASS_MAPPINGS = {"MF_BurnSubtitle": MF_BurnSubtitle}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_BurnSubtitle": "🔥 Burn Subtitle"}
