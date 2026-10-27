"""時間軸編輯器的兩條本機 route：來源 probe、低解析預覽 proxy。

兩條 route 都**沒有認證**（core 的 /prompt 等也沒有）；這裡只保證它們不比 core 更容易被
濫用——它們會啟動 FFmpeg，是資源耗用的入口：
- 跨站請求一律拒絕。使用者以 --enable-cors-header 啟動時，core 用 CORS middleware 取代
  origin-only middleware，不再擋跨站 POST，所以要自己檢查 Origin / Sec-Fetch-Site。
- 只讀 input/ 內的檔案（timeline_source 的路徑規則），FFmpeg 一律走白名單輸入。
- probe / proxy 都有 timeout、並行上限、等待佇列上限；proxy 快取有總量與筆數預算。
handler 寫成不依賴 aiohttp 的純 async 函式，測試可以直接呼叫。
"""
import asyncio
import hashlib
import json
import os
import shutil
import time
from urllib.parse import urlsplit

from .cache_key import path_fingerprint
from .ffmpeg import resolve_ffmpeg_cmd
from .timeline_source import (
    check_relative_name, ffmpeg_input_args, opaque_fingerprint, parse_probe_output, probe_command,
    resolve_timeline_source,
)

MAX_BODY_BYTES = 4096
BODY_READ_TIMEOUT = 5
MAX_SOURCE_BYTES = 8 * 1024 ** 3
MAX_DURATION_US = 1800 * 1_000_000
MAX_SIDE = 8192
MAX_PIXELS = 33_554_432
MAX_FPS = 120
PROBE_TIMEOUT = 10
PROXY_TIMEOUT = 300
PROBE_CONCURRENCY = 2
PROXY_CONCURRENCY = 1
MAX_WAITING = 4
MAX_STDOUT = 64 * 1024
STDERR_TAIL = 64 * 1024
MAX_PROXY_BYTES = 256 * 1024 ** 2
CACHE_BUDGET_BYTES = 4 * 1024 ** 3
CACHE_MAX_ENTRIES = 128
CACHE_TTL_SEC = 24 * 3600
MIN_FREE_BYTES = 1024 ** 3
PRESET = "timeline_v1"
PROXY_SUBFOLDER = "mediaforge_timeline_proxy"


class RouteError(Exception):
    def __init__(self, status, code, message, retryable=False):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable

    def body(self):
        return {"version": 1, "error": {"code": self.code, "message": self.message[:256], "retryable": self.retryable}}


def _endpoint(netloc, scheme):
    parts = urlsplit("//" + netloc)
    return (parts.hostname or "", parts.port or (443 if scheme == "https" else 80))


def check_origin(headers, scheme, host):
    if headers.get("Sec-Fetch-Site") == "cross-site":
        raise RouteError(403, "cross_site", "拒絕跨站請求。")
    origin = headers.get("Origin")
    if origin is None:
        return  # 非瀏覽器的直接請求（與 core origin-only middleware 相同的判準）
    parts = urlsplit(origin)
    if origin == "null" or parts.scheme != scheme or _endpoint(parts.netloc, scheme) != _endpoint(host or "", scheme):
        raise RouteError(403, "cross_site", "拒絕跨站請求。")


def _reject_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(name):
    raise ValueError(name)


def parse_request(raw, kind):
    """closed JSON → input/ 相對路徑與請求內容。"""
    if len(raw) > MAX_BODY_BYTES:
        raise RouteError(413, "too_large", "請求內容過大。")
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise RouteError(400, "bad_request", "請求不是合法的 JSON。") from None
    expected = {"version", "source"} if kind == "probe" else {"version", "source", "fingerprint", "preset"}
    source = data.get("source") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or set(data) != expected or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(source, dict) or set(source) != {"type", "subfolder", "filename"}
            or source["type"] != "input" or not isinstance(source["subfolder"], str)
            or not isinstance(source["filename"], str)):
        raise RouteError(400, "bad_request", "請求欄位不符合格式。")
    if kind == "proxy" and (data["preset"] != PRESET or not isinstance(data["fingerprint"], str)):
        raise RouteError(400, "bad_request", "請求欄位不符合格式。")
    rel = f"{source['subfolder']}/{source['filename']}" if source["subfolder"] else source["filename"]
    try:
        check_relative_name(rel)
    except ValueError:
        raise RouteError(400, "bad_request", "來源必須是 input/ 內的相對路徑。") from None
    return rel, data


def _resolve(rel):
    try:
        path = resolve_timeline_source(rel)
    except (ValueError, FileNotFoundError):
        # 不存在與不可存取一律 404，不透露是哪一種
        raise RouteError(404, "not_found", "找不到來源影片。") from None
    if os.path.getsize(path) > MAX_SOURCE_BYTES:
        raise RouteError(413, "too_large", "來源影片超過 8 GiB 上限。")
    return path


async def _drain(stream, limit, tail):
    data = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(data)
        data += chunk
        if tail:
            del data[:-limit]
        elif len(data) > limit:
            raise RouteError(422, "unprocessable", "來源 metadata 過大。")


