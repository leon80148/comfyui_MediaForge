"""MF_ConcatVideos — 串接多個影片檔案，含 audio 軌、跨容器 / 跨 codec。

v2.1 ROADMAP Phase 3 — VHS Video Combine 不做 path-level concat 的缺口。
mode='copy' 走 concat demuxer 快路徑（同 codec / 同參數）；
mode='transcode' 走 filter concat 安全路徑（可跨 codec），可加 xfade transition。
"""
import os
import tempfile

from ..utils.ffmpeg import ensure_ffmpeg, run_ffmpeg
from ..utils.output_path import resolve_output_path
from ..utils.video_io import encode_tensor_to_tempfile

# 註：本 module 不 import `escape_filter_path` — concat demuxer 走 list 檔走 abspath、
# transcode 走 -i 路徑（filter graph 不直接寫 path），所以 escape 不必要。
# 之前留 import + `_ = escape_filter_path` hack 已移除。


class MF_ConcatVideos:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                # multiline STRING：一行一個檔案路徑
                "video_paths": (
                    "STRING",
                    {
                        "default": "input/clip1.mp4\ninput/clip2.mp4",
                        "multiline": True,
                    },
                ),
                "filename_prefix": ("STRING", {"default": "MediaForge/concat"}),
                # copy = 同 codec/同參數，秒接；transcode = 跨 codec / 加 transition
                "mode": (["copy", "transcode"], {"default": "transcode"}),
                # xfade duration (sec)；只在 mode=transcode 生效；0 = 純 cut
                "transition_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 5.0, "step": 0.05}),
                "transition_type": (
                    ["fade", "wipeleft", "wiperight", "slideleft", "slideright",
                     "circleopen", "circleclose", "dissolve"],
                    {"default": "fade"},
                ),
                # transcode 模式輸出參數
                "fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "width": ("INT", {"default": 1920, "min": 16, "max": 7680, "step": 2}),
                "height": ("INT", {"default": 1080, "min": 16, "max": 4320, "step": 2}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
            },
            "optional": {
                # In-memory chain: 連 frames 時把它寫成 temp mp4 作為「第一段」（path[0]），
                # video_paths 內列的檔案 shift 到 path[1..N]。需要 ≥1 個 video_paths 條目組成 ≥2 段。
                # tensor_fps 跟上面 required.fps 是不同概念：required.fps 是 transcode mode
                # 輸出目標 fps；tensor_fps 是 tensor → temp mp4 寫入時的 fps。
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "concat"
    CATEGORY = "MediaForge/Video"

    def concat(self, video_paths, filename_prefix, mode, transition_sec, transition_type,
               fps, width, height, crf,
               frames=None, tensor_fps=30.0, audio=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Concat Videos] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        output_path = resolve_output_path(filename_prefix, ".mp4")

        cleanup_tmp = None
        try:
            paths = [p.strip() for p in video_paths.splitlines() if p.strip()]
            if frames is not None:
                # tensor 進來 → 寫 temp mp4 prepend 成 path[0]
                cleanup_tmp = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
                paths = [cleanup_tmp] + paths
            elif audio is not None:
                # path mode + audio dict 的語意在 multi-path concat 下是 ambiguous：
                # 該 audio 要接哪一段 clip？所有 clip？單獨 prepend 成新 audio clip？
                # 沒有明顯正確答案 — 印 warn 提示使用者要嘛走 frames pipeline、要嘛
                # 上游自己先把 audio mux 進對應的 video 檔再接給 ConcatVideos
                print(
                    "[Concat Videos] 注意：path mode 下 audio input 語意不明（接到哪段？），"
                    "已忽略。若要 mux 獨立 audio，請走 frames+audio tensor pipeline、"
                    "或上游先 mux 好 audio 再傳檔案路徑進來。"
                )
            if len(paths) < 2:
                raise ValueError(
                    f"[Concat Videos] 至少需要 2 個輸入，但只有 {len(paths)} 個"
                    "（提示：連 frames 時 video_paths 至少還要 1 條路徑）"
                )
            for p in paths:
                if not os.path.exists(p):
                    raise FileNotFoundError(f"[Concat Videos] 找不到檔案：{p}")

            out_dir = os.path.dirname(output_path)
            if out_dir and not os.path.exists(out_dir):
                os.makedirs(out_dir, exist_ok=True)

            if mode == "copy":
                if transition_sec > 0:
                    print("[Concat Videos] 注意：mode=copy 無法加 transition，已忽略 transition_sec")
                self._concat_demuxer(paths, output_path)
            else:
                self._concat_transcode(
                    paths, output_path,
                    transition_sec=transition_sec,
                    transition_type=transition_type,
                    fps=fps, width=width, height=height, crf=crf,
                )

            print(f"[Concat Videos] 輸出成功（{len(paths)} 段）: {output_path}")
            return (output_path,)
        finally:
            if cleanup_tmp:
                try:
                    os.unlink(cleanup_tmp)
                except OSError:
                    pass

    @staticmethod
    def _concat_demuxer(paths, output_path):
        # concat demuxer 要一個 list 檔；filename 要用 single-quote escape 防特殊字元。
        # R7 P2 fix：concat demuxer 把 list 內路徑當「相對於 list 檔所在目錄」解析，
        # 但 list 檔在 /tmp 而使用者的 cwd 可能不在 /tmp → 相對路徑會解析錯。
        # 一律 abspath 再寫進 list 檔。
        fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="mf_concat_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for p in paths:
                    abs_p = os.path.abspath(p)
                    safe = abs_p.replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c", "copy",
                output_path,
            ]
            if not run_ffmpeg(cmd, tag="Concat Videos"):
                raise RuntimeError(
                    "[Concat Videos] copy 模式失敗。可能原因：來源檔 codec/解析度/timebase 不一致；"
                    "改用 mode=transcode 重試。"
                )
        finally:
            try:
                os.unlink(list_path)
            except OSError:
                pass

    @staticmethod
    def _concat_transcode(paths, output_path, *, transition_sec, transition_type,
                          fps, width, height, crf):
        from ..utils.ffmpeg import probe as _probe
        from ..utils.ffmpeg import probe_video_duration as _pd

        n = len(paths)
        cmd = ["ffmpeg", "-y"]
        for p in paths:
            cmd.extend(["-i", p])

        # 對每個 input 做 normalization：縮放、補黑邊到目標 W×H、setpts、fps、format yuv420p
        # 為什麼要 per-input 偵測 audio：若任一 input 沒 audio 軌、aresample 會 fail；
        # 我們用 anullsrc 補一條靜音軌讓 concat filter 對齊。
        parts = []
        v_labels = []
        a_labels = []
        durations = [_pd(p) or 0.0 for p in paths]
        # Probe 失敗或 0 長度 → 直接 raise。
        # 為什麼不退階：apad=whole_dur=0 / atrim=duration=0 / xfade offset=0 都會讓
        # FFmpeg 跑出晦澀錯誤（filter graph 內部失敗），訊息不會點出真正原因（probe）。
        # 早 raise 給使用者「為什麼 probe 失敗」的線索（path 拼錯？codec ffprobe 不認？）。
        bad = [paths[i] for i, d in enumerate(durations) if d <= 0]
        if bad:
            raise RuntimeError(
                f"[Concat Videos] ffprobe 取不到 video 時長（{bad}）。"
                "可能是檔案損毀 / 容器 ffprobe 不認 / 路徑含特殊字元。"
                "改用 mode=copy 跳過 probe，或先用 MF_ProbeMedia 確認檔案可解析。"
            )
        has_audio_flags = [
            bool(info and any(s.get("codec_type") == "audio" for s in info.get("streams", [])))
            for info in (_probe(p) for p in paths)
        ]

        for i in range(n):
            parts.append(
                f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
                f"format=yuv420p,setpts=PTS-STARTPTS[v{i}]"
            )
            if has_audio_flags[i]:
                # R5 P2 fix + R10 P2 fix：
                # - R5: atrim 截到 video 長 (避免長 audio 把 concat 拉長)
                # - R10: 先 apad 補到 video 長 (避免短 audio 跟 xfade seam 對不齊)
                # 順序：aresample → apad to video length → atrim to same length。
                # 這跟 loop_video 的 base_a 處理方式一致。
                parts.append(
                    f"[{i}:a]aresample=async=1:first_pts=0,"
                    f"apad=whole_dur={durations[i]:.6f},"
                    f"atrim=duration={durations[i]:.6f},asetpts=PTS-STARTPTS[a{i}]"
                )
            else:
                # 該 input 沒 audio → 生 stereo 44.1kHz 靜音、長度匹配
                parts.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=44100,"
                    f"atrim=duration={durations[i]:.6f}[a{i}]"
                )
            v_labels.append(f"v{i}")
            a_labels.append(f"a{i}")

        # R8 P2 fix：transition clamp 必須只用 shortest*0.99（無 floor）；clip 太短 → 直接 disable
        use_transition = False
        effective_xfade = 0.0
        if transition_sec > 0 and n >= 2:
            shortest = min(durations) if durations else transition_sec
            candidate = min(transition_sec, shortest * 0.99)
            if candidate <= 1e-3:
                print(
                    f"[Concat Videos] 注意：clip 最短 {shortest:.3f}s 太短，無法做 transition、"
                    "改用純 concat（無淡接）"
                )
            else:
                use_transition = True
                effective_xfade = candidate
                if effective_xfade < transition_sec - 1e-6:
                    print(
                        f"[Concat Videos] 注意：transition_sec={transition_sec}s 比最短 clip "
                        f"({shortest:.2f}s) 還長，clamped 到 {effective_xfade:.3f}s"
                    )
        if use_transition:

            # chained xfade — 每多一段，total len 增加 (dur_i - effective_xfade)
            offset = max(0.0, durations[0] - effective_xfade)
            prev_v = v_labels[0]
            prev_a = a_labels[0]
            for i in range(1, n):
                out_v = f"xv{i}" if i < n - 1 else "outv"
                out_a = f"xa{i}" if i < n - 1 else "outa"
                parts.append(
                    f"[{prev_v}][{v_labels[i]}]xfade=transition={transition_type}:"
                    f"duration={effective_xfade:.6f}:offset={offset:.6f}[{out_v}]"
                )
                parts.append(
                    f"[{prev_a}][{a_labels[i]}]acrossfade=d={effective_xfade:.6f}[{out_a}]"
                )
                prev_v, prev_a = out_v, out_a
                offset += max(0.0, durations[i] - effective_xfade)
        else:
            # 純 concat
            parts.append(
                "".join(f"[{v}][{a}]" for v, a in zip(v_labels, a_labels))
                + f"concat=n={n}:v=1:a=1[outv][outa]"
            )

        cmd.extend([
            "-filter_complex", ";".join(parts),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            output_path,
        ])
        if not run_ffmpeg(cmd, tag="Concat Videos"):
            raise RuntimeError("[Concat Videos] transcode 模式失敗，請查看上方 stderr 輸出。")


NODE_CLASS_MAPPINGS = {"MF_ConcatVideos": MF_ConcatVideos}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ConcatVideos": "🔗 Concat Videos"}
