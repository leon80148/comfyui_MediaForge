import collections
import json
import os
import shutil
import subprocess


# FFmpeg filter graph 官方兩層 escape 規則（ffmpeg-filters.html "Notes on
# filtergraph escaping"）：
#   Level 1（單一 filter option value 內）：`:` `'` `\` 需要 escape。
#   Level 2（整個 filtergraph 敘述）：`\` `'` `[` `]` `,` `;` 需要 escape。
# 兩層依序疊加，level 1 產生的反斜線會被 level 2 的規則再跳脫一次 —— colon 因此變成
# `\\:`（2 個反斜線 + colon），這是既有行為、必須維持相容（FFmpeg 4.x/5.x parser
# lenient、單層也接受；8.x stricter，必爆）。
_LEVEL1_SPECIALS = frozenset({':', "'", '\\'})
_LEVEL2_SPECIALS = frozenset({'\\', "'", '[', ']', ',', ';'})


def _escape_chars(s, specials):
    # 逐字元掃過『原始』輸入字串一次、屬於 specials 集合的字元前面補一個反斜線。
    # 只掃原字串（不重掃剛產生的反斜線），兩層疊加時才不會互相干擾出非預期的跳脫次數。
    out = []
    for ch in s:
        if ch in specials:
            out.append('\\')
        out.append(ch)
    return ''.join(out)


def escape_filter_path(path):
    # Windows path 的 `\` 先正規化成 `/`（filter graph 路徑慣例），做完後字串內沒有
    # 反斜線，才不會被下面兩層 escape 誤判成使用者原本就寫的 escape char。
    normalized = path.replace('\\', '/')
    level1 = _escape_chars(normalized, _LEVEL1_SPECIALS)
    return _escape_chars(level1, _LEVEL2_SPECIALS)


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


# ─── probe() 進程內快取（W2-1）───
#
# 為什麼：probe_duration / probe_video_duration / probe_has_audio_stream / probe_video_fps
# 都各自呼叫 probe()，單一節點執行常對同一檔案重複 probe 2-4 次（ConcatVideos transcode
# 對 N 檔 2N 次、ComposeVideo 2 次、LoopVideo 3-4 次）。ffprobe spawn 在 WSL/UNC 路徑上
# 有可感知延遲，快取掉重複呼叫直接省下對應次數的 subprocess spawn。
#
# Key 用 (abspath, mtime_ns, size) 而非單純 path：同路徑覆寫內容要視為 cache miss，
# 語意對齊 W1-13 IS_CHANGED 用的 mtime fingerprint（utils/cache_key.py）。stat 失敗
# （檔案不存在等）不快取，讓 subprocess 照舊走、給原生錯誤路徑。
PROBE_CACHE_MAXSIZE = 64
_PROBE_CACHE = collections.OrderedDict()


def clear_probe_cache():
    """清空 probe() 的進程內快取。

    Runtime 不需要呼叫——cache key 已含 mtime/size，同路徑換內容會自動 miss。
    測試用：tests/conftest.py 的 autouse fixture 每個 test 開始前呼叫，避免固定
    tmp 檔名重用（同 path/mtime/size 剛好重合）造成的跨 test 汙染。
    """
    _PROBE_CACHE.clear()


def probe(path):
    cache_key = None
    try:
        st = os.stat(path)
        cache_key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        pass  # 檔案不存在等 stat 失敗情況——不快取

    if cache_key is not None and cache_key in _PROBE_CACHE:
        _PROBE_CACHE.move_to_end(cache_key)
        return _PROBE_CACHE[cache_key]

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
        info = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"[ffprobe] 失敗: {e}")
        info = None

    if cache_key is not None:
        _PROBE_CACHE[cache_key] = info
        _PROBE_CACHE.move_to_end(cache_key)
        if len(_PROBE_CACHE) > PROBE_CACHE_MAXSIZE:
            _PROBE_CACHE.popitem(last=False)

    return info


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


def probe_video_fps(path):
    """Video stream fps：優先 avg_frame_rate，值為 0（variable frame rate 常見）才退
    r_frame_rate。找不到可用值回傳 None。

    用途：LoopVideo 估算 loop filter 需緩衝的 frame 數是否超過 MAX_LOOP_FRAMES（W1-4）。
    走 probe() 而非自建 subprocess，日後 W2-1 probe cache 生效時這裡自動吃到。
    """
    info = probe(path)
    if info is None:
        return None
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        return None
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate_str = v.get(key) or "0/0"
        try:
            num, den = rate_str.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 0.0
            if fps > 0:
                return fps
        except (ValueError, AttributeError):
            continue
    return None


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
