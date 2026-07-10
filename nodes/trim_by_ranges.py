"""MF_TrimByRanges — 依 list of [start, end] 範圍切影片。

v2.1 ROADMAP Phase 3。
- mode='keep'：保留 ranges 內的片段，concat 起來
- mode='remove'：移除 ranges (= MF_DetectSilence 下游典型用法，移除靜音段)
"""
import json
import os
import shutil
import tempfile

from ..utils.cache_key import path_fingerprint
from ..utils.encoder import build_encoder_args, get_available_codecs, pick_default_codec
from ..utils.ffmpeg import (
    ensure_ffmpeg,
    escape_filter_path,
    probe,
    probe_has_audio_stream,
    probe_keyframe_times,
    probe_video_duration,
    run_ffmpeg,
    unmapped_stream_descriptions,
)
from ..utils.output_path import output_path_to_ui_entry, resolve_output_path, source_container_ext
from ..utils.video_io import encode_tensor_to_tempfile, mux_path_with_audio_dict


class MF_TrimByRanges:
    @classmethod
    def INPUT_TYPES(s):
        codec_choices = list(get_available_codecs().keys())
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                "filename_prefix": ("STRING", {"default": "MediaForge/trimmed"}),
                "mode": (["keep", "remove"], {"default": "remove"}),
                # 兩條 input path：(1) SILENCE_RANGES 連線；(2) 手寫 JSON fallback
                "ranges_json": (
                    "STRING",
                    {"default": "[[0.0, 1.0], [5.0, 7.5]]", "multiline": True},
                ),
                # crossfade 接縫時的淡入淡出 sec；0 = 直接 cut
                "crossfade_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 2.0, "step": 0.05}),
                # P1-4：codec / crf / preset 與其他 file-producer 對齊
                "codec": (codec_choices, {"default": pick_default_codec()}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
                "preset": (
                    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"],
                    {"default": "medium"},
                ),
            },
            "optional": {
                "ranges": ("SILENCE_RANGES",),
                # In-memory chain: 連 frames 後改走 tensor → temp mp4 → trim 流程
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),
                # C2 fix：ComfyUI 存檔的 widgets_values 是「完整 widget 順序」的
                # positional array——required 全部 widget（依宣告順序）+ optional
                # 中「實際有 widget」的項目（依宣告順序；ranges/frames/audio 是純
                # link 型別，不佔位置）。這個節點的 optional 裡 tensor_fps 是
                # FLOAT widget，加 precision 前的完整順序是 8 個 required +
                # tensor_fps = 9 個值。precision 若放 required 尾端（F1 舊解法），
                # 會插在 tensor_fps 前面、把它擠到第 10 位——舊存檔 widgets_values[8]
                # （tensor_fps=30.0）被錯位餵給 precision（COMBO），直接觸發
                # "Value not in list"，每個舊 TrimByRanges workflow 一載入就壞
                # （這是實際發生過的 regression，見 git history）。改放 optional
                # 尾端（在 audio 之後宣告）才能維持舊 9 值前綴不變、precision 自己
                # 落到 default。日後新 widget 一律 append 到「完整 widget 順序」最
                # 尾端——這個 node 的 optional 已經有 widget，新 widget 要放
                # optional 尾端而非 required 尾端。
                # W3-1：precise = 現行 trim+setpts re-encode；lossless = stream
                # copy 切段 + concat demuxer 合併，無 re-encode 但切點只能對齊來源
                # keyframe。
                "precision": (
                    ["precise (re-encode)", "lossless (stream copy)"],
                    {"default": "precise (re-encode)"},
                ),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "trim"
    CATEGORY = "MediaForge/Video"

    def trim(self, video_path, filename_prefix, mode, ranges_json, crossfade_sec,
             codec="h264 (libx264)", crf=18, preset="medium", precision="precise (re-encode)",
             ranges=None,
             frames=None, tensor_fps=30.0, audio=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Trim By Ranges] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        # R2-2 fix：crossfade_sec 在 precision=lossless 時被前端隱藏
        # (web/dual_input_lock.js WIDGET_VALUE_LOCKS)，使用者切到 lossless 後看不到這個
        # widget，殘留的非 0 值（例如先在 precise 模式設過再切模式）若讓它 raise，
        # 使用者會看不到造成錯誤的值 —— 是 dead-UI 陷阱。lossless 的可見意圖就是
        # 「不 re-encode」，忽略 crossfade_sec、印警告後照常硬切輸出，符合 CLAUDE.md
        # graceful degradation 判準（輸出是使用者依 UI 表現可預期接受的東西）。
        is_lossless = precision == "lossless (stream copy)"
        if is_lossless and crossfade_sec > 0:
            print(
                f"[Trim By Ranges] 注意：precision=lossless (stream copy) 無法做 crossfade "
                f"淡接（stream copy 無法混合像素），已忽略 crossfade_sec={crossfade_sec}。"
                "要淡接請切回 precision=precise (re-encode)。"
            )

        cleanup_tmp = None
        try:
            # Dual-input dispatch（與 BurnSubtitle / LoopVideo 的 source resolve 對稱）：
            # path mode + audio dict 同時接時要 pre-mux，否則 audio 會 silently drop
            if frames is not None:
                source_path = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
                cleanup_tmp = source_path
            else:
                if not os.path.exists(video_path):
                    raise FileNotFoundError(f"[Trim By Ranges] 找不到影片：{video_path}")
                if audio is not None:
                    source_path = mux_path_with_audio_dict(video_path, audio)
                    cleanup_tmp = source_path
                else:
                    source_path = video_path

            # ProRes 需要 .mov 容器才能 mux 成功 (W1-6)；_build_concat_command 內部另外查一次
            # codec_id 供 encoder args 用 — 這裡只是為了決定副檔名，不影響下游那份查表。
            # W3-1：lossless 模式不轉容器，副檔名沿用來源（stream copy 語意上就是「跟來源
            # 一致」），codec/crf/preset widget 在這個分支被忽略。F3：共用 helper 抽到
            # utils/output_path.py，concat_videos.py 的 copy 模式也用同一份。
            # C7 fix：path 模式 + AUDIO dict 時 source_path 其實是
            # mux_path_with_audio_dict() 產出的 .mkv transit 檔（temp 容器固定
            # .mkv，見 utils/video_io.py R3-1 說明），跟使用者填的 video_path 副檔名
            # 無關。沿用 source_path 的副檔名會讓「.mp4 來源」的 lossless 輸出悄悄
            # 變成 .mkv，違反 README 承諾的「沿用來源副檔名」、也弄壞下游 mp4-only
            # pipeline。改成沿用「使用者實際輸入」的副檔名：tensor 模式沒有
            # video_path 可言，沿用 source_path 本身（encode_tensor_to_tempfile
            # 固定產出 .mp4，沒有這個問題）；path 模式一律沿用 video_path。唯一
            # 例外：video_path 是 .webm 且外接了 AUDIO dict —— transit 檔內的音軌是
            # AAC（mux_path_with_audio_dict 寫死 `-c:a aac`），WebM 容器不支援
            # AAC，只能退回 transit 檔本身的 .mkv 並印出說明。
            if is_lossless:
                if frames is not None:
                    ext = source_container_ext(source_path)
                else:
                    ext = source_container_ext(video_path)
                    if ext == ".webm" and audio is not None:
                        ext = ".mkv"
                        print(
                            "[Trim By Ranges] 注意：來源是 .webm 但外接了 AUDIO dict，"
                            "混音後音軌是 AAC（WebM 容器不支援），lossless 輸出改用 "
                            ".mkv 容器。"
                        )
            else:
                codec_map = get_available_codecs()
                codec_id, _pix_fmt = codec_map.get(codec, codec_map["h264 (libx264)"])
                ext = ".mov" if codec_id == "prores_ks" else ".mp4"
            output_path = resolve_output_path(filename_prefix, ext)

            # R7 P1 fix：連線到 MF_DetectSilence 的 ranges=[] (沒偵到靜音) 不能 fallback 到
            # ranges_json (那是 disconnect 時的手填預設)。用 `is not None` 區分「連了空 list」
            # 與「沒連」。
            if ranges is not None:
                effective_ranges = list(ranges)  # 可能是空 list
            else:
                effective_ranges = self._parse_ranges_json(ranges_json)

            duration = probe_video_duration(source_path)
            if duration is None or duration <= 0:
                raise RuntimeError(
                    f"[Trim By Ranges] 無法讀取影片長度：{source_path}。"
                    "請先用 MF_ProbeMedia 驗證檔案可解析"
                )

            # mode=remove + 空 ranges → 整段保留（identity），是合法 no-op
            if mode == "remove":
                if not effective_ranges:
                    print("[Trim By Ranges] 注意：ranges 為空 + mode=remove，保留整段影片 (identity)")
                    keep_ranges = [[0.0, duration]]
                else:
                    keep_ranges = _complement_ranges(effective_ranges, duration)
            else:  # keep
                if not effective_ranges:
                    raise ValueError(
                        "[Trim By Ranges] mode=keep + ranges 為空：沒有東西可保留。"
                        "若是想保留整段請改用 mode=remove。"
                    )
                keep_ranges = [list(r) for r in effective_ranges]

            keep_ranges = _normalize_ranges(keep_ranges, duration)
            if not keep_ranges:
                raise ValueError(
                    f"[Trim By Ranges] mode={mode} 處理後沒有剩下任何片段（檢查 ranges 是否覆蓋整段影片）"
                )

            has_audio = probe_has_audio_stream(source_path)

            if is_lossless:
                self._trim_lossless(source_path, output_path, keep_ranges)
            else:
                cmd = self._build_concat_command(
                    source_path, output_path, keep_ranges, crossfade_sec, has_audio,
                    codec=codec, crf=crf, preset=preset,
                )
                if not run_ffmpeg(cmd, tag="Trim By Ranges"):
                    raise RuntimeError("[Trim By Ranges] FFmpeg 失敗，請查看上方 stderr 輸出。")
        finally:
            if cleanup_tmp:
                try:
                    os.unlink(cleanup_tmp)
                except OSError:
                    pass

        print(f"[Trim By Ranges] 輸出成功（{len(keep_ranges)} 個片段，audio={has_audio}）: {output_path}")
        return {"ui": {"images": [output_path_to_ui_entry(output_path)]}, "result": (output_path,)}

    @staticmethod
    def _parse_ranges_json(text):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"[Trim By Ranges] ranges_json 解析失敗：{e}") from e
        if not isinstance(data, list):
            raise ValueError(f"[Trim By Ranges] ranges_json 必須是 list，但拿到 {type(data).__name__}")
        out = []
        for item in data:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                raise ValueError(f"[Trim By Ranges] 每個 range 必須是 [start, end]，但拿到 {item}")
            out.append([float(item[0]), float(item[1])])
        return out

    @staticmethod
    def _build_concat_command(video_path, output_path, keep_ranges, xfade, has_audio,
                              *, codec="h264 (libx264)", crf=18, preset="medium"):
        # 對每個 keep range 用 trim+setpts 抽出來。filter graph 路徑不必 escape (是 -i)。
        parts = []
        seg_durations = []
        for i, (s, e) in enumerate(keep_ranges):
            parts.append(
                f"[0:v]trim=start={s:.6f}:end={e:.6f},setpts=PTS-STARTPTS[v{i}]"
            )
            if has_audio:
                parts.append(
                    f"[0:a]atrim=start={s:.6f}:end={e:.6f},asetpts=PTS-STARTPTS[a{i}]"
                )
            seg_durations.append(max(0.0, e - s))

        n = len(keep_ranges)
        if xfade > 0 and n < 2:
            # 單段沒 seam，xfade 無意義 — 直接 concat 並提示
            print(f"[Trim By Ranges] 注意：只有 1 段、xfade={xfade} 無作用，改用直接 concat")
            xfade = 0.0

        if xfade > 0:
            # 對每個接縫做 chained xfade — duration 不能超過任一相鄰段的長度 - 一點 padding
            # （xfade 期間兩段都要存在）；超出時 clamp。
            effective_xfade = min(xfade, *seg_durations) * 0.99
            if effective_xfade < xfade - 1e-6:
                print(
                    f"[Trim By Ranges] 注意：xfade={xfade:.2f}s 比最短段還長，clamped to {effective_xfade:.3f}s"
                )

            # chained xfade：offset 是「當前累積長度 - xfade」
            prev_v = "[v0]"
            prev_a = "[a0]" if has_audio else None
            accum = seg_durations[0]
            for i in range(1, n):
                cur_v = f"[v{i}]"
                out_v = "[outv]" if i == n - 1 else f"[xv{i}]"
                offset = max(0.0, accum - effective_xfade)
                parts.append(
                    f"{prev_v}{cur_v}xfade=transition=fade:"
                    f"duration={effective_xfade:.6f}:offset={offset:.6f}{out_v}"
                )
                if has_audio:
                    cur_a = f"[a{i}]"
                    out_a = "[outa]" if i == n - 1 else f"[xa{i}]"
                    parts.append(f"{prev_a}{cur_a}acrossfade=d={effective_xfade:.6f}{out_a}")
                    prev_a = out_a
                prev_v = out_v
                # 累積長度：(prev_total + new_seg) - xfade 被重疊
                accum += seg_durations[i] - effective_xfade
            maps = ["-map", "[outv]"]
            if has_audio:
                maps.extend(["-map", "[outa]"])
            else:
                maps.append("-an")
        else:
            # 純 concat 路徑 — per-segment 交錯：[v0][a0][v1][a1]...
            if has_audio:
                interleaved = "".join(f"[v{i}][a{i}]" for i in range(n))
                parts.append(f"{interleaved}concat=n={n}:v=1:a=1[outv][outa]")
                maps = ["-map", "[outv]", "-map", "[outa]"]
            else:
                interleaved = "".join(f"[v{i}]" for i in range(n))
                parts.append(f"{interleaved}concat=n={n}:v=1:a=0[outv]")
                maps = ["-map", "[outv]", "-an"]

        _ = escape_filter_path  # 留 import，避免 ruff F401 未來重構時刪掉；此節點 -i 路徑不入 filter
        # P1-4：解析 codec choice → encoder args (含 NVENC ↔ x264 preset 自動映射)
        codec_map = get_available_codecs()
        codec_id, default_pix_fmt = codec_map.get(codec, codec_map["h264 (libx264)"])
        encoder_args = build_encoder_args(codec_id, crf=crf, preset=preset)
        return [
            "ffmpeg", "-y", "-i", video_path,
            "-filter_complex", ";".join(parts),
            *maps,
            *encoder_args,
            "-pix_fmt", default_pix_fmt,
            output_path,
        ]

    @staticmethod
    def _trim_lossless(source_path, output_path, keep_ranges):
        # C3+C6：mapping 合約收斂成「主影音 + 字幕」（見下方 -map 0:V ...），
        # data/timecode/封面圖軌會被略過——先警告，取代舊版的 silent drop。
        info = probe(source_path)
        if info:
            skipped = unmapped_stream_descriptions(info)
            if skipped:
                print(
                    "[Trim By Ranges] 注意：lossless 模式僅保留主影音與字幕軌，"
                    f"已略過以下 stream：{source_path}：{', '.join(skipped)}"
                )

        # R6-1 [P1] fix：stream copy 只能在 keyframe 上切。原本 `-ss {s} -to {e}` 都是
        # input-side seek——FFmpeg 會把 `-ss` 往前退到 s 之前最近的 keyframe，但 `-to`
        # 是絕對終點不變，GOP 稀疏時等於把 mode=remove 判定要移除的內容重新包進輸出
        # （甚至整段重複）。改成「前向」對齊：每個 keep range 的起點只准往後挪到下一顆
        # keyframe，絕不往前擴進已移除區間；range 內完全沒有可用 keyframe 就 raise
        # （往前擴或悄悄跳過都會產生錯誤內容，不是可接受的 graceful degradation）。
        #
        # C10：keyframe 掃描要 decode 整段（只解 keyframe但仍需通讀整個 stream），
        # 長素材上不是瞬間完成——先告知使用者，避免以為卡住。
        print(f"[Trim By Ranges] 正在掃描來源 keyframe（長素材可能需要一段時間）：{source_path}")
        keyframes = probe_keyframe_times(source_path)
        if not keyframes:
            raise RuntimeError(
                "[Trim By Ranges] 無法取得來源影片的 keyframe 資訊，lossless 模式需要"
                "keyframe 對齊才能安全切段，請改用 precision=precise (re-encode)。"
            )

        adjusted = []
        for i, (s, e) in enumerate(keep_ranges):
            candidates = [k for k in keyframes if k >= s - 1e-3]
            if not candidates or candidates[0] >= e - 1e-3:
                raise RuntimeError(
                    f"[Trim By Ranges] 第 {i + 1} 段 keep range [{s:.2f}s, {e:.2f}s] "
                    "內沒有可用的 keyframe，lossless 模式無法保留此段。請改用 "
                    "precision=precise (re-encode)，或調整 ranges 讓每段至少涵蓋一顆 "
                    "keyframe。"
                )
            adjusted.append((s, candidates[0], e))

        detail = ", ".join(
            f"第{i + 1}段 {s:.3f}s→{s_adj:.3f}s(+{max(0.0, s_adj - s):.3f}s)"
            for i, (s, s_adj, _e) in enumerate(adjusted)
        )
        print(
            "[Trim By Ranges] 注意：precision=lossless 用 stream copy 切段，keep 段起點"
            f"已向後對齊至最近的來源 keyframe（絕不往前擴進已移除內容）：{detail}"
        )

        ext = os.path.splitext(output_path)[1] or ".mp4"
        tmpdir = tempfile.mkdtemp(prefix="mf_trim_lossless_")
        try:
            seg_paths = []
            for i, (s, s_adj, e) in enumerate(adjusted):
                seg_path = os.path.join(tmpdir, f"seg_{i}{ext}")
                cmd = [
                    "ffmpeg", "-y",
                    # +0.5ms 微偏移保證 input seek 精確落在 s_adj 這顆 keyframe 上，
                    # 不會因浮點數 timestamp 誤差又回退一顆；`-t`（相對秒數，取代
                    # `-to` 絕對終點）從實際 seek 到的位置起算，避免 input-side `-to`
                    # 的版本語意分歧。結尾切在非 keyframe 是安全的——段首是 keyframe、
                    # 後續 frame 順序可解。
                    "-ss", f"{s_adj + 0.0005:.6f}",
                    "-i", source_path,
                    "-t", f"{e - s_adj:.6f}",
                    # R4-2 fix：不加 -map 只會抽到預設音軌，雙音軌來源（如英文 +
                    # 評論軌 mkv）在切段當下就已經丟軌——「lossless」掉軌非常違反直覺。
                    # C3+C6 fix：`-map 0`（全部 stream）改收斂成「主影音 + 字幕」——
                    # data/timecode stream（GoPro/DJI 的 tmcd/gpmd）塞進 `-c copy`
                    # 會直接 exit 失敗，demuxer/muxer 給不了這類 stream 該有的 codec
                    # 參數。`0:V` 排除 attached_pic（封面圖）；`?` 讓沒有音軌/字幕軌
                    # 時不會因為 map 不到而報錯。
                    "-map", "0:V", "-map", "0:a?", "-map", "0:s?",
                    "-c", "copy", "-avoid_negative_ts", "make_zero",
                    seg_path,
                ]
                if not run_ffmpeg(cmd, tag="Trim By Ranges"):
                    raise RuntimeError(
                        f"[Trim By Ranges] lossless 切段失敗（第 {i + 1} 段 "
                        f"{s_adj:.2f}s-{e:.2f}s），請查看上方 stderr 輸出。"
                    )
                seg_paths.append(seg_path)

            # concat demuxer list 檔 — 同 ConcatVideos._concat_demuxer 的 single-quote escape 寫法
            list_path = os.path.join(tmpdir, "concat_list.txt")
            with open(list_path, "w", encoding="utf-8") as f:
                for p in seg_paths:
                    safe = os.path.abspath(p).replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")

            concat_cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                # R4-2 fix：段檔本身可能保留了多軌（上面 per-segment 已加 -map），
                # 合併時同樣要 map 才不會在這一步二次丟軌。C3+C6 fix：與分段 cmd
                # 一致收斂成「主影音 + 字幕」——段檔本來就不含 data/attached_pic
                # （上一步已經略過），這裡維持同款合約單純是介面一致、無額外副作用。
                "-i", list_path, "-map", "0:V", "-map", "0:a?", "-map", "0:s?",
                "-c", "copy", output_path,
            ]
            if not run_ffmpeg(concat_cmd, tag="Trim By Ranges"):
                raise RuntimeError("[Trim By Ranges] lossless 合併片段失敗，請查看上方 stderr 輸出。")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # C1 fix：ComfyUI IS_CHANGED 收到的 linked 輸入（frames/audio）永遠是
        # None，用它判斷 tensor 模式是 dead code。無條件 fingerprint video_path
        # ——tensor 模式下沒被讀，fingerprint 它只是無害的多餘敏感度。
        return path_fingerprint(kwargs.get("video_path", ""))


def _complement_ranges(ranges, duration):
    """[remove_ranges] → keep ranges = (0, duration) 扣掉 remove 段。"""
    sorted_r = sorted([(max(0.0, s), min(duration, e)) for s, e in ranges if e > s])
    if not sorted_r:
        return [[0.0, duration]]

    keep = []
    cursor = 0.0
    for s, e in sorted_r:
        if s > cursor:
            keep.append([cursor, s])
        cursor = max(cursor, e)
    if cursor < duration:
        keep.append([cursor, duration])
    return keep


def _normalize_ranges(ranges, duration):
    """Clamp 到 [0, duration]、過濾無效（start >= end）。"""
    out = []
    for s, e in ranges:
        s = max(0.0, float(s))
        e = min(duration, float(e))
        if e - s > 1e-4:  # 短於 0.1ms 的片段視為 noise
            out.append([s, e])
    return out


NODE_CLASS_MAPPINGS = {"MF_TrimByRanges": MF_TrimByRanges}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_TrimByRanges": "✂️ Trim by Ranges"}
