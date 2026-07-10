import math
import os

from ..utils.cache_key import path_fingerprint
from ..utils.encoder import build_encoder_args, get_available_codecs, pick_default_codec
from ..utils.ffmpeg import (
    ensure_ffmpeg,
    probe_has_audio_stream,
    probe_video_duration,
    probe_video_fps,
    probe_video_frame_count,
    run_ffmpeg,
)
from ..utils.output_path import output_path_to_ui_entry, resolve_output_path
from ..utils.video_io import encode_tensor_to_tempfile, mux_path_with_audio_dict


# loop filter `size` 是「buffered frames 上限」(FFmpeg INT16_MAX 為 32767)；
# 32767 frames 在 30fps 下 ≈ 18 分鐘的素材，實務上夠用。
MAX_LOOP_FRAMES = 32767
MAX_LOOP_SAMPLES = 1_000_000_000  # aloop 上限：~10 小時的 48kHz 音訊
MAX_CROSSFADE_LOOPS = 50  # chained xfade 上限，避免 filter graph 太大


def _atempo_chain(speed):
    # 單一 atempo 上下限是 [0.5, 2.0]；超出範圍要 chain。例：4.0 -> atempo=2,atempo=2
    if abs(speed - 1.0) < 1e-9:
        return []
    parts = []
    r = speed
    while r > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        r /= 2.0
    while r < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        r *= 2.0
    if abs(r - 1.0) > 1e-9:
        parts.append(f"atempo={r:.6f}")
    return parts


def _loop_repeat_count(mode, eff_dur, target):
    # C4 fix：strict / ping_pong 的 loop filter 重複次數 n，跟 `_build_filter`
    # 用同一個算式（呼叫端與 filter 組裝端不能各算一次、算法漂移會讓 guard 跟實際
    # filter graph 對不上）。n=1 代表「來源本身已達到/超過 target，不需要真的
    # loop」——loop=n-1=0 時 loop filter 是 pure pass-through、不會把整段素材
    # buffer 進 `size` frames 上限；只有 n>1（filter 真的要 loop）才需要檢查
    # MAX_LOOP_FRAMES guard。
    if mode == "strict":
        return max(1, math.ceil(target / eff_dur))
    if mode == "ping_pong":
        return max(1, math.ceil(target / (eff_dur * 2)))
    raise ValueError(
        f"[Loop Video] _loop_repeat_count 收到未支援的 mode={mode!r}"
        "（內部錯誤，理論上呼叫端已檢查過 loop_mode，請回報此 bug）"
    )


