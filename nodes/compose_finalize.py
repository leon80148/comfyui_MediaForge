"""MF_ComposeFinalize — compile Compose IR → 單次 FFmpeg encode → 輸出影片。"""
import os

from ..utils.compose_ir import ComposeIR, compile_ir, write_filter_script_if_long
from ..utils.ffmpeg import ensure_ffmpeg, probe_video_duration, run_ffmpeg
from ..utils.output_path import resolve_output_path
from ..utils.video_io import svtav1_preset_from_name


class MF_ComposeFinalize:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "compose": ("MF_COMPOSE",),
                "filename_prefix": ("STRING", {"default": "MediaForge/composed"}),
                "codec": (["libx264", "libx265", "libsvtav1", "prores_ks"], {"default": "libx264"}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
                "preset": (
                    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"],
                    {"default": "medium"},
                ),
                # 是否保留 main video 的原 audio 軌
                "keep_audio": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("final_video_path", "filter_complex_script")
    FUNCTION = "finalize"
    CATEGORY = "MediaForge/Compose"

    def finalize(self, compose, filename_prefix, codec, crf, preset, keep_audio):
        if not ensure_ffmpeg():
            raise RuntimeError("[Compose Finalize] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not isinstance(compose, ComposeIR):
            raise ValueError(
                f"[Compose Finalize] 輸入不是 MF_COMPOSE IR，拿到 {type(compose).__name__}"
            )
        if not compose.inputs:
            raise RuntimeError("[Compose Finalize] IR 沒有任何 input — 是否漏接 MF_ComposeStart？")

        # Codec-aware container：prores_ks 必須走 .mov；其餘走 .mp4
        # (取代舊版的 _ensure_compatible_container post-hoc 修正；現在 ext 從 prefix resolve
        # 時就決定，counter 也按該 ext 算 max digit、不會跨 container 撞數)
        ext = ".mov" if codec == "prores_ks" else ".mp4"
        output_path = resolve_output_path(filename_prefix, ext)

        # IR 經 compile 會 mutate 內部 ops 的 depends_on（rewrite main_label → norm_label），
        # 為避免破壞下游可能 cache 住的 IR，先 clone。
        ir = compose.clone()
        script, final_label, drawtext_cleanup = compile_ir(ir)
        filter_arg_value, tmp_path = write_filter_script_if_long(script)

        cmd = ["ffmpeg", "-y"]
        # 按 ir.inputs.index 排序 -i
        sorted_inputs = sorted(ir.inputs.values(), key=lambda x: x.index)
        for ent in sorted_inputs:
            if ent.source_type == "image_path":
                # static image 用 -loop 1 才能與 video 同長度
                cmd.extend(["-loop", "1", "-i", ent.path])
            else:
                cmd.extend(["-i", ent.path])

        if tmp_path is None:
            cmd.extend(["-filter_complex", filter_arg_value])
        else:
            cmd.extend(["-filter_complex_script", filter_arg_value])

        cmd.extend(["-map", f"[{final_label}]"])

        if keep_audio and ir.main_audio_label:
            cmd.extend(["-map", ir.main_audio_label, "-c:a", "aac", "-b:a", "192k"])
        else:
            cmd.append("-an")

        cmd.extend(["-c:v", codec])
        if codec in ("libx264", "libx265"):
            cmd.extend(["-crf", str(crf), "-preset", preset])
        elif codec == "libsvtav1":
            # SVT-AV1 用 numeric preset 0–13；x264-style 字串會被 ffmpeg 拒掉。
            # 把 UI 上 ultrafast..veryslow 映射到 13..0 的常用區間。
            cmd.extend(["-crf", str(crf), "-preset", svtav1_preset_from_name(preset)])
        elif codec == "prores_ks":
            cmd.extend(["-profile:v", "3"])  # 422 HQ

        cmd.extend(["-pix_fmt", "yuv420p" if codec != "prores_ks" else "yuv422p10le"])

        # 為什麼不用 -shortest：當 main video 的 audio stream 比 video 短 (常見於 capture
        # 或編輯軟體輸出)，-shortest 會把整段 output 截到 audio 結尾、silently 丟掉影片尾。
        # 改用 explicit `-t {main_video_duration}` 鎖長度。Image inputs (-loop 1) 本來
        # 就靠 overlay 的 eof_action=pass 跟著 main 結束，所以不需要 -shortest 處理 image。
        main_video_path = sorted_inputs[0].path  # is_main 強制 index=0
        main_dur = probe_video_duration(main_video_path)
        if main_dur and main_dur > 0:
            cmd.extend(["-t", f"{main_dur:.6f}"])
        else:
            # 退階：probe 失敗就保留 -shortest 行為（image -loop 1 還是要有東西結束）
            print(f"[Compose Finalize] 注意：無法取得 {main_video_path} 時長，退用 -shortest")
            cmd.append("-shortest")
        cmd.append(output_path)

        try:
            ok = run_ffmpeg(cmd, tag="Compose Finalize")
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            for p in drawtext_cleanup:
                try:
                    os.unlink(p)
                except OSError:
                    pass

        if not ok:
            raise RuntimeError(
                "[Compose Finalize] FFmpeg encode 失敗，請查看上方 stderr 輸出。"
                f"\n（filter_complex 長度 {len(script)} 字元，"
                f"{'已 dump 到 tempfile' if tmp_path else '直接走 CLI'}）"
            )
        print(f"[Compose Finalize] 輸出成功（{len(ir.ops)} ops）: {output_path}")
        return (output_path, script)


NODE_CLASS_MAPPINGS = {"MF_ComposeFinalize": MF_ComposeFinalize}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ComposeFinalize": "✅ Compose Finalize"}
