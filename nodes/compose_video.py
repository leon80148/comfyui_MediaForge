"""MF_ComposeVideo — Compose pipeline 單一終點節點。

取代舊的 MF_ComposeStart + MF_ComposeFinalize 兩段式設計:這個節點同時負責
1. Source 解析(dual-input: video_path / frames tensor)
2. 0-sentinel target dimension probe (target_*=0 → 沿用 source)
3. Compose IR 建構 + apply overlays (MF_COMPOSE_OPS chain) + apply audio ops (MF_COMPOSE_AUDIO_OPS chain)
4. compile_ir + compile_audio_chain → 單條 -filter_complex
5. ffmpeg encode + UI metadata emit + cleanup

優勢:
- 最少 1 個節點(只 video_path、沒 overlay)就能跑、純 transcode 也 OK
- 跟其他 file-producing node (BurnSubtitle / LoopVideo / SaveVideoFrames) 同
  return shape:`{"ui": {"images": [...]}, "result": (path, script)}`
- 字幕 + 浮水印 + overlay + audio mix 一次 encode 完成、不再要二次 re-encode
"""
import os

from ..utils.cache_key import path_fingerprint
from ..utils.compose_ir import ComposeIR, compile_audio_chain, compile_ir, write_filter_script_if_long
from ..utils.compose_ops import apply_audio_ops_to_ir, apply_overlays_to_ir
from ..utils.encoder import build_encoder_args, get_available_codecs, pick_default_codec
from ..utils.ffmpeg import (
    ensure_ffmpeg,
    get_video_display_dims,
    probe,
    run_ffmpeg,
)
from ..utils.output_path import output_path_to_ui_entry, resolve_output_path
from ..utils.video_io import encode_tensor_to_tempfile, mux_path_with_audio_dict