async def _discard(stream):
    while await stream.read(65536):
        pass


async def run_process(cmd, timeout, stdout_limit=MAX_STDOUT):
    """async 子程序：stdout 有上限、stderr 只留尾端；逾時、超量或被取消一律 kill + wait。"""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    readers = [asyncio.ensure_future(_drain(proc.stdout, stdout_limit, False)),
               asyncio.ensure_future(_drain(proc.stderr, STDERR_TAIL, True))]
    try:
        out, err = await asyncio.wait_for(asyncio.gather(*readers), timeout)
        await proc.wait()
    except BaseException:
        for r in readers:
            r.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        if proc.returncode is None:
            proc.kill()
        # StreamReader 緩衝滿會暫停讀取；kill 後不把 pipe 讀到 EOF，transport 不會關、wait() 永遠不返回
        await asyncio.gather(_discard(proc.stdout), _discard(proc.stderr), return_exceptions=True)
        await proc.wait()
        raise
    return proc.returncode, out, err


class _Gate:
    """並行上限 + 等待佇列上限；滿了直接 429，不讓請求無限堆積。"""

    def __init__(self, concurrency):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.waiting = 0

    async def __aenter__(self):
        if self.waiting >= MAX_WAITING:
            raise RouteError(429, "busy", "預覽工作忙碌中，請稍後再試。", retryable=True)
        self.waiting += 1
        try:
            await self.semaphore.acquire()
        finally:
            self.waiting -= 1

    async def __aexit__(self, *exc):
        self.semaphore.release()


_state = {"loop": None}


def _runtime():
    # asyncio 物件綁定在 event loop 上；測試每次 asyncio.run 都是新 loop，所以按 loop 重建
    loop = asyncio.get_running_loop()
    if _state["loop"] is not loop:
        _state.update(loop=loop, probe=_Gate(PROBE_CONCURRENCY), proxy=_Gate(PROXY_CONCURRENCY), jobs={}, ffmpeg=None)
    return _state


def _check_limits(info):
    width, height = info["display_size"]
    fps = info["fps"]
    if ((info["duration_us"] or 0) > MAX_DURATION_US or max(width, height) > MAX_SIDE or width * height > MAX_PIXELS
            or (fps and fps[0] > MAX_FPS * fps[1])):
        raise RouteError(413, "too_large", "來源影片超過時長 / 解析度 / 幀率上限。")


