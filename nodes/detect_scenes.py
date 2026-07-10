"""MF_DetectScenes — FFmpeg scene-change 偵測，回傳 list of scene ranges。

Roadmap FrameSampler 的 thin-FFmpeg 子集（生態缺口同 MF_DetectSilence：純 FFmpeg
`scene` filter 表達式偵測畫面突變分數，不做自訂 frame-diff 演算法）。

RETURN_TYPES 用 "SILENCE_RANGES"（歷史命名）而非 "SCENE_RANGES" —— 這是 MediaForge
自訂連線型別，本質上只是 [[start, end], ...] 的 list-of-pair 契約，跟 MF_DetectSilence
輸出的 range 型別完全相容；沿用同一個型別字串讓 MF_TrimByRanges 不必多接受一種
型別就能直接吃 DetectScenes 的輸出。
"""
import json
import os
import re
import subprocess

from ..utils.cache_key import path_fingerprint
from ..utils.ffmpeg import ensure_ffmpeg, probe_video_duration, resolve_ffmpeg_cmd


# showinfo 輸出例：
#   [Parsed_showinfo_1 @ 0x...] n:  30 pts:     30 pts_time:3.0   duration: ...
# select='gt(scene,threshold)' 只放行場景分數超過門檻的幀，所以每一行 showinfo
# 對應的 pts_time 就是一個場景切點。
PTS_TIME_RE = re.compile(r"pts_time:([\d.]+)")


class MF_DetectScenes:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                # scene 分數門檻（ffmpeg `scene` filter 表達式輸出 0.0-1.0）；
                # 越低越敏感，容易把小幅度畫面變化也判定為切點。
                "threshold": ("FLOAT", {"default": 0.4, "min": 0.05, "max": 1.0, "step": 0.05}),
                # 相鄰場景短於此秒數併入前一段，避免雜訊切點把輸出切得太碎。
                "min_scene_sec": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 60.0, "step": 0.1}),
            },
        }

    RETURN_TYPES = ("SILENCE_RANGES", "STRING", "INT")
    RETURN_NAMES = ("scene_ranges", "ranges_json", "count")
    FUNCTION = "detect"
    CATEGORY = "MediaForge/Analysis"

    def detect(self, video_path, threshold, min_scene_sec):
        if not ensure_ffmpeg():
            raise RuntimeError("[Detect Scenes] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"[Detect Scenes] 找不到輸入檔：{video_path}")

        duration = probe_video_duration(video_path)
        if duration is None or duration <= 0:
            raise RuntimeError(
                f"[Detect Scenes] 無法讀取影片長度：{video_path}。"
                "請先用 MF_ProbeMedia 驗證檔案可正常解析"
            )

        cmd = resolve_ffmpeg_cmd([
            "ffmpeg", "-v", "info",  # showinfo 訊息走 stderr at info level（同 DetectSilence 慣例）
            "-i", video_path,
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-an",
            "-f", "null", "-",
        ])
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-30:])
            raise RuntimeError(f"[Detect Scenes] FFmpeg 失敗 (exit {proc.returncode}):\n{tail}")

        cut_points = _parse_scene_cuts(proc.stderr or "", duration=duration)
        ranges = _build_scene_ranges(cut_points, duration)
        ranges = _merge_short_scenes(ranges, min_scene_sec)

        ranges_json = json.dumps(ranges)
        print(
            f"[Detect Scenes] 偵測到 {len(ranges)} 個場景 "
            f"(threshold={threshold}, min_scene_sec={min_scene_sec})"
        )
        return (ranges, ranges_json, len(ranges))

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return path_fingerprint(kwargs.get("video_path", ""))


def _parse_scene_cuts(log, duration=None):
    """Parse showinfo stderr → sorted、去重的場景切點時間戳（秒）。

    pts_time<=0（影片第一幀，沒有「前一幀」可比較 scene 分數）不是有意義的切點；
    duration 給定時順便濾掉浮點誤差可能落在片長之外的雜訊值。
    """
    times = [float(m.group(1)) for m in PTS_TIME_RE.finditer(log)]
    times = [t for t in times if t > 0]
    if duration is not None:
        times = [t for t in times if t < duration]
    return sorted(set(times))


def _build_scene_ranges(cut_points, duration):
    """切點 list + 總長 → [[0,t1],[t1,t2],...,[tn,duration]]；無切點 → [[0,duration]]。"""
    boundaries = [0.0, *cut_points, duration]
    return [[boundaries[i], boundaries[i + 1]] for i in range(len(boundaries) - 1)]


def _merge_short_scenes(ranges, min_scene_sec):
    """相鄰場景長度 < min_scene_sec 併入前一段；純函式，方便 unit test（W3-3）。

    第一段本身太短（沒有「前段」可併）是唯一沒辦法照「併入前段」字面規則處理的邊界
    情況，改併入下一段。min_scene_sec<=0 時永遠不合併（片段長度必為非負，
    `< 0` 恆假）。
    """
    if len(ranges) <= 1:
        return [list(r) for r in ranges]

    merged = [list(ranges[0])]
    for start, end in ranges[1:]:
        if (end - start) < min_scene_sec:
            merged[-1][1] = end  # 併入前一段：延伸前一段的 end
        else:
            merged.append([start, end])

    if len(merged) > 1 and (merged[0][1] - merged[0][0]) < min_scene_sec:
        merged[1][0] = merged[0][0]
        merged.pop(0)

    return merged


NODE_CLASS_MAPPINGS = {"MF_DetectScenes": MF_DetectScenes}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_DetectScenes": "🎞️ Detect Scenes"}
