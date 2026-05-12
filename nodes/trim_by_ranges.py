"""MF_TrimByRanges — 依 list of [start, end] 範圍切影片。

v2.1 ROADMAP Phase 3。
- mode='keep'：保留 ranges 內的片段，concat 起來
- mode='remove'：移除 ranges (= MF_DetectSilence 下游典型用法，移除靜音段)
"""
import json
import os

from ..utils.ffmpeg import ensure_ffmpeg, escape_filter_path, probe, probe_video_duration, run_ffmpeg


class MF_TrimByRanges:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                "output_path": ("STRING", {"default": "output/trimmed.mp4"}),
                "mode": (["keep", "remove"], {"default": "remove"}),
                # 兩條 input path：(1) SILENCE_RANGES 連線；(2) 手寫 JSON fallback
                "ranges_json": (
                    "STRING",
                    {"default": "[[0.0, 1.0], [5.0, 7.5]]", "multiline": True},
                ),
                # crossfade 接縫時的淡入淡出 sec；0 = 直接 cut
                "crossfade_sec": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 2.0, "step": 0.05}),
            },
            "optional": {
                "ranges": ("SILENCE_RANGES",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "trim"
    CATEGORY = "MediaForge/Video"

    def trim(self, video_path, output_path, mode, ranges_json, crossfade_sec, ranges=None):
        if not ensure_ffmpeg():
            raise RuntimeError("[Trim By Ranges] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"[Trim By Ranges] 找不到影片：{video_path}")

        # R7 P1 fix：連線到 MF_DetectSilence 的 ranges=[] (沒偵到靜音) 不能 fallback 到
        # ranges_json (那是 disconnect 時的手填預設)。用 `is not None` 區分「連了空 list」
        # 與「沒連」。
        if ranges is not None:
            effective_ranges = list(ranges)  # 可能是空 list
        else:
            effective_ranges = self._parse_ranges_json(ranges_json)

        duration = probe_video_duration(video_path)
        if duration is None or duration <= 0:
            raise RuntimeError(f"[Trim By Ranges] 無法讀取影片長度：{video_path}")

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

        info = probe(video_path)
        has_audio = bool(info and any(s.get("codec_type") == "audio" for s in info.get("streams", [])))

        cmd = self._build_concat_command(video_path, output_path, keep_ranges, crossfade_sec, has_audio)
        if not run_ffmpeg(cmd, tag="Trim By Ranges"):
            raise RuntimeError("[Trim By Ranges] FFmpeg 失敗，請查看上方 stderr 輸出。")
        print(f"[Trim By Ranges] 輸出成功（{len(keep_ranges)} 個片段，audio={has_audio}）: {output_path}")
        return (output_path,)

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
    def _build_concat_command(video_path, output_path, keep_ranges, xfade, has_audio):
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
        return [
            "ffmpeg", "-y", "-i", video_path,
            "-filter_complex", ";".join(parts),
            *maps,
            output_path,
        ]


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