async def _probe(path):
    try:
        code, out, _ = await run_process(probe_command(path), PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        raise RouteError(504, "timeout", "讀取來源逾時。", retryable=True) from None
    info = parse_probe_output(out) if code == 0 else None
    if info is None:
        raise RouteError(415, "unsupported_media", "無法讀取這個檔案（格式不在允許清單或檔案損毀）。")
    return info


async def handle_probe(raw, headers, scheme, host):
    check_origin(headers, scheme, host)
    rel, _ = parse_request(raw, "probe")
    path = _resolve(rel)
    async with _runtime()["probe"]:
        info = await _probe(path)
    _check_limits(info)
    width, height = info["display_size"]
    return 200, {
        "version": 1, "fingerprint": opaque_fingerprint(path),
        "source_start_us": info["source_start_us"], "duration_us": info["duration_us"], "fps": info["fps"],
        "frame_count": info["frame_count"], "frame_count_kind": "metadata" if info["frame_count"] else "unknown",
        "timing": info["timing"], "width": width, "height": height, "sar": info["sar"],
        "rotation_deg": info["rotation_deg"], "video_codec": info["video_codec"], "has_audio": info["has_audio"],
    }


def proxy_dir():
    import folder_paths
    path = os.path.join(folder_paths.get_temp_directory(), PROXY_SUBFOLDER)
    os.makedirs(path, exist_ok=True)
    return path


def _evict(directory):
    """LRU：由最久沒用的開始刪，直到有空間再放一個 proxy。在製的 .partial 不在掃描範圍。"""
    now = time.time()
    entries = sorted((e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(directory) if e.name.endswith(".mp4"))
    count, total = len(entries), sum(size for _, size, _ in entries)
    for mtime, size, path in entries:
        if now - mtime <= CACHE_TTL_SEC and count < CACHE_MAX_ENTRIES and total + MAX_PROXY_BYTES <= CACHE_BUDGET_BYTES:
            break
        for p in (path, path[:-4] + ".json"):
            try:
                os.unlink(p)
            except OSError:
                pass
        count, total = count - 1, total - size


async def _ffmpeg_version(runtime):
    if runtime["ffmpeg"] is None:
        _, out, _ = await run_process(resolve_ffmpeg_cmd(["ffmpeg", "-version"]), PROBE_TIMEOUT)
        runtime["ffmpeg"] = out.decode("utf-8", errors="replace").splitlines()[0] if out else "unknown"
    return runtime["ffmpeg"]


def proxy_command(source, target):
    return resolve_ffmpeg_cmd(
        ["ffmpeg", "-nostdin", "-v", "error", "-y"] + ffmpeg_input_args(source)
        + ["-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1",
           "-vf", "setpts=PTS-STARTPTS,scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1,"
                  "scale=w='min(960,iw)':h='min(540,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
           "-fpsmax", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
           "-bf", "0", "-force_key_frames", "expr:gte(t,n_forced*0.5)", "-movflags", "+faststart",
           "-fs", str(MAX_PROXY_BYTES), "-f", "mp4", target]
    )


async def _build_proxy(runtime, source, target):
    async with runtime["proxy"]:
        directory = os.path.dirname(target)
        _evict(directory)
        if shutil.disk_usage(directory).free < MIN_FREE_BYTES + MAX_PROXY_BYTES:
            raise RouteError(507, "insufficient_storage", "暫存空間不足，無法建立預覽。")
        partial = target + ".partial"
        try:
            try:
                code, _, _ = await run_process(proxy_command(source, partial), PROXY_TIMEOUT, stdout_limit=0)
            except asyncio.TimeoutError:
                raise RouteError(504, "timeout", "建立預覽逾時。", retryable=True) from None
            if code != 0 or not os.path.exists(partial):
                raise RouteError(415, "unsupported_media", "無法為這個檔案建立預覽。")
            if os.path.getsize(partial) >= MAX_PROXY_BYTES:
                raise RouteError(413, "too_large", "預覽檔超過大小上限。")
            info = await _probe(partial)
            os.replace(partial, target)
        finally:
            if os.path.exists(partial):
                os.unlink(partial)
    with open(target[:-4] + ".json", "w", encoding="utf-8") as f:
        json.dump({"preset": PRESET}, f)
    return info


async def handle_proxy(raw, headers, scheme, host):
    check_origin(headers, scheme, host)
    rel, data = parse_request(raw, "proxy")
    path = _resolve(rel)
    if opaque_fingerprint(path) != data["fingerprint"]:
        raise RouteError(409, "fingerprint_mismatch", "來源影片已變更，請重新讀取。", retryable=True)
    runtime = _runtime()
    key = hashlib.sha256(f"{path_fingerprint(path)}|{PRESET}|{await _ffmpeg_version(runtime)}".encode("utf-8")).hexdigest()
    target = os.path.join(proxy_dir(), key + ".mp4")
    cache_hit = os.path.exists(target) and os.path.exists(target[:-4] + ".json")
    if cache_hit:
        os.utime(target)
        info = await _probe(target)
    else:
        # 同一個 key 只跑一個 FFmpeg；最後一個等待者離開才取消
        job = runtime["jobs"].get(key)
        if job is None:
            job = {"task": asyncio.ensure_future(_build_proxy(runtime, path, target)), "waiters": 0}
            runtime["jobs"][key] = job
            job["task"].add_done_callback(lambda _: runtime["jobs"].pop(key, None))
        job["waiters"] += 1
        try:
            info = await asyncio.shield(job["task"])
        finally:
            job["waiters"] -= 1
            if job["waiters"] == 0 and not job["task"].done():
                job["task"].cancel()
    width, height = info["display_size"]
    return 200, {
        "version": 1, "fingerprint": data["fingerprint"],
        "view": {"type": "temp", "subfolder": PROXY_SUBFOLDER, "filename": key + ".mp4"},
        "width": width, "height": height, "fps": info["fps"], "duration_us": info["duration_us"],
        "source_start_us": info["source_start_us"], "muted": True, "cache_hit": cache_hit,
    }


async def _read_body(request):
    body = bytearray()
    while len(body) <= MAX_BODY_BYTES:
        chunk = await request.content.read(MAX_BODY_BYTES + 1 - len(body))
        if not chunk:
            break
        body += chunk
    return bytes(body)


async def respond(request, handler):
    from aiohttp import web
    try:
        if request.headers.get("Content-Encoding"):
            raise RouteError(415, "unsupported_media", "不接受壓縮的請求內容。")
        try:
            raw = await asyncio.wait_for(_read_body(request), BODY_READ_TIMEOUT)
        except asyncio.TimeoutError:
            raise RouteError(400, "bad_request", "讀取請求逾時。") from None
        status, body = await handler(raw, request.headers, request.scheme, request.host)
    except RouteError as e:
        status, body = e.status, e.body()
    except Exception as e:  # noqa: BLE001 — 細節只留在 server log
        print(f"[MediaForge] timeline route 失敗：{type(e).__name__}: {e}")
        status, body = 503, RouteError(503, "unavailable", "服務暫時無法處理。", retryable=True).body()
    return web.json_response(body, status=status, headers={"Cache-Control": "no-store"})


def register_timeline_routes():
    """可重複呼叫；防重複旗標綁在實際的 PromptServer instance 上。"""
    from server import PromptServer
    instance = PromptServer.instance
    if getattr(instance, "_mediaforge_timeline_routes", False):
        return
    routes = instance.routes

    @routes.post("/mediaforge/timeline/probe")
    async def _probe_route(request):
        return await respond(request, handle_probe)

    @routes.post("/mediaforge/timeline/proxy")
    async def _proxy_route(request):
        return await respond(request, handle_proxy)

    instance._mediaforge_timeline_routes = True
