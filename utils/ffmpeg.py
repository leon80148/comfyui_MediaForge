import json
import shutil
import subprocess


def escape_filter_path(path):
    # FFmpeg's filter graph uses ':' to separate options, so Windows drive letters
    # (C:\...) must be escaped or the path is mis-parsed as filter options.
    return path.replace('\\', '/').replace(':', r'\:')


def ensure_ffmpeg():
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        print(f"[Media Template] 錯誤：找不到 {', '.join(missing)}。請安裝 FFmpeg 並加到系統 PATH。")
        return False
    return True


def run_ffmpeg(command, tag="FFmpeg"):
    """Run an ffmpeg/ffprobe command, capturing stderr so failures show the actual FFmpeg error."""
    print(f"[{tag}] 執行: {' '.join(command)}")
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        return True
    except subprocess.CalledProcessError as e:
        tail = "\n".join((e.stderr or "").strip().splitlines()[-30:])
        print(f"[{tag}] 執行失敗 (exit {e.returncode}):\n{tail}")
        return False


def probe(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"[ffprobe] 失敗: {e}")
        return None


def probe_duration(path):
    """Container 整體時長 (含最長 stream)。對影片 timeline 計算不準 — 用 probe_video_duration。"""
    info = probe(path)
    if info is None:
        return None
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        return None


def get_video_display_dims(stream):
    """從 ffprobe 的 video stream dict 算出「顯示尺寸」(post auto-rotate)。

    portrait phone 影片：coded width/height 是未 rotate 的；FFmpeg 預設輸出時會 auto-rotate。
    rotation 在 90/270° 時 width 與 height 對調。集中放這裡讓 LoadVideoFrames / ProbeMedia
    等 caller 共用，避免 R9/R10 那種 fix 沒擴散到所有 callers 的情境再發生。
    """
    coded_w = int(stream.get("width", 0))
    coded_h = int(stream.get("height", 0))
    rotation = 0
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rotation = int(sd["rotation"])
            except (ValueError, TypeError):
                pass
            break
    if rotation == 0:
        try:
            rotation = int(stream.get("tags", {}).get("rotate", 0))
        except (ValueError, TypeError):
            pass
    rotation = abs(rotation) % 360
    if rotation in (90, 270):
        return coded_h, coded_w
    return coded_w, coded_h


def probe_video_duration(path):
    """Video stream 自己的時長。

    為什麼分開做：container duration 是所有 stream 的 max；若 audio 比 video 長 (常見於
    capture / 編輯軟體輸出)，用 container duration 算 loop 次數 / trim 互補 / Compose -t
    會多出尾部凍結幀。Codex R5 P2 finding。
    """
    info = probe(path)
    if info is None:
        return None
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        return None
    # 優先用 video stream 的 duration；某些 codec / container 不直接給 → 退階用 nb_frames/fps
    try:
        d = v.get("duration")
        if d is not None:
            return float(d)
    except (ValueError, TypeError):
        pass
    try:
        nb_frames = int(v.get("nb_frames") or 0)
        rate_str = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
        num, den = rate_str.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0.0
        if nb_frames > 0 and fps > 0:
            return nb_frames / fps
    except (ValueError, ZeroDivisionError, AttributeError):
        pass
    # 最後退階：container duration
    return probe_duration(path)
