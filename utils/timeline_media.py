"""Timeline 素材解碼：來源影片區段、素材切片、圖片（LoadSegmentFrames / LoadSegmentMedia / EncodeH3Segment 共用）。

FFmpeg 一律走 timeline_source 的安全輸入旗標。時間軸幀 f ↔ 素材時間 f·den/num 秒（相對第一個
video 幀）；先 accurate seek 再以 fps=num/den 重取樣——同 fps 的來源逐幀精確，不同 fps 則是決定
性的最近幀取樣。
"""
import subprocess
from fractions import Fraction

import numpy as np
import torch

from .ffmpeg import resolve_ffmpeg_cmd
from .timeline_source import ffmpeg_input_args

AUDIO_PREROLL = Fraction(1)  # 秒；遠大於常見音訊封包（AAC ≈ 21 ms、PCM-in-MKV ≈ 85 ms）


def scale_filter(resize, width, height):
    if resize == "fit":
        return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if resize == "fill":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
    if resize == "stretch":
        return f"scale={width}:{height},setsar=1"
    return None


def run_capture(cmd, label, what):
    proc = subprocess.run(resolve_ffmpeg_cmd(cmd), capture_output=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-30:])
        raise RuntimeError(f"[{label}] FFmpeg {what}失敗 (exit {proc.returncode})：\n{tail}")
    return proc.stdout


def decode_video(path, seek, count, fps, size, scale, label):
    num, den = fps
    vf = f"fps={num}/{den}" + (f",{scale}" if scale else "")
    # -map 與 probe 一樣取第一條 video；不指定時 FFmpeg 可能選到 default 的其他串流
    out = run_capture(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{float(seek):.9f}"] + ffmpeg_input_args(path)
                      + ["-map", "0:v:0", "-vf", vf, "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                      label, "解碼畫面")
    w, h = size
    n = len(out) // (w * h * 3)
    arr = np.frombuffer(out[: n * w * h * 3], dtype=np.uint8).reshape(n, h, w, 3)
    return torch.from_numpy(arr.copy()).float() / 255.0


def decode_audio(path, seek, video_start, count, sample_rate, channels, label):
    # 輸入端 -ss 跳到「不晚於目標的 video 關鍵幀」所在的 cluster：音訊可能晚一個封包才開始，第一個
    # video 幀之前的音訊則整段讀不到；raw PCM 又沒有時間戳。所以往前多解一段——pre-roll 會落到第一個
    # video 幀之前時就從檔頭讀——aresample 依 PTS 補齊空白（音軌晚於起點時也靠它），再以 sample 數精確裁掉前段
    pre = seek if seek - AUDIO_PREROLL < video_start else AUDIO_PREROLL
    af = (f"aresample=async=1:first_pts=0,atrim=start_sample={round(pre * sample_rate)},asetpts=PTS-STARTPTS,"
          f"apad=whole_len={count},atrim=end_sample={count}")
    head = ["-ss", f"{float(seek - pre):.9f}"] if seek > pre else []
    out = run_capture(["ffmpeg", "-v", "error", "-nostdin"] + head + ffmpeg_input_args(path)
                      + ["-map", "0:a:0", "-af", af, "-ac", str(channels), "-ar", str(sample_rate), "-f", "f32le", "pipe:1"],
                      label, "解碼音訊")
    pcm = np.frombuffer(out, dtype=np.float32).reshape(-1, channels).T
    return torch.from_numpy(pcm.copy()).unsqueeze(0)
