"""Serializable H3 render settings. Model discovery reads headers, never loads tensors."""
from .timeline_source import check_relative_name

MODES = ("direct", "latent", "two_stage", "selflift")
DEFAULT_RENDER = {"mode": "direct", "width": 1344, "height": 768,
                  "base_width": 672, "base_height": 384, "model": "", "high_steps": 2,
                  "ref_video_short_edge": 0}


def parse_render(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_RENDER):
        raise ValueError("render 必須是物件，且只能包含已定義的生成設定。")
    out = {**DEFAULT_RENDER, **raw}
    if out["mode"] not in MODES:
        raise ValueError("render.mode 必須是 direct / latent / two_stage / selflift。")
    for key in ("width", "height", "base_width", "base_height", "high_steps", "ref_video_short_edge"):
        v = out[key]
        if type(v) is not int:
            raise ValueError(f"render.{key} 必須是整數。")
        if key == "high_steps":
            valid = 1 <= v <= 10000
        elif key == "ref_video_short_edge":
            valid = 0 <= v <= 4096 and v % 32 == 0
        else:
            valid = 32 <= v <= 8192 and v % 32 == 0
        if not valid:
            raise ValueError(f"render.{key} 超出範圍；尺寸必須是 32 的倍數。")
    if not isinstance(out["model"], str):
        raise ValueError("render.model 必須是模型檔名。")
    if out["model"]:
        check_relative_name(out["model"])
    if out["mode"] in ("latent", "two_stage") and (
            out["base_width"] > out["width"] or out["base_height"] > out["height"] or
            (out["base_width"], out["base_height"]) == (out["width"], out["height"])):
        raise ValueError("基礎尺寸必須小於目標尺寸，且寬高都不能超過目標。")
    if out["mode"] == "selflift" and (out["width"] % 64 or out["height"] % 64):
        raise ValueError("SelfLift 使用 50% 基礎尺寸，目標寬高必須是 64 的倍數。")
    return out


def h3_models():
    import folder_paths
    from safetensors import safe_open
    out = []
    for name in folder_paths.get_filename_list("latent_upscale_models"):
        if not name.lower().endswith(".safetensors"):
            continue
        try:
            with safe_open(folder_paths.get_full_path_or_raise("latent_upscale_models", name), framework="pt", device="cpu") as f:
                keys = set(f.keys())
                if {"conv_in.weight", "in_blocks.0.emb_layers.1.weight", "in_blocks.1.dwconv.weight"} <= keys:
                    out.append(name)
        except Exception:  # unreadable/corrupt headers are not selectable models
            continue
    return out


def validate_runtime(settings, sigmas, sampler):
    """Fail before encoding references / sampling if the recipe cannot run."""
    if settings["mode"] == "direct":
        return
    import nodes
    required = ["LatentUpscaleModelLoader", "MiniMaxH3LatentUpscaleWithModel"]
    if settings["mode"] == "selflift":
        required = ["MiniMaxH3TimelineSelfLiftSampler", "MiniMaxH3FiniteLatentContinuation"]
        if getattr(getattr(sampler, "sampler_function", None), "__name__", "") != "sample_euler":
            raise ValueError("[Sample Segments] SelfLift 只支援標準 Euler；請選擇 euler 或改用一般二階段生成。")
    missing = [n for n in required if n not in nodes.NODE_CLASS_MAPPINGS]
    if missing:
        raise ValueError(f"[Sample Segments] 缺少放大節點：{', '.join(missing)}。SelfLift 需要 TimelineDirector；一般 latent 放大需要新版 ComfyUI。")
    if settings["model"] not in h3_models():
        raise ValueError("[Sample Segments] 請選擇存在且相容的 H3 latent 放大模型；LTX 模型不能用於 H3。")
    if settings["mode"] in ("two_stage", "selflift") and not 1 <= settings["high_steps"] < len(sigmas) - 1:
        raise ValueError(f"[Sample Segments] 高清步數必須介於 1 和 {len(sigmas) - 2}（小於實際 schedule 的總步數）。")
