import json
import os
import shutil
import subprocess


def escape_filter_path(path):
    # FFmpeg filter graph 兩層 escape：path 內的 colon 必須兩層都做。
    # Level 1 (filter args): colon 是 option separator → escape 成 `\` + `:`
    # Level 2 (filter description): backslash 再 escape 成 `\\`
    # 合併後每個 colon 在 filter description 字串中是 `\\:` (3 chars)。
    # FFmpeg 4.x/5.x parser lenient、單層也接受；FFmpeg 8.x stricter，必爆。
    # Windows path 的 `\` 先正規化成 `/`（filter graph 慣例），剩下純粹處理 colon。
    return path.replace('\\', '/').replace(':', r'\\:')


# ─── Binary resolution: system → imageio-ffmpeg → static-ffmpeg ───
#
# 為什麼分層 fallback：
#   1. system ffmpeg/ffprobe 永遠是最快、最相容、最多 codec 支援的選項
#   2. imageio-ffmpeg 只帶 ffmpeg (沒 ffprobe)、輕量、是 pip 主流方案
#   3. static-ffmpeg 兩個都帶 (lazily downloads on first add_paths())、是 ffprobe 的後備
# 解析結果 cache 在 process lifetime — 第二次 call 不會重複 shutil.which / 重新 init。


_FFMPEG_PATH_CACHE = None   # None=未查 / ""=查過找不到 / str=resolved path
_FFPROBE_PATH_CACHE = None
_STATIC_FFMPEG_INITIALIZED = False


def _try_init_static_ffmpeg():
    """Lazy init static-ffmpeg；把它 bundled 的 ffmpeg/ffprobe 加到 PATH。

    首次呼叫可能會觸發下載 (~30-60MB)，之後 cache 在 ~/.cache/static_ffmpeg/。
    """
    global _STATIC_FFMPEG_INITIALIZED
    if _STATIC_FFMPEG_INITIALIZED:
        return
    _STATIC_FFMPEG_INITIALIZED = True
    try:
        from static_ffmpeg import add_paths
        add_paths()
    except ImportError:
        pass
    except Exception as e:
        # 不爆，讓 caller 退階到下一個 fallback
        print(f"[MediaForge] static-ffmpeg 初始化失敗（會嘗試其他 fallback）：{e}")


def get_ffmpeg_bin():
    """Resolve ffmpeg binary path. 優先 system → imageio-ffmpeg → static-ffmpeg。

    Returns 絕對路徑 str，或 None 表示三個來源都拿不到。Cached after first call.
    """
    global _FFMPEG_PATH_CACHE
    if _FFMPEG_PATH_CACHE is not None:
        return _FFMPEG_PATH_CACHE or None

    # 1. system ffmpeg
    p = shutil.which("ffmpeg")
    if p:
        _FFMPEG_PATH_CACHE = p
        return p

    # 2. imageio-ffmpeg bundled binary (只給 ffmpeg、沒 ffprobe)
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            _FFMPEG_PATH_CACHE = p
            return p
    except ImportError:
        pass
    except Exception as e:
        print(f"[MediaForge] imageio-ffmpeg 解析失敗（會嘗試 static-ffmpeg）：{e}")

    # 3. static-ffmpeg (add_paths 之後 shutil.which 才找得到)
    _try_init_static_ffmpeg()
    p = shutil.which("ffmpeg")
    if p:
        _FFMPEG_PATH_CACHE = p
        return p

    _FFMPEG_PATH_CACHE = ""
    return None


def get_ffprobe_bin():
    """Resolve ffprobe binary path. 優先 system → static-ffmpeg。

    imageio-ffmpeg 不帶 ffprobe，所以只剩 static-ffmpeg 是 bundled fallback。
    Returns 絕對路徑 str 或 None。Cached.
    """
    global _FFPROBE_PATH_CACHE
    if _FFPROBE_PATH_CACHE is not None:
        return _FFPROBE_PATH_CACHE or None

    # 1. system ffprobe
    p = shutil.which("ffprobe")
    if p:
        _FFPROBE_PATH_CACHE = p
        return p

    # 2. static-ffmpeg (PATH-injecting)
    _try_init_static_ffmpeg()
    p = shutil.which("ffprobe")
    if p:
        _FFPROBE_PATH_CACHE = p
        return p

    _FFPROBE_PATH_CACHE = ""
    return None


def resolve_ffmpeg_cmd(cmd):
    """把 cmd[0] (若為 'ffmpeg' / 'ffprobe' 字串) 替換成 resolved binary 絕對路徑。

    Caller 應該先 ensure_ffmpeg() 確認可用、再丟 cmd 進來。若 resolve 失敗（理論上
    ensure_ffmpeg 應該已經 raise），這裡退階到原字串，subprocess 自己會報 FileNotFoundError。

    回傳新 list（不 mutate caller 的 cmd）。
    """
    if not cmd:
        return cmd
    if cmd[0] == "ffmpeg":
        return [get_ffmpeg_bin() or "ffmpeg", *cmd[1:]]
    if cmd[0] == "ffprobe":
        return [get_ffprobe_bin() or "ffprobe", *cmd[1:]]
    return cmd


def ensure_ffmpeg():
    """確認 ffmpeg + ffprobe 都拿得到（任一 backend）。失敗時印導引訊息。"""
    ffmpeg_ok = bool(get_ffmpeg_bin())
    ffprobe_ok = bool(get_ffprobe_bin())
    if not ffmpeg_ok:
        print(
            "[MediaForge] 錯誤：找不到 ffmpeg。"
            "建議：(1) 系統裝 FFmpeg 加進 PATH，或 (2) `pip install imageio-ffmpeg static-ffmpeg`（已列在 requirements.txt）。"
        )
    if not ffprobe_ok:
        print(
            "[MediaForge] 錯誤：找不到 ffprobe。"
            "imageio-ffmpeg 不帶 ffprobe，建議：(1) 系統裝 FFmpeg，或 (2) `pip install static-ffmpeg`（已列在 requirements.txt）。"
        )
    return ffmpeg_ok and ffprobe_ok


def run_ffmpeg(command, tag="FFmpeg"):
    """Run an ffmpeg/ffprobe command, capturing stderr so failures show the actual FFmpeg error."""
    command = resolve_ffmpeg_cmd(command)
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
    cmd = resolve_ffmpeg_cmd([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ])
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


def probe_has_audio_stream(path):
    """Return True iff ffprobe 顯示 path 有 audio stream。

    用途:burn_subtitle / loop_video / trim_by_ranges / compose audio chain compile
    都需要決定「source 有沒有可混音的音軌」。集中在這裡避免邏輯重複。
    Probe 失敗 (檔案不存在 / 不是媒體檔) 視為「沒音軌」、不 raise。
    """
    info = probe(path)
    if not info:
        return False
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


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