class MF_ComposeVideo:
    @classmethod
    def INPUT_TYPES(s):
        codec_ids = [codec_id for codec_id, _pix_fmt in get_available_codecs().values()]
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                "filename_prefix": ("STRING", {"default": "MediaForge/composed"}),
                # 0 sentinel = 沿用 source 對應數值 (跟 LoadVideoFrames / BurnSubtitle 慣例對齊)
                # 大多時候使用者不需要動這三個欄位、留 0 即可
                "target_fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 240.0, "step": 0.1}),
                "target_width": ("INT", {"default": 0, "min": 0, "max": 7680, "step": 2}),
                "target_height": ("INT", {"default": 0, "min": 0, "max": 4320, "step": 2}),
                # Encoder 控制 — codec smart default (有 NVENC 用 GPU、沒有退 libx264)
                "codec": (codec_ids, {"default": pick_default_codec(by_id=True)}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
                "preset": (
                    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"],
                    {"default": "medium"},
                ),
                # 是否保留 main video 自帶 audio 軌 (沒接 audio_ops 時的預設行為)
                "keep_audio": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                # In-memory chain:接 frames 走 tensor → temp mp4 → Compose 流程
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),
                # MF_COMPOSE_OPS chain:從 OverlayText / OverlayImage / Watermark / BurnSubtitle 串進來
                "overlays": ("MF_COMPOSE_OPS",),
                # MF_COMPOSE_AUDIO_OPS chain:從 Volume / AudioMix / AudioFade / Normalize 串進來
                "audio_ops": ("MF_COMPOSE_AUDIO_OPS",),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("final_video_path", "filter_complex_script")
    FUNCTION = "compose"
    CATEGORY = "MediaForge/Compose"

    def compose(self, video_path, filename_prefix,
                target_fps, target_width, target_height,
                codec, crf, preset, keep_audio,
                frames=None, tensor_fps=30.0, audio=None,
                overlays=None, audio_ops=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Compose Video] FFmpeg / FFprobe 未在 PATH 中,請先安裝。")

        # Source 解析 — dual-input pattern
        cleanup_source_tmp = None
        cleanup_paths = []  # drawtext textfile / 超長 filter script / IR tensor temp（W1-3：finally 統一清）
        ir = None  # R5-1：宣告在 try 外，讓 finally 在 apply_overlays_to_ir / apply_audio_ops_to_ir /
        # compile_ir 任一階段 raise 時，都能透過 ir.tmp_paths_to_cleanup 兜底清 tensor temp / audio WAV
        if frames is not None:
            # tensor → temp mp4 (encode_tensor_to_tempfile 已處理 audio mux)
            source_path = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
            cleanup_source_tmp = source_path
        else:
            if not os.path.exists(video_path):
                raise FileNotFoundError(f"[Compose Video] 找不到影片:{video_path}")
            if audio is not None:
                # path mode + audio dict 同時接:pre-mux,避免 audio 在 chain 下游 silent drop。
                # keep_audio=True → 把原音 + 外接 audio amix(讓 keep_audio widget 行為符合命名);
                # keep_audio=False → replace(只留外接 audio,使用者明確想蓋掉原音)。
                source_path = mux_path_with_audio_dict(
                    video_path, audio, keep_original=bool(keep_audio),
                )
                cleanup_source_tmp = source_path
            else:
                source_path = video_path

        try:
            # Probe source 拿尺寸 / fps / has_audio (給 0-sentinel resolve 用 + 音訊 chain decision)
            info = probe(source_path)
            if not info:
                raise RuntimeError(
                    f"[Compose Video] ffprobe 無法解析:{source_path}。"
                    "請先用 MF_ProbeMedia 驗證檔案可解析、或檢查是否為損毀影片"
                )
            v_stream = next(
                (s for s in info.get("streams", []) if s.get("codec_type") == "video"),
                None,
            )
            if not v_stream:
                raise RuntimeError(
                    f"[Compose Video] source 沒有 video stream:{source_path}。"
                    "請改傳影片檔（不能用純音訊檔當 ComposeVideo 的 source）"
                )
            # W2-3：直接用手上已經 probe 過的 info 判斷，不再多打一次
            # probe_has_audio_stream(source_path)（該函式內部也是呼叫 probe()，
            # 對同一個 source_path 重複 probe 純屬多餘的 ffprobe spawn）。
            has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))

            # 0-sentinel resolve target dimensions
            if target_width == 0 or target_height == 0:
                src_w, src_h = get_video_display_dims(v_stream)
                if target_width == 0:
                    target_width = src_w
                if target_height == 0:
                    target_height = src_h
            if target_fps == 0.0:
                rfr = v_stream.get("r_frame_rate") or ""
                try:
                    num, den = rfr.split("/")
                    d = float(den)
                    target_fps = float(num) / d if d != 0 else 30.0
                except (ValueError, AttributeError):
                    target_fps = 30.0  # 極罕見 fallback

            # 建 IR
            ir = ComposeIR(
                target_fps=target_fps,
                target_width=int(target_width),
                target_height=int(target_height),
            )
            ir.add_video_input(source_path, is_main=True, has_audio=has_audio)
            if cleanup_source_tmp:
                ir.tmp_paths_to_cleanup.append(cleanup_source_tmp)
                # cleanup_source_tmp ownership 轉給 IR、避免 try/finally 雙刪
                cleanup_source_tmp = None

            # Apply overlays chain (drawtext / overlay / watermark / subtitle)
            if overlays:
                apply_overlays_to_ir(ir, overlays)

            # Apply audio ops chain (volume / amix / afade / loudnorm)
            if audio_ops:
                apply_audio_ops_to_ir(ir, audio_ops)

            # Compile IR → video filter chain + audio filter chain
            video_script, final_v_label, cleanup_paths = compile_ir(ir)
            audio_script, final_a_label = compile_audio_chain(ir)

            # 合併兩條 filter chain(若有 audio chain)
            if audio_script:
                full_script = video_script + ";" + audio_script
            else:
                full_script = video_script

            # 超長 filter graph 自動切 -filter_complex_script tempfile
            cli_arg, tmp_script_path = write_filter_script_if_long(full_script)
            if tmp_script_path:
                cleanup_paths.append(tmp_script_path)

            # 解析輸出路徑(prores_ks 走 .mov、其他 .mp4)
            ext = ".mov" if codec == "prores_ks" else ".mp4"
            output_path = resolve_output_path(filename_prefix, ext)

            # 建 ffmpeg command
            cmd = ["ffmpeg", "-y"]
            for inp in ir.inputs.values():
                # image input 加 -loop 1 讓單張 PNG 變 looping video stream、否則 overlay 只出現 1 frame
                if inp.source_type == "image_path":
                    cmd.extend(["-loop", "1", "-framerate", str(target_fps), "-i", inp.path])
                else:
                    cmd.extend(["-i", inp.path])

            # filter_complex 走 inline string 或 script file (依長度自動切)
            if tmp_script_path:
                cmd.extend(["-filter_complex_script", cli_arg])
            else:
                cmd.extend(["-filter_complex", cli_arg])

            # Map video output
            cmd.extend(["-map", f"[{final_v_label}]"])

            # Map audio output 邏輯:
            #   1. 有 audio_ops chain → 用 chain output [aN]
            #   2. 沒 chain 但有 audio 可輸出 → 走 source audio (`0:a?`)。
            #      "有 audio 可輸出" 的條件:
            #        - keep_audio=True 且 source 有 audio (原音保留)
            #        - 使用者外接了 audio dict (此時 source_path 已被 pre-mux 含外接 audio,
            #          不管 keep_audio 是 True/False 都該輸出 — 否則使用者接 audio 卻聽不到)
            #   3. 都不是 → -an (silent output)
            external_audio_provided = audio is not None
            if final_a_label and audio_script:
                cmd.extend(["-map", f"[{final_a_label}]"])
                cmd.extend(["-c:a", "aac", "-b:a", "192k"])
            elif has_audio and (keep_audio or external_audio_provided):
                cmd.extend(["-map", "0:a?"])
                cmd.extend(["-c:a", "aac", "-b:a", "192k"])
            else:
                cmd.append("-an")

            # Video encoder
            codec_map = get_available_codecs()
            # codec_ids → 找回 (codec_id, pix_fmt);用 codec str 反查
            codec_pix_fmt = None
            for _name, (cid, pfmt) in codec_map.items():
                if cid == codec:
                    codec_pix_fmt = pfmt
                    break
            if codec_pix_fmt is None:
                # fallback safe value
                codec_pix_fmt = "yuv420p" if codec != "prores_ks" else "yuv422p10le"

            cmd.extend(build_encoder_args(codec, crf=crf, preset=preset))
            cmd.extend(["-pix_fmt", codec_pix_fmt])

            # ProRes 需要 -profile:v 3 (HQ) 才能用、否則 prores_ks 用預設 profile=2 (standard)
            # 不強制、讓 caller 在未來如要 profile 控制再加 UI

            cmd.append(output_path)

            ok = run_ffmpeg(cmd, tag="Compose Video")
            if not ok:
                raise RuntimeError(
                    "[Compose Video] FFmpeg encode 失敗,請查看上方 stderr 輸出。"
                    f"(filter_complex 長度 {len(full_script)} 字元、"
                    f"{'已 dump 到 tempfile' if tmp_script_path else '直接走 CLI'})"
                )

            print(f"[Compose Video] 輸出成功({len(ir.ops)} video ops + {len(ir.audio_ops)} audio ops): {output_path}")

            # UI metadata emit:讓 /history/<prompt_id> 看到、API 端 /view 可下載
            # R9-1：filename_prefix 逃出 output_dir 時 entry 為 None，ui.images 留空陣列。
            entry = output_path_to_ui_entry(output_path)
            return {
                "ui": {"images": [entry] if entry else []},
                "result": (output_path, full_script),
            }
        finally:
            # W1-3：compile-time tmp files (drawtext textfile / 超長 filter script /
            # IR tensor temp) 原本只在成功路徑 unlink;encode 失敗 raise 後就永久留在
            # 磁碟(tensor temp mp4 可達數百 MB)。移進 finally 讓兩種 path 都清得到。
            #
            # R5-1：cleanup_paths 要等 compile_ir 成功回傳才會被填,但
            # apply_overlays_to_ir / apply_audio_ops_to_ir / compile_ir 任一階段中途
            # raise 時,tensor temp（ir.add_video_input 後）與 _is_temp audio WAV
            # （apply_audio_ops_to_ir 的 amix 分支）都已經寫進 ir.tmp_paths_to_cleanup、
            # 只是還沒被 compile_ir 併入 cleanup_paths。改成 set 聯集三個來源
            # （cleanup_paths ∪ ir.tmp_paths_to_cleanup ∪ cleanup_source_tmp）再逐一
            # best-effort unlink,任一階段 raise 都不 leak；重複路徑 unlink 兩次無害
            # （第二次 OSError → pass）。
            all_cleanup = set(cleanup_paths)
            if ir is not None:
                all_cleanup.update(ir.tmp_paths_to_cleanup)
            if cleanup_source_tmp:
                all_cleanup.add(cleanup_source_tmp)
            for p in all_cleanup:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # C1 fix：ComfyUI IsChangedCache.get() 呼叫
        # get_input_data(node["inputs"], class_def, node_id, None) —— 第 4 參數
        # execution_list 固定傳 None，官方註解寫明 "We only want constants in
        # IS_CHANGED"。後果：overlays / audio_ops（跟 frames / audio 一樣）是
        # linked 輸入，IS_CHANGED 執行時收到的值永遠是 None。舊版在這裡遍歷
        # kwargs.get("overlays") / kwargs.get("audio_ops") 找 image_path /
        # srt_path / audio_path 完全是 dead code —— 迴圈永遠跑 0 次，換掉
        # watermark PNG 這類上游檔案不會 invalidate cache。
        #
        # 真正的檔案變更偵測責任在「持有該 path widget」的節點自己：
        # MF_ComposeWatermark / MF_ComposeOverlayImage / MF_ComposeBurnSubtitle /
        # MF_ComposeAudioMix 各自的 IS_CHANGED 已補上對應 fingerprint（它們的
        # image_path / srt_path / audio_path 是自己的 widget，IS_CHANGED 看得到）。
        # 這裡只留 video_path —— 它是 ComposeVideo 自己的 widget。tensor 模式下
        # video_path 沒被讀，fingerprint 它只是無害的多餘敏感度。
        return path_fingerprint(kwargs.get("video_path", ""))


NODE_CLASS_MAPPINGS = {"MF_ComposeVideo": MF_ComposeVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeVideo": "🎬 Compose Video"}
