"""Compose op resolvers — translate chain op specs into IR ops/params。

Chain nodes (MF_ComposeOverlay* / MF_ComposeBurnSubtitle / MF_ComposeWatermark) emit
op specs as plain dicts; MF_ComposeVideo at execution time dispatches them into
ComposeIR via these helpers.

設計原則:op spec 是「使用者意圖」(relative_scale=0.15、placement="bottom_right")、
IR 是「絕對座標」(scale_w=288、x="W-w-20")。Resolution 延後到 ComposeVideo
compile 時做、原因:overlay 節點被 wire 時還不知道 target_width 是多少 (使用者可能
設 target_*=0 沿用 source dimensions、compile 時才 probe 出來)。
"""
import math

from .ffmpeg import probe


# Watermark placement preset 公式 — {margin_*} placeholder 在 resolve 時 fill in。
# 從 nodes/compose_watermark.py 搬過來、ComposeWatermark 節點變成純 op spec
# producer 後這邏輯只剩 compile 時 resolve 用。
PLACEMENT_PRESETS = {
    "top_left": ("{margin_l}", "{margin_t}"),
    "top_right": ("W-w-{margin_r}", "{margin_t}"),
    "bottom_left": ("{margin_l}", "H-h-{margin_b}"),
    "bottom_right": ("W-w-{margin_r}", "H-h-{margin_b}"),
    "center": ("(W-w)/2", "(H-h)/2"),
    "tile": ("0", "0"),  # tile filter 已平鋪滿幅、overlay 從 (0,0) 起算即可
}


def probe_image_dims(path):
    """ffprobe → (width, height);解析失敗回 (0, 0)。給 tile mode 算 row/col 用。"""
    info = probe(path)
    if not info:
        return (0, 0)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return (0, 0)
    return (int(v.get("width", 0)), int(v.get("height", 0)))


def resolve_watermark_params(op_params, image_path, target_width, target_height):
    """Translate watermark op spec params → IR overlay op params。

    Input (op spec):
        placement / relative_scale / opacity / margin_top/right/bottom/left /
        visible_start_sec / visible_end_sec
    Output (給 ir.append_op("overlay", <output>, extra_input=...)):
        x / y / scale_w / [alpha] / [tile] / [start_sec, end_sec]

    relative_scale → scale_w = target_width * relative_scale (clamp 最少 8 px,避免 1×1)
    tile mode 用 ffprobe 拿真實 aspect ratio 算需要幾 row 才能滿幅 (對長條 logo 也準確)。
    """
    placement = op_params.get("placement", "bottom_right")
    if placement not in PLACEMENT_PRESETS:
        raise ValueError(f"[Compose Watermark] 未知 placement={placement!r}")

    x_tpl, y_tpl = PLACEMENT_PRESETS[placement]
    margins = {
        "margin_l": int(op_params.get("margin_left", 20)),
        "margin_r": int(op_params.get("margin_right", 20)),
        "margin_t": int(op_params.get("margin_top", 20)),
        "margin_b": int(op_params.get("margin_bottom", 20)),
    }
    x_expr = x_tpl.format(**margins)
    y_expr = y_tpl.format(**margins)

    relative_scale = float(op_params.get("relative_scale", 0.15))
    scale_w = max(8, int(target_width * relative_scale))

    params = {"x": x_expr, "y": y_expr, "scale_w": scale_w}

    opacity = float(op_params.get("opacity", 0.7))
    if opacity < 0.999:
        # IR overlay op 走 colorchannelmixer aa=k 把 PNG alpha 整體乘 k
        params["alpha"] = opacity

    vs = float(op_params.get("visible_start_sec", 0))
    ve = float(op_params.get("visible_end_sec", 0))
    if ve > vs:
        params["start_sec"] = vs
        params["end_sec"] = ve

    if placement == "tile":
        # Tile mode 計算需要幾 column × row 才能覆蓋整個 frame。
        # 用 ffprobe 拿 watermark 真實 aspect ratio 算 scaled_h、否則正方形假設(對長條 logo
        # row 數會嚴重低估、輸出 只覆蓋 30% 畫面)。
        wm_w, wm_h = probe_image_dims(image_path)
        if wm_w > 0 and wm_h > 0:
            scaled_h = scale_w * (wm_h / wm_w)
        else:
            print(f"[Compose Watermark] 注意:無法讀取 {image_path} 尺寸、tile 行數按正方形估計")
            scaled_h = float(scale_w)
        cols = max(1, math.ceil(target_width / scale_w))
        rows = max(1, math.ceil(target_height / max(1.0, scaled_h)))
        params["tile"] = f"{cols}x{rows}"

    return params


