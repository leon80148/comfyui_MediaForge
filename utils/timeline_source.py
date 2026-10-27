"""Timeline 來源解析與 FFmpeg 安全輸入（route、MF_EditTimeline、MF_LoadSegmentFrames 共用）。

只接受 ComfyUI input/ 內的相對路徑；FFmpeg 一律以 `file:` 協定 + demuxer 白名單
開檔——使用者給的名字可能是 `concat:` / `http:` 或偽裝成 .mp4 的 playlist，不能交給
FFmpeg 自動判斷。不沿用 `resolve_output_path()`（刻意允許跳出 output/）或
`MF_SelectVideo.select()`（只做 join、沒有 containment）。
"""
import hashlib
import json
import os
import re
import subprocess
from fractions import Fraction

from .cache_key import path_fingerprint
from .ffmpeg import resolve_ffmpeg_cmd

DEMUXER_WHITELIST = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts,mpeg"
FFMPEG_INPUT_GUARD = ["-protocol_whitelist", "file", "-format_whitelist", DEMUXER_WHITELIST]
PROBE_TIMEOUT_SEC = 10
MAX_NAME_BYTES = 255
MAX_PATH_BYTES = 1024

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_ANNOTATION = re.compile(r"\s\[(input|output|temp)\]$")


def check_relative_name(rel):
    """拒絕任何不是「input/ 內乾淨相對路徑」的名字；回傳路徑元件 list。"""
    if not isinstance(rel, str) or not rel:
        raise ValueError("來源路徑必須是非空字串")
    if len(rel.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError("來源路徑過長")
    if any(ord(c) < 32 or ord(c) == 127 for c in rel):
        raise ValueError("來源路徑含控制字元")
    # `:` 一併擋掉磁碟代號、NTFS ADS 與 `file:` / `concat:` 之類的協定前綴
    if ":" in rel or _ANNOTATION.search(rel) or rel[0] in "/\\":
        raise ValueError("來源必須是 input/ 內的相對路徑")
    parts = re.split(r"[\\/]", rel)
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError("來源路徑含空白、`.` 或 `..` 元件")
        if part[-1] in ". " or len(part.encode("utf-8")) > MAX_NAME_BYTES:
            raise ValueError("來源路徑元件結尾不可為點或空白，且不可過長")
        if part.split(".")[0].upper() in _WINDOWS_RESERVED:
            raise ValueError("來源路徑含 Windows 保留名稱")
    return parts


def resolve_timeline_source(rel, input_dir=None):
    """input/ 相對路徑 → 絕對路徑。symlink 允許，但 realpath 後必須仍在 realpath(input/) 內。"""
    parts = check_relative_name(rel)
    if input_dir is None:
        import folder_paths
        input_dir = folder_paths.get_input_directory()
    root = os.path.realpath(input_dir)
    path = os.path.realpath(os.path.join(root, *parts))
    if os.path.normcase(os.path.commonpath([root, path])) != os.path.normcase(root):
        raise ValueError("來源路徑解析後不在 input/ 內")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到來源影片：input/{rel}")
    return path


def ffmpeg_input_args(path):
    return FFMPEG_INPUT_GUARD + ["-i", "file:" + path]


def opaque_fingerprint(path):
    """對外的來源指紋：`path_fingerprint` 含絕對路徑，不能直接進 plan_json 或回給瀏覽器。"""
    return hashlib.sha256(path_fingerprint(path).encode("utf-8")).hexdigest()


def _rate(text):
    try:
        value = Fraction(text or "0/1")
    except (ValueError, ZeroDivisionError):
        return None
    return [value.numerator, value.denominator] if value > 0 else None


def _micros(text):
    try:
        return round(float(text) * 1_000_000)
    except (TypeError, ValueError):
        return None


def _timing(video):
    avg, real = _rate(video.get("avg_frame_rate")), _rate(video.get("r_frame_rate"))
    if avg is None or real is None:
        return "unknown"
    return "cfr" if avg == real else "vfr"


def probe_command(path):
    return resolve_ffmpeg_cmd(
        ["ffprobe", "-v", "error"] + ffmpeg_input_args(path)
        + ["-show_entries", "stream=codec_type,codec_name,avg_frame_rate,r_frame_rate,nb_frames,start_time,width,height,"
           "sample_aspect_ratio,sample_rate,channels:stream_side_data=rotation:format=duration,start_time", "-of", "json"]
    )


def probe_source(path):
    """ffprobe（帶白名單與 timeout）→ 時間軸需要的來源 metadata；欄位未知時為 None。"""
    try:
        proc = subprocess.run(probe_command(path), capture_output=True, timeout=PROBE_TIMEOUT_SEC, check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"[Timeline] ffprobe 逾時（>{PROBE_TIMEOUT_SEC}s）：{os.path.basename(path)}") from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:])
        raise RuntimeError(f"[Timeline] 無法讀取來源影片（格式不在允許清單或檔案損毀）：\n{tail}")
    info = parse_probe_output(proc.stdout)
    if info is None:
        raise RuntimeError(f"[Timeline] 來源沒有影像串流：{os.path.basename(path)}")
    return info


def parse_probe_output(stdout):
    """ffprobe JSON → metadata dict；沒有 video stream 回 None（route 與節點共用）。"""
    info = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    nb_frames = video.get("nb_frames")
    rotation = next((int(float(d["rotation"])) for d in video.get("side_data_list", []) if "rotation" in d), 0) % 360
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "fps": _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
        "timing": _timing(video),
        "frame_count": int(nb_frames) if str(nb_frames or "").isdigit() else None,
        "source_start_us": _micros(video.get("start_time")),
        "duration_us": _micros(info.get("format", {}).get("duration")),
        # FFmpeg 的 -ss 以檔案起點（format start_time）為 0，時間軸卻錨在第一個 video 幀
        "format_start_us": _micros(info.get("format", {}).get("start_time")),
        "width": width,
        "height": height,
        # FFmpeg 解碼時預設自動轉正，所以實際輸出的畫面尺寸要依旋轉角對調
        "display_size": (height, width) if rotation in (90, 270) else (width, height),
        "rotation_deg": rotation,
        "sar": _rate((video.get("sample_aspect_ratio") or "1:1").replace(":", "/")) or [1, 1],
        "video_codec": video.get("codec_name", ""),
        "has_audio": audio is not None,
        "audio_sample_rate": int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        "audio_channels": int(audio["channels"]) if audio and audio.get("channels") else None,
    }


def describe_source(rel, input_dir=None):
    """compile_plan() 用的來源描述；`path` 只在 Python 內部使用，不會寫進 plan。"""
    path = resolve_timeline_source(rel, input_dir)
    return {"source_ref": rel.replace("\\", "/"), "path": path,
            "fingerprint": opaque_fingerprint(path), **probe_source(path)}