class MF_LoopVideo:
    @classmethod
    def INPUT_TYPES(s):
        # 與 SaveVideoFrames / BurnSubtitle / ComposeFinalize 共用 encoder catalog；
        # NVENC variants 視 ffmpeg capability 動態加入。
        codec_choices = list(get_available_codecs().keys())
        return {
            "required": {
                "video_path": ("STRING", {"default": "input/sample.mp4"}),
                "filename_prefix": ("STRING", {"default": "MediaForge/looped"}),
                "target_duration_sec": ("FLOAT", {"default": 30.0, "min": 0.1, "max": 36000.0, "step": 0.1}),
                "loop_mode": (["strict", "ping_pong", "crossfade"], {"default": "strict"}),
                "crossfade_sec": ("FLOAT", {"default": 1.0, "min": 0.1, "max": 10.0, "step": 0.1}),
                "speed": ("FLOAT", {"default": 1.0, "min": 0.25, "max": 4.0, "step": 0.05}),
                "reverse": ("BOOLEAN", {"default": False}),
                "keep_audio": ("BOOLEAN", {"default": True}),
                # 音量倍率 — 1.0 = 原音；0.0 = 靜音；0.5 = 半音量。只在 keep_audio=True 時生效
                "audio_volume": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05}),
                # P1-4：codec / crf / preset 與 BurnSubtitle 對齊；NVENC 自動進 dropdown
                "codec": (codec_choices, {"default": pick_default_codec()}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 51}),
                "preset": (
                    ["ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow"],
                    {"default": "medium"},
                ),
            },
            "optional": {
                # In-memory chain: 連 frames 後改走 tensor → temp mp4 → loop 流程
                "frames": ("IMAGE",),
                "tensor_fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 240.0, "step": 0.1}),
                "audio": ("AUDIO",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("final_video_path",)
    FUNCTION = "loop"
    CATEGORY = "MediaForge/Video"

    def loop(self, video_path, filename_prefix, target_duration_sec, loop_mode,
             crossfade_sec, speed, reverse, keep_audio, audio_volume,
             codec, crf, preset,
             frames=None, tensor_fps=30.0, audio=None):

        if not ensure_ffmpeg():
            raise RuntimeError("[Loop Video] FFmpeg / FFprobe 未在 PATH 中，請先安裝。")

        # P1-4：跟 BurnSubtitle / SaveVideoFrames / ComposeFinalize 共用 encoder builder
        codec_map = get_available_codecs()
        codec_id, default_pix_fmt = codec_map.get(codec, codec_map["h264 (libx264)"])
        # 解析輸出路徑 — auto-counter，避免覆蓋；ProRes 需要 .mov 容器才能 mux 成功 (W1-6)
        ext = ".mov" if codec_id == "prores_ks" else ".mp4"
        output_path = resolve_output_path(filename_prefix, ext)

        cleanup_tmp = None
        try:
            # Dual-input dispatch（與 BurnSubtitle 的 source resolve 對稱）：
            # 1. frames 接了 → encode_tensor_to_tempfile 已把 audio 一併 mux 進 temp.mp4
            # 2. path mode + audio dict 接了 → pre-mux 兩者 (修 dangling-input bug)
            # 3. 純 path mode → 沿用 source 自帶 audio (或無 audio)
            if frames is not None:
                source_path = encode_tensor_to_tempfile(frames, fps=tensor_fps, audio=audio)
                cleanup_tmp = source_path
            else:
                if not os.path.exists(video_path):
                    raise FileNotFoundError(f"[Loop Video] 找不到影片：{video_path}")
                if audio is not None:
                    source_path = mux_path_with_audio_dict(video_path, audio)
                    cleanup_tmp = source_path
                else:
                    source_path = video_path

            source_dur = probe_video_duration(source_path)
            if source_dur is None or source_dur <= 0:
                raise RuntimeError(
                    f"[Loop Video] 無法讀取影片長度：{source_path}。"
                    "請先用 MF_ProbeMedia 驗證檔案可解析，或檢查容器/編碼是否被 ffprobe 支援"
                )

            effective_dur = source_dur / speed
            has_audio = keep_audio and probe_has_audio_stream(source_path)

            if loop_mode == "crossfade" and crossfade_sec >= effective_dur:
                print(f"[Loop Video] 警告：crossfade_sec({crossfade_sec}) >= 有效片段長度({effective_dur:.2f}s)，改用 strict")
                loop_mode = "strict"
            # P0-2 fix：target <= xfade 邊界。原公式 ceil((target-xfade)/(eff-xfade)) 變 ≤0，
            # max(2, ...) 強塞 n=2 → 兩段完全重疊輸出時長 = eff_dur，不到 target、silent 偏短。
            # 退階到 strict：strict 用 -t 截長度、行為可預測。
            if loop_mode == "crossfade" and target_duration_sec <= crossfade_sec:
                print(
                    f"[Loop Video] 警告：target_duration({target_duration_sec})s ≤ crossfade_sec({crossfade_sec})s，"
                    "退階到 strict（無交疊接縫）"
                )
                loop_mode = "strict"

            if loop_mode in ("strict", "ping_pong"):
                # C4 fix：n=1（來源本身已達到/超過 target）時 loop=n-1=0，loop
                # filter 是 pure pass-through、不 buffer 整段素材，不受 `size`
                # 上限影響——20 分鐘來源 + target 30s 這種案例在舊碼本來就能正常
                # 輸出，guard 只該在「真的需要 loop」（n>1）時把關。n 的算法跟
                # `_build_filter` 共用 `_loop_repeat_count()`，避免兩處算式漂移。
                n = _loop_repeat_count(loop_mode, effective_dur, target_duration_sec)
                if n > 1:
                    # loop filter 的 size 是「單一 unit」緩衝上限（INT16_MAX），不是總輸出
                    # 長度；超過就只會 buffer 前 32767 frames、其餘 silently 消失 — 必須在
                    # 送進 ffmpeg 前擋下（CLAUDE.md gotcha #3）。crossfade 走 xfade、不受此限制。
                    #
                    # F2 fix：`setpts` 只改時間戳、不改幀數 —— 進 loop filter 的幀數是「來源
                    # 實際幀數」，與 speed 無關（舊公式用 effective_dur(=source_dur/speed) ×
                    # fps 估算：speed=4 時低估 4 倍、guard 誤放行；speed=0.25 時高估、誤擋
                    # 合法輸入）。reverse 同理不影響幀數。優先用 ffprobe 原生 nb_frames
                    # （精確值），沒有才退階用 fps × 來源 duration 估算。
                    source_frames = probe_video_frame_count(source_path)
                    if source_frames is None:
                        src_fps = probe_video_fps(source_path)
                        source_frames = source_dur * src_fps if src_fps else None
                    if source_frames is not None:
                        # ping_pong 是 concat(正+反) 後再 loop，單位幀數 = 來源幀數 ×2
                        unit_frames = source_frames * 2 if loop_mode == "ping_pong" else source_frames
                        if unit_frames > MAX_LOOP_FRAMES:
                            raise ValueError(
                                f"[Loop Video] loop_mode={loop_mode} 需要緩衝約 {unit_frames:.0f} frames"
                                f"（來源 {source_frames:.0f} frames"
                                f"{' ×2(ping_pong 正反合併)' if loop_mode == 'ping_pong' else ''}），"
                                f"超過 FFmpeg loop filter 上限 {MAX_LOOP_FRAMES}，只會 loop 到前段內容、"
                                "輸出會是錯誤結果。請改用 loop_mode=crossfade，或先降低來源 fps / 縮短素材長度。"
                            )

            filter_parts, v_out, a_out = self._build_filter(
                loop_mode, effective_dur, target_duration_sec, crossfade_sec,
                speed, reverse, has_audio, audio_volume,
            )

            cmd = ["ffmpeg", "-y", "-i", source_path,
                   "-filter_complex", ";".join(filter_parts),
                   "-map", f"[{v_out}]"]
            if has_audio:
                cmd.extend(["-map", f"[{a_out}]"])
            else:
                cmd.append("-an")
            # codec_id / default_pix_fmt 已在輸出路徑解析時算過 (W1-6)，這裡直接沿用
            cmd.extend(build_encoder_args(codec_id, crf=crf, preset=preset))
            cmd.extend(["-pix_fmt", default_pix_fmt])
            cmd.extend(["-t", f"{target_duration_sec}", output_path])

            if not run_ffmpeg(cmd, tag="Loop Video"):
                raise RuntimeError("[Loop Video] FFmpeg loop 失敗，請查看上方 stderr 輸出。")
        finally:
            if cleanup_tmp:
                try:
                    os.unlink(cleanup_tmp)
                except OSError:
                    pass

        print(f"[Loop Video] 輸出成功: {output_path}")
        return {"ui": {"images": [output_path_to_ui_entry(output_path)]}, "result": (output_path,)}

    @staticmethod
    def _build_filter(mode, eff_dur, target, xfade_dur, speed, reverse, has_audio, audio_volume):
        # Defensive：crossfade 模式但 target <= xfade，靜默退階到 strict（避免兩段完全
        # overlap 輸出比 target 短）。caller (.loop) 已先檢查、這裡是保險。
        if mode == "crossfade" and target <= xfade_dur:
            mode = "strict"
        # Step 1: 前處理（音量、speed、reverse）套用到來源串流
        v_pre = []
        a_pre = []
        # volume 放在 a_pre 最前面：FFmpeg audio filter 多數 commutative，順序不影響結果，
        # 但概念上「先調量、再做其他變換」最清楚。0.0-1.0 範圍；1.0 跳過避免無謂的 filter graph 節點
        if has_audio and abs(audio_volume - 1.0) > 1e-9:
            a_pre.append(f"volume={audio_volume:.3f}")
        if reverse:
            v_pre.append("reverse")
            a_pre.append("areverse")
        if abs(speed - 1.0) > 1e-9:
            # setpts=PTS/speed 把每幀的播放時間縮放；speed>1 變快、<1 變慢
            v_pre.append(f"setpts={1.0/speed:.6f}*PTS")
            a_pre.extend(_atempo_chain(speed))
        v_pre_str = ",".join(v_pre) if v_pre else "null"
        a_pre_str = ",".join(a_pre) if a_pre else "anull"

        parts = [f"[0:v]{v_pre_str}[base_v]"]
        if has_audio:
            # R7 P2 fix：把 audio 截/補到 video 的 effective duration (eff_dur)，
            # 否則來源 video/audio 長度不同 (常見於編輯軟體輸出或合成素材) 會讓
            # aloop 與 video loop 的「單位長度」不一致，最終輸出音訊長度偏差。
            # apad 加靜音、atrim 截斷至 eff_dur，雙保險。
            parts.append(
                f"[0:a]{a_pre_str},"
                f"apad=whole_dur={eff_dur:.6f},"
                f"atrim=duration={eff_dur:.6f},asetpts=PTS-STARTPTS[base_a]"
            )

        # Step 2: 依 loop_mode 組接
        if mode == "strict":
            n = _loop_repeat_count(mode, eff_dur, target)
            parts.append(f"[base_v]loop=loop={n - 1}:size={MAX_LOOP_FRAMES}:start=0[looped_v]")
            if has_audio:
                parts.append(f"[base_a]aloop=loop={n - 1}:size={MAX_LOOP_SAMPLES}:start=0[looped_a]")

        elif mode == "ping_pong":
            n = _loop_repeat_count(mode, eff_dur, target)
            parts.append("[base_v]split=2[bv1][bv2]")
            parts.append("[bv2]reverse[bv2r]")
            parts.append("[bv1][bv2r]concat=n=2:v=1:a=0[pp_v]")
            parts.append(f"[pp_v]loop=loop={n - 1}:size={MAX_LOOP_FRAMES}:start=0[looped_v]")
            if has_audio:
                parts.append("[base_a]asplit=2[ba1][ba2]")
                parts.append("[ba2]areverse[ba2r]")
                parts.append("[ba1][ba2r]concat=n=2:v=0:a=1[pp_a]")
                parts.append(f"[pp_a]aloop=loop={n - 1}:size={MAX_LOOP_SAMPLES}:start=0[looped_a]")

        elif mode == "crossfade":
            # N 個 loop 用 chained xfade。每多一個 loop 增加 (eff_dur - xfade_dur) 長度。
            n = max(2, math.ceil((target - xfade_dur) / (eff_dur - xfade_dur)))
            if n > MAX_CROSSFADE_LOOPS:
                print(f"[Loop Video] 警告：crossfade 需要 {n} 個 loop，已截到 {MAX_CROSSFADE_LOOPS}（輸出可能不到目標長度）")
                n = MAX_CROSSFADE_LOOPS

            v_labels = [f"v{i}" for i in range(n)]
            parts.append(f"[base_v]split={n}[{']['.join(v_labels)}]")
            prev = v_labels[0]
            for i in range(1, n):
                offset = i * (eff_dur - xfade_dur)
                out_label = "looped_v" if i == n - 1 else f"xv{i}"
                parts.append(
                    f"[{prev}][{v_labels[i]}]xfade=transition=fade:"
                    f"duration={xfade_dur}:offset={offset:.6f}[{out_label}]"
                )
                prev = out_label

            if has_audio:
                a_labels = [f"a{i}" for i in range(n)]
                parts.append(f"[base_a]asplit={n}[{']['.join(a_labels)}]")
                prev_a = a_labels[0]
                for i in range(1, n):
                    out_label = "looped_a" if i == n - 1 else f"xa{i}"
                    parts.append(f"[{prev_a}][{a_labels[i]}]acrossfade=d={xfade_dur}[{out_label}]")
                    prev_a = out_label

        else:
            raise ValueError(
                f"[Loop Video] 未支援的 loop_mode={mode!r}，"
                "請改用 'strict' / 'pingpong' / 'crossfade' 其中一個"
            )

        return parts, "looped_v", "looped_a"

    @classmethod
    def IS_CHANGED(s, **kwargs):
        # C1 fix：ComfyUI IS_CHANGED 收到的 linked 輸入（frames/audio）永遠是
        # None，用它判斷 tensor 模式是 dead code。無條件 fingerprint video_path
        # ——tensor 模式下沒被讀，fingerprint 它只是無害的多餘敏感度。
        return path_fingerprint(kwargs.get("video_path", ""))


NODE_CLASS_MAPPINGS = {"MF_LoopVideo": MF_LoopVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_LoopVideo": "🔁 Loop Video"}