def apply_overlays_to_ir(ir, overlays):
    """Dispatch MF_COMPOSE_OPS list → IR ops。

    Each op spec dict:
        {"type": "drawtext" | "overlay" | "watermark" | "subtitle", "params": {...}, "image_path"?: ...}

    IR 必須已 init (有 main_label + target_width/height)。順序 = z-order。
    """
    for op_spec in overlays:
        kind = op_spec.get("type")
        params = dict(op_spec.get("params", {}))  # defensive copy

        if kind == "drawtext":
            ir.append_op("drawtext", params)

        elif kind == "overlay":
            image_path = op_spec.get("image_path")
            if not image_path:
                raise ValueError("[Compose] overlay op 缺 image_path")
            wm_label = ir.add_image_input(image_path)
            ir.append_op("overlay", params, extra_input=wm_label)

        elif kind == "watermark":
            image_path = op_spec.get("image_path")
            if not image_path:
                raise ValueError("[Compose] watermark op 缺 image_path")
            wm_label = ir.add_image_input(image_path)
            resolved = resolve_watermark_params(
                params, image_path, ir.target_width, ir.target_height,
            )
            ir.append_op("overlay", resolved, extra_input=wm_label)

        elif kind == "subtitle":
            # subtitle op 直接帶 srt_path + style_string、compile_ir 內 dispatch
            ir.append_op("subtitle", params)

        else:
            raise NotImplementedError(f"[Compose] 未支援的 overlay op type={kind!r}")


def apply_audio_ops_to_ir(ir, audio_ops):
    """Dispatch MF_COMPOSE_AUDIO_OPS list → IR audio ops。

    Each op spec dict:
        {"type": "volume" | "amix" | "afade" | "loudnorm", "params": {...},
         "audio_path"?: ..., "_is_temp"?: bool}

    amix 特殊處理:
    - keep_source=True:source audio + external BGM 混音
    - keep_source=False:純粹用外部音源 (chain 起點)、source audio 不用

    `_is_temp=True` 的 audio_path (e.g., AUDIO dict materialize 出來的 WAV)
    **故意不**加進 ir.tmp_paths_to_cleanup — 見下方 amix 分支說明 (C5)。
    """
    for op_spec in audio_ops:
        kind = op_spec.get("type")
        params = dict(op_spec.get("params", {}))

        if kind == "volume":
            ir.append_audio_op("volume", params)

        elif kind == "amix":
            audio_path = op_spec.get("audio_path")
            if not audio_path:
                raise ValueError("[Compose Audio] amix op 缺 audio_path")
            # C5 [嚴重] fix：`_is_temp` 的 audio_path 故意不加進
            # ir.tmp_paths_to_cleanup。這個 path 是 MF_ComposeAudioMix 節點
            # 「輸出值」的一部分（op spec dict），會被 ComfyUI 按 upstream 節點的
            # input hash cache 住；只改 ComposeVideo 下游 widget（例如 crf）重跑
            # 時，AudioMix 沒有任何輸入變化、cache-hit 直接回傳舊的 op spec，仍
            # 指著同一個 WAV path。若這裡把它排進 cleanup、ComposeVideo finally
            # 執行完就把該檔刪掉，下次 cache-hit 重跑會對著已刪除的檔案送
            # ffmpeg、每次必炸，直到使用者手動強制 AudioMix 重新執行。改成寫進
            # plugin tmp（nodes/compose_audio_mix.py 用
            # utils.plugin_tmp.mkstemp_in_plugin_tmp）、由
            # utils/plugin_tmp.py 的 sweep_stale_plugin_tmp()（plugin 載入時
            # 呼叫，見 __init__.py）按時效（預設 >24h）回收即可。
            ext_label = ir.add_audio_input(audio_path)

            if params.get("keep_source", True):
                # 混音模式:source + BGM 一起 amix
                ir.append_audio_op("amix", params, extra_audio_input=ext_label)
            else:
                # 純外部音源模式:chain 起點變成 ext_label,跳過 amix 走 anull rename
                ir.append_audio_op("amix", params, depends_on=ext_label)

        elif kind == "afade":
            ir.append_audio_op("afade", params)

        elif kind == "loudnorm":
            ir.append_audio_op("loudnorm", params)

        else:
            raise NotImplementedError(f"[Compose Audio] 未支援的 audio op type={kind!r}")
