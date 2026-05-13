"""Audio filter helpers — shared between MF_BurnSubtitle.keep_source_audio amix
跟 MF_ComposeAudioMix (Phase 6 audio chain) compile time。

Reason this exists: BurnSubtitle 內 inline 組了 `[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[aout]`
這串字串、ComposeAudioMix 也要組類似的字串(但 chain 內 labels 不固定為 0:a/1:a)。
把 amix 語法 + curve 名稱清單抽到這裡,單一 source of truth、未來 ffmpeg 行為改了
也只改一處。
"""


# FFmpeg afade filter curve 名稱清單 — 給 ComposeAudioFade dropdown 用。
# 不同 curve 影響淡入/淡出的感官曲率:
#   tri  = triangle (線性,最常見)
#   qsin = quarter sine (S-curve、聽起來最自然)
#   esin = exponential sine
#   hsin = half sine (對稱平滑)
#   log  = logarithmic
#   par  = parabolic (尾端快、頭端慢)
#   qua  = quadratic
#   cub  = cubic
#   squ  = square root (頭端快、尾端慢)
#   cbr  = cubic root
FADE_CURVES = ["tri", "qsin", "esin", "hsin", "log", "par", "qua", "cub", "squ", "cbr"]


# amix duration 模式:輸出長度依哪個 input 決定
#   first    = 跟第一個 input 一樣長
#   longest  = 跟最長的 input 一樣長(其他補靜音)
#   shortest = 截到最短 input(其他被截掉)
AMIX_DURATIONS = ["first", "longest", "shortest"]


def build_amix_filter(input_labels, *, duration="first", dropout_transition=0, out_label="aout"):
    """Build `[a][b]...amix=inputs=N:duration=X:dropout_transition=Y[out]` filter string.

    Args:
        input_labels: list of FFmpeg stream labels like ['0:a', '1:a'] or
                      ['[vol_a]', '[bgm_a]'] (chain 中間結果)。**不需要**外層方括號 —
                      函式自動加上(讓 caller 既能傳 raw label 也能傳 bracketed)。
        duration: 'first' / 'longest' / 'shortest' (見 AMIX_DURATIONS)
        dropout_transition: 0 = 關掉(amix 預設 2s 淡入淡出 — 給「中途某條結束」用,
                            兩條都跑到尾時不需要)
        out_label: 輸出 label 名稱(會被加上方括號)

    Returns: 完整 amix filter string,例如:
        "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[aout]"

    為什麼 BurnSubtitle.keep_source_audio + ComposeAudioMix 共用這個 helper:
    兩個都是「兩條 audio stream 混在一起、輸出長度跟第一條走」的同個 use case,
    只是 input labels 來源不同。集中後 ffmpeg 語法改了(例:未來 amix 加新參數) 一處改就好。
    """
    if len(input_labels) < 2:
        raise ValueError(
            f"[audio_mix] amix 至少需要 2 條 input,給了 {len(input_labels)} 條"
        )

    # 自動 normalize labels:給 "0:a" 自動加上 [],給 "[0:a]" 保持原狀
    def _bracket(label):
        return label if label.startswith("[") else f"[{label}]"

    in_block = "".join(_bracket(label) for label in input_labels)
    return (
        f"{in_block}amix=inputs={len(input_labels)}:"
        f"duration={duration}:dropout_transition={dropout_transition}[{out_label}]"
    )


def build_afade_filter(input_label, *, direction="in", start_sec=0.0, duration_sec=2.0,
                       curve="tri", out_label="afaded"):
    """Build `[a]afade=t=in:st=0:d=2:curve=tri[out]` filter string.

    Args:
        input_label: 上游 stream label(可帶或不帶方括號)
        direction: 'in' (淡入) / 'out' (淡出)
        start_sec: 開始時間(秒)。淡入通常 0、淡出通常 video_duration - duration_sec
        duration_sec: 淡入淡出時長(秒)
        curve: FADE_CURVES 內任一值
        out_label: 輸出 label 名稱

    Returns: 完整 afade filter 字串
    """
    if direction not in ("in", "out"):
        raise ValueError(f"[audio_mix] afade direction 必須是 'in' 或 'out',給了 {direction!r}")
    if curve not in FADE_CURVES:
        raise ValueError(
            f"[audio_mix] 未知 fade curve {curve!r}、應該是 {FADE_CURVES} 之一"
        )

    in_label = input_label if input_label.startswith("[") else f"[{input_label}]"
    return (
        f"{in_label}afade=t={direction}:st={start_sec:.6f}:"
        f"d={duration_sec:.6f}:curve={curve}[{out_label}]"
    )


def build_volume_filter(input_label, scale, *, out_label="vol"):
    """Build `[a]volume=X[out]` filter string. scale 0.0=mute / 1.0=原音 / 2.0=2x。

    Used by:
    - MF_ComposeVolume (使用者明確調整)
    - MF_ComposeAudioMix 在 mix 前對 BGM 衰減(常見 mix workflow)
    """
    in_label = input_label if input_label.startswith("[") else f"[{input_label}]"
    return f"{in_label}volume={scale:.6f}[{out_label}]"


def build_loudnorm_filter(input_label, *, target_i=-16.0, target_tp=-1.0, target_lra=11.0,
                          linear=True, out_label="aloud"):
    """Build `[a]loudnorm=I=...:TP=...:LRA=...:linear=true[out]` filter string.

    單 pass loudnorm — 對 streaming(YouTube/Spotify -14 LUFS、podcast -16 LUFS) 用途夠。
    要 EBU R128 broadcast 級準確度才需要 two-pass (跑兩遍 ffmpeg、第一遍 measure、
    第二遍套 measured 值)。MediaForge 預設場景不需要那麼準。

    Defaults 對齊 streaming 慣例:
    - target_i = -16 LUFS (Apple Podcasts / Spotify spoken-word 推薦)
    - target_tp = -1 dBTP (True Peak ceiling、避免 clip)
    - target_lra = 11 LU (Loudness Range、保留動態)
    - linear = true (避免 dynamic range compression、保留原作 mix)
    """
    in_label = input_label if input_label.startswith("[") else f"[{input_label}]"
    linear_str = "true" if linear else "false"
    return (
        f"{in_label}loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:"
        f"linear={linear_str}[{out_label}]"
    )
