# MediaForge

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg) ![License MIT](https://img.shields.io/badge/license-MIT-green.svg) ![ComfyUI](https://img.shields.io/badge/ComfyUI-custom__nodes-orange.svg)

> **Power-user FFmpeg toolkit for ComfyUI.** Tensor↔media bridge with broadcast-grade codec control. Depth > breadth.

FFmpeg-driven custom_nodes plugin: subtitle burn-in, looped video, audio probe, tensor-native frame I/O, silence detection, multi-clip concat, multi-overlay Compose pipeline, and provider-agnostic AI subtitle/translate. All nodes are thin FFmpeg wrappers — no GPU/AI inference baked in. AI nodes use a `MF_AIConfig` connection so you can swap providers in one place.

📖 **[繁體中文 / Traditional Chinese →](README_ZHTW.md)**

## Highlights

- 🎞️ **22 nodes** across 5 categories — Subtitle / Video / Analysis / Compose (now with audio chain) / AI (+ standalone Audio / Net / Image planned)
- 🔗 **Dual-input bridge** — file-consumer nodes accept *either* a `video_path` string *or* an in-memory `IMAGE + AUDIO + tensor_fps` triplet, so MediaForge chains with VHS / AnimateDiff / any IMAGE-pipeline plugin without a SaveVideoFrames round-trip
- 🚀 **Smart GPU codec default** — `h264_nvenc` is auto-selected when NVENC is available, libx264 fallback on CPU-only systems. No per-workflow switching needed
- 📡 **API-ready output** — every file-producing node emits ComfyUI `ui.images` metadata so `/history/<prompt_id>` exposes the output filename; download via `/view?filename=X&subfolder=Y&type=output` (see [Using via API](#using-via-api))
- 🧪 **Broadcast-grade codec control** — H.264 / HEVC / AV1 / ProRes with CRF / bitrate / target-size encode modes; NVENC variants (`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`) auto-detected via `ffmpeg -encoders` probe
- 🎚️ **Single-encode multi-overlay Compose pipeline** — `filter_complex` graph compiler; lossless overlay stacking vs. N re-encodes
- 🎙️ **Audio mixing built-in** — BurnSubtitle's `keep_source_audio` toggle `amix`-es an external audio pin with the source's native audio (default on); cinematic fps rates (23.976 / 29.97 / 59.94) accepted as FLOAT
- 🤖 **Provider-agnostic AI** — `MF_AIConfig` lets you point Whisper / Translate at OpenAI / Groq / Ollama / local backends in one place
- 🪶 **Zero hard dependencies** — only `ffmpeg` + `ffprobe` in PATH. `requests` / `faster-whisper` are lazy-imported on first use
- 🔁 **Tensor-native** — `IMAGE [B,H,W,C] float32` and ComfyUI-canonical `AUDIO` dict; rawvideo roundtrip at PSNR > 38 dB

## Table of Contents

1. [Quick Start](#quick-start)
2. [Using via API](#using-via-api)
3. [Smart GPU codec default](#smart-gpu-codec-default)
4. [Why this plugin (vs VideoHelperSuite)](#why-this-plugin-vs-videohelpersuite)
5. [Nodes (18)](#nodes-18)
6. [AI Provider Recipes](#ai-provider-recipes)
7. [Hidden Contracts](#hidden-contracts)
8. [Architecture](#architecture)
9. [Requirements](#requirements)
10. [Install](#install)
11. [Troubleshooting](#troubleshooting)
12. [FAQ](#faq)
13. [Testing](#testing)
14. [Roadmap](#roadmap)
15. [License & Acknowledgments](#license)

## Quick Start

Three minimal workflows in increasing complexity. Each is described as a node chain — drop them into ComfyUI and wire as shown.

### 1. Auto-cut silence (3 nodes — simplest)

Strip dead air from a lecture recording or podcast.

```
[LoadVideoFrames]──video_path──▶[DetectSilence]──SILENCE_RANGES──▶[TrimByRanges]──▶ output.mp4
                                                                       ▲
                                                                       │ mode=remove
```

| Node | Key inputs |
|---|---|
| `MF_LoadVideoFrames` | `video_path = "lecture.mp4"` |
| `MF_DetectSilence` | `noise_db = -30`, `min_duration_sec = 1.5` |
| `MF_TrimByRanges` | `mode = "remove"` |

### 2. Compose: watermark + intro text + BGM + subtitle in **one** re-encode (5 nodes)

```
[ComposeWatermark]→[ComposeOverlayText]→[ComposeBurnSubtitle]→ MF_COMPOSE_OPS ─┐
                                                                                ▼
[ComposeAudioMix(bgm.mp3)] ────────────────────── MF_COMPOSE_AUDIO_OPS ──► [ComposeVideo] → output.mp4
                                                                                ↑
                                                                                video_path
```

| Node | Key inputs |
|---|---|
| `MF_ComposeWatermark` | `image_path = "logo.png"`, `placement = "bottom_right"`, `relative_scale = 0.12`, `opacity = 0.6` |
| `MF_ComposeOverlayText` | `text = "Episode 01"`, `fontsize = 64`, `start_sec = 0`, `end_sec = 5` |
| `MF_ComposeBurnSubtitle` | `srt_path = "subs.srt"`, `font = "msjh.ttc"`, `font_size = 24` |
| `MF_ComposeAudioMix` | `audio_path = "bgm.mp3"`, `keep_source = True`, `bgm_volume = 0.3` |
| `MF_ComposeVideo` | `video_path = "clip.mp4"`, `target_*=0` (inherit), `codec = "h264_nvenc"`, `crf = 18` |

All four effects accumulate and compile to **one** `filter_complex_script` — the input decodes once and encodes once. Stack 10 overlays + 4 audio ops and you still pay one re-encode.

> **Migrating from v1?** `MF_ComposeStart` + `MF_ComposeFinalize` were merged into `MF_ComposeVideo`. See [the migration guide](#migrating-from-compose-v1).

### 3. AI auto-subtitle: transcribe → translate → burn (6 nodes — most powerful)

```
[AIConfig (ASR)]──▶[WhisperTranscribe]──srt──▶[TranslateSubtitle]──srt──▶[BurnSubtitle]──▶ output.mp4
[AIConfig (LLM)]─────────────────────────────────▲
```

| Node | Key inputs |
|---|---|
| `MF_AIConfig` (ASR) | `provider = "openai"`, `base_url = "https://api.groq.com/openai/v1"`, `model = "whisper-large-v3"` |
| `MF_WhisperTranscribe` | `audio_path = "interview.mp4"`, `backend = "openai_compatible"` |
| `MF_AIConfig` (LLM) | `provider = "openai"`, `base_url = "https://api.openai.com/v1"`, `model = "gpt-4o-mini"` |
| `MF_TranslateSubtitle` | `target_language = "繁體中文"` |
| `MF_BurnSubtitle` | picks up SRT path, sets ASS font/color/outline |

See [AI Provider Recipes](#ai-provider-recipes) below for copy-paste `base_url` / `model` combos.

## Using via API

All file-producing nodes (BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeFinalize / ConvertChinese) emit ComfyUI `ui.images` metadata alongside the STRING path output. API clients can discover and download outputs without parsing paths.

### 1. Submit a workflow

```bash
curl -X POST http://localhost:8188/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt": <workflow_json>}'
# → {"prompt_id": "abc-123", "number": 1, ...}
```

### 2. Poll history for outputs

```bash
curl http://localhost:8188/history/abc-123
```

Each file-producing node exposes its output in two places:

```json
{
  "abc-123": {
    "outputs": {
      "<node_id>": {
        "images": [
          {"filename": "subtitled_00001.mp4", "subfolder": "MediaForge", "type": "output"}
        ]
      }
    }
  }
}
```

(`images` is ComfyUI's universal UI key — video / audio / arbitrary files all flow through it. Downstream-wired STRING `final_video_path` still works for chaining MediaForge nodes; the metadata is additive.)

### 3. Download the output

```bash
curl "http://localhost:8188/view?filename=subtitled_00001.mp4&subfolder=MediaForge&type=output" \
  -o final.mp4
```

`type` is always `output` for MediaForge results. Each run produces a new auto-counter file (`_00001.mp4` → `_00002.mp4` → ...), so multiple workflow runs never silently overwrite.

### Subtitle / text outputs

`MF_ConvertChinese` writes `.srt` or `.txt` (heuristic: presence of `-->` in converted text). `MF_WhisperTranscribe` / `MF_TranslateSubtitle` return SRT **as a string** (not a file) — pass them downstream into `MF_BurnSubtitle` directly, or pipe through `MF_ConvertChinese` with `filename_prefix` set to materialize a file.

## Smart GPU codec default

All encode-capable nodes (BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeFinalize) default their `codec` dropdown to **`h264 NVIDIA GPU (h264_nvenc)`** when ffmpeg detects NVENC support at startup. CPU-only systems automatically fall back to **`h264 (libx264)`** — no manual configuration, no broken defaults on machines without NVIDIA cards.

The probe runs once per ComfyUI session via `utils/encoder.py:pick_default_codec()`, which checks `ffmpeg -encoders` for `h264_nvenc` presence. NVENC variants (`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`) all appear in the dropdown when available; `av1_nvenc` requires Ada Lovelace (RTX 4000+).

To override per-node, just pick from the dropdown — existing workflows that hard-coded `"h264 (libx264)"` keep working (default change only affects newly dragged nodes).

## Why this plugin (vs VideoHelperSuite)

| Capability | VHS | MediaForge | Notes |
|---|---|---|---|
| Load video → IMAGE batch | ✅ opencv | ✅ FFmpeg | MediaForge handles AV1 / HEVC 10-bit / ProRes / VP9 / arbitrary colorspaces |
| Save IMAGE batch → video | ✅ H.264 only | ✅ H.264 / HEVC / AV1 / ProRes | Plus CRF / bitrate / target-size modes |
| Audio in/out (canonical dict) | ⚠️ limited | ✅ both directions | `{'waveform': Tensor[B,C,T], 'sample_rate': int}` |
| Trim by ranges | ✅ image-batch | ✅ video + image-batch | MediaForge takes `SILENCE_RANGES` from `MF_DetectSilence` |
| **Path-level video concat (cross-codec, with audio)** | **❌** | **✅** | MediaForge-only — VHS Combine only stitches IMAGE batches |
| Silence detection | ❌ | ✅ | |
| Subtitle burn | ❌ | ✅ | |
| Multi-overlay Compose pipeline (single re-encode) | ❌ | ✅ | filter_complex graph compiler |
| Watermark preset (opacity / margins / temporal / placement) | ❌ | ✅ | |
| AI subtitle (transcribe + translate) | ❌ | ✅ | provider-agnostic |
| ffprobe metadata | ⚠️ partial | ✅ | |

**TL;DR:** Install both. VHS for fast IMAGE-batch workflows; MediaForge for broadcast encoding, file-level ops, Compose pipeline, and AI-driven subtitle work.

## Nodes (22)

> **Dual-input note**: nodes marked **(dual-input)** below accept *either* a file path (existing `video_path` STRING widget) *or* an in-memory tensor (wire `frames` + `tensor_fps` + `audio` from VHS / AnimateDiff / `MF_LoadVideoFrames` / etc.). When tensor is wired, MediaForge writes a temp .mp4 internally and FFmpeg processes that. Path mode stays the default fast path — no quality loss when chaining MediaForge-to-MediaForge.
>
> The frontend extension `web/dual_input_lock.js` connects widget visibility to wiring state: the path widget hides when `frames` is wired (tensor mode), and `tensor_fps` only appears when needed. Path-mode-only widgets like BurnSubtitle's `keep_source_audio` hide in tensor mode.

### `MediaForge/Subtitle`

#### 🀄 Convert Chinese (`MF_ConvertChinese`)

OpenCC simplified ↔ traditional Chinese conversion for any text or SRT. Four profiles (`s2twp` Taiwan-vocab default / `s2t` generic / `tw2sp` reverse / `t2s` reverse generic). Triple input shape: paste into `text` widget, wire upstream STRING, or read from `input_path`. Optional `filename_prefix` writes to `output/<prefix>_NNNNN.srt` (or `.txt`) with the same auto-counter pattern other producers use; extension auto-detected from `-->` presence in the converted text. Lazy-imports `opencc-python-reimplemented` — install with `pip install opencc-python-reimplemented`.

#### 🔥 Burn Subtitle (`MF_BurnSubtitle`) **(dual-input)**

SRT → hard-burned overlay with full ASS style control. Colors are accepted as `#RRGGBB`; internally converted to ASS BGR-with-alpha.

**Output**: `filename_prefix` STRING (default `MediaForge/subtitled`) — ComfyUI `SaveImage`-style counter pattern; each run produces `output/<prefix>_00001.mp4` → `_00002.mp4` → ... so subsequent runs don't silently overwrite earlier results. Subdirectories OK; extension `.mp4` auto-appended.

Styling knobs:
- **Font**: `font` dropdown reads `<plugin>/font/*.ttf|.otf|.ttc`. Drop a TTF in `font/` and pick it from the dropdown — MediaForge lazy-imports `fontTools` to auto-extract the TTF's internal Family Name for libass. Falls back to the filename stem if `fontTools` isn't installed (`pip install fontTools` recommended).
- **Weight / style**: `bold` + `italic` booleans, `letter_spacing` FLOAT (ASS Spacing in pixels).
- **Outline / shadow / box**: `outline_color_hex` + `outline_width` + `shadow_depth` + `border_style` (1=outline, 3=box with semi-transparent `back_color_hex`).
- **Position**: `alignment` dropdown (9 named positions: `bottom_center (2)`, `top_right (9)`, etc.) + `margin_v` + `margin_l` + `margin_r`. Subtitle effective width = play area − `margin_l` − `margin_r` (so asymmetric margins push the text box left / right while controlling its width).

Optional advanced inputs: `video_path` (file path, fallback when no tensor wired), `tensor_fps` (only used when `frames` is wired), `keep_source_audio` (BOOLEAN, default `True` — when both an external `audio` pin and the source video carry audio, `amix` both tracks; set to `False` for the old replace-source-with-external behavior), `target_fps` (output fps override; `0.0` = sync to source — FLOAT to support cinematic rates like 23.976 / 29.97 / 59.94).

### `MediaForge/Video`

#### 📂 Select Video (`MF_SelectVideo`)

Dropdown picker for `ComfyUI/input/` video files. Walks subdirectories, lists `.mp4 / .mov / .mkv / .webm / .avi / .m4v / .mpg / .mpeg / .ts`. Outputs `STRING video_path` — wire to any file-consumer node. `IS_CHANGED` hashes file mtime so the cache invalidates when you replace the file with a same-named version.

#### 🔁 Loop Video (`MF_LoopVideo`) **(dual-input)**

Loop a video to a target duration. Three modes for different repetition aesthetics, optional speed change and reversal.

**Loop modes**:
- **`strict`** — hard repeat-and-cut to exact duration (no seam smoothing). Fastest and most predictable.
- **`ping_pong`** — A→A-reversed→A→A-reversed (smooth back-and-forth, no visible seam).
- **`crossfade`** — chained `xfade` between repetitions; capped at 50 loops; auto-falls back to `strict` if `crossfade_sec >= effective clip duration` (graceful degradation, not an error).

**Core settings**:
- `target_duration_sec` (FLOAT, default 30.0) — output duration in seconds.
- `crossfade_sec` (FLOAT, default 1.0) — overlap between repetitions; only used in `crossfade` mode.
- `speed` (FLOAT 0.25–4.0, default 1.0) — playback speed (uses `setpts` + `atempo` chain; `atempo` is automatically chained for out-of-range values).
- `reverse` (BOOLEAN) — play the source backwards before looping.
- `keep_audio` (BOOLEAN, default True) + `audio_volume` (FLOAT 0.0–1.0, default 1.0) — attenuate the muxed audio. `0.0` mutes, `0.5` halves, `1.0` keeps original.

**Encoding**: `codec` / `crf` (default 18) / `preset` (default `medium`) — `codec` defaults to GPU NVENC when available. See [Smart GPU codec default](#smart-gpu-codec-default).

**Output**: `filename_prefix` (default `MediaForge/looped`) → `output/MediaForge/looped_<NNNNN>.mp4` (auto-counter).

**Hard limits**: FFmpeg `loop` filter buffers up to `MAX_LOOP_FRAMES = 32767` (INT16). Very long sources at high fps will hit this — use `crossfade` mode (xfade chain has no single buffer) or reduce fps.

#### 📥 Load Video Frames (`MF_LoadVideoFrames`)

FFmpeg-decode any container/codec → `IMAGE` batch `[B,H,W,C] float32 [0,1]` + `AUDIO` dict (`{'waveform': Tensor[B,C,T], 'sample_rate': int}`) + fps + metadata JSON. Rotation-aware (portrait phone videos display correctly), memory-bounded. Optional `target_fps` resample, `max_frames` cap.

#### 📤 Save Video Frames (`MF_SaveVideoFrames`)

`IMAGE` batch (+ optional `AUDIO` dict) → encoded video file. The canonical tensor→file producer; pairs with `MF_LoadVideoFrames` for symmetric roundtrip.

**Encode modes** (single dropdown, mutually exclusive):
- **`crf` (default)** — constant quality (lower number = higher quality / bigger file). `crf` 0=lossless, 18=visually lossless, 23=libx264 standard, 28=acceptable, 51=worst.
- **`bitrate`** — target a specific `bitrate_kbps` (e.g. 4000 = 4 Mbps).
- **`target_size`** — auto-compute bitrate to hit a `target_size_mb` ceiling (two-pass-style estimate from duration).

**Codecs** (auto-corrects container extension):
- H.264 / HEVC / AV1 / ProRes — all CPU + NVENC variants (`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`) auto-detected.
- ProRes → `.mov` (other codecs → `.mp4`). The `prores_ks` encoder uses `yuv422p10le` pix_fmt for 10-bit precision.

**Tensor → fps**: `fps` controls the source frame rate of the rawvideo pipe. For roundtrip from `MF_LoadVideoFrames`, use `meta_fps` from its output.

**Output**: `filename_prefix` → `output/<prefix>_<NNNNN>.<ext>` (ext auto-picked).

#### ✂️ Trim by Ranges (`MF_TrimByRanges`) **(dual-input)**

Cut a video by time ranges. The primary use case is auto-cutting silence (wire from `MF_DetectSilence`), but accepts raw range JSON too for manual edits.

**Range input** (either, mutually exclusive):
- `ranges` (pin) — `SILENCE_RANGES` list `[[start_sec, end_sec], ...]` from upstream (typically `MF_DetectSilence`).
- `ranges_json` (STRING widget, JSON literal) — manual override, e.g. `[[1.5, 3.0], [10.0, 12.5]]`.

**Modes**:
- **`keep`** — keep the ranges, drop everything else. Empty ranges → raises (nothing to keep, refuse to produce empty output).
- **`remove`** — drop the ranges, keep everything else. Empty ranges → identity (no-op, returns full clip).

**Seam handling**:
- `crossfade_sec` (FLOAT, default 0.0) — fade between adjacent kept segments via chained `xfade`. Set to 0 for hard cuts.

**Encoding**: same `codec` / `crf` / `preset` triplet as other encode nodes; GPU NVENC default.

**Output**: `filename_prefix` (default `MediaForge/trimmed`) → `output/<prefix>_<NNNNN>.mp4`.

**Audio handling**: audio and video are interleaved-concat (`[v0][a0][v1][a1]...concat=n=N:v=1:a=1`) so audio stays in sync across cut boundaries. Sources missing audio are auto-handled.

#### 🔗 Concat Videos (`MF_ConcatVideos`) **(dual-input, prepend semantics)**

Stitch multiple video files end-to-end at the path level. Two strategies for different speed/compatibility tradeoffs.

**Modes**:
- **`copy`** — FFmpeg concat demuxer, no re-encode (stream copy). Lightning fast but requires inputs to share codec / resolution / fps / pix_fmt. Best for stitching clips out of the same camera or pre-normalized assets.
- **`transcode`** — `filter_complex` graph with optional transition. Always works, always re-encodes. Required when sources differ in codec / dimensions / fps.

**Transitions** (`transcode` mode only, `transition_sec > 0`):
- `fade`, `wipeleft`, `wiperight`, `slideleft`, `slideright`, `circleopen`, `circleclose`, `dissolve` — FFmpeg's `xfade` built-ins.

**Inputs**:
- `video_paths` (STRING widget, multiline) — one absolute path per line. ≥ 2 required.
- `frames` (IMAGE pin, optional) — when wired, materialised as path[0] (prepended); `video_paths` lines shift to path[1..N]. Needs ≥ 1 path entry to reach the 2-clip minimum.

**Transcode settings**: `fps` / `width` / `height` / `crf` / `codec` / `preset`. GPU NVENC default codec.

**Output**: `filename_prefix` (default `MediaForge/concat`) → `output/<prefix>_<NNNNN>.mp4`.

**Behavior notes**: inputs missing audio are auto-filled with `anullsrc` silence (transcode mode); the demuxer mode refuses on audio-stream mismatch.

### `MediaForge/Analysis`

#### 🔍 Probe Media (`MF_ProbeMedia`) **(dual-input)**

`ffprobe` wrapper that returns structured metadata for any media file. Pure read, no FFmpeg encode invoked.

**Outputs** (6 ports):
- `duration_sec` (FLOAT) — **video stream's** duration. Distinct from container duration (they can differ in muxes like MKV); MediaForge uses video duration as the authoritative timeline.
- `width` / `height` (INT) — **display dimensions** (rotation-aware). For portrait phone videos with rotation metadata, swap happens here so downstream Compose / Save uses the correct orientation.
- `fps` (FLOAT) — parsed from `r_frame_rate` (e.g. `30000/1001` → 29.97).
- `video_codec` (STRING) — e.g. `"h264"`, `"hevc"`, `"av1"`, `""` if no video stream.
- `audio_codec` (STRING) — e.g. `"aac"`, `"opus"`, `""` if no audio stream.

Pairs naturally with `MF_LoadVideoFrames` (probe first for dimensions, then load) and the Compose pipeline (feed dimensions to `MF_ComposeVideo` — or leave its `target_*` at 0 to inherit automatically).

#### 🤫 Detect Silence (`MF_DetectSilence`)

FFmpeg `silencedetect` wrapper → `SILENCE_RANGES` list (`[[start_sec, end_sec], ...]`). The canonical upstream for `MF_TrimByRanges` in lecture-timelapse / podcast pre-edit / streaming-highlight workflows.

**Tunables**:
- `noise_db` (FLOAT, default -30.0) — anything below this dB level for ≥ `min_duration_sec` counts as silence. Less negative (-20) = more aggressive; more negative (-40) = stricter.
- `min_duration_sec` (FLOAT, default 1.5) — minimum silence length to register a range. Below this is treated as natural pause and ignored.

**Output**: `ranges` (SILENCE_RANGES). Empty list = no silence found; downstream `MF_TrimByRanges` treats empty + `mode="remove"` as identity (full clip preserved).

### `MediaForge/Compose` — single-encode pipeline (video overlays + audio chain)

The Compose pipeline gives you **one ffmpeg encode** for any combination of overlays, subtitles, and audio operations. Build two chains in parallel (video overlays + audio ops), feed both into `MF_ComposeVideo`, ship a single output file.

```
[OverlayText] → [Watermark] → [BurnSubtitle] ──► MF_COMPOSE_OPS ─┐
                                                                  ▼
[Volume] → [AudioMix(+bgm)] → [Fade] → [Normalize] ── AUDIO_OPS ──► [ComposeVideo] → output.mp4
                                                                          ↑
                                                                          video_path
```

The **minimal workflow** is 2 nodes: drop a single overlay node, wire it into `ComposeVideo`. Skip the audio chain entirely for video-only work, or vice versa.

#### 🎬 Compose Video (`MF_ComposeVideo`) **(dual-input)**

The endpoint of every Compose workflow — replaces the older `ComposeStart` + `ComposeFinalize` two-node pattern with one node.

**Settings**:
- `video_path` (STRING) + dual-input `frames` / `tensor_fps` / `audio` — source media.
- `target_fps` (FLOAT, **default 0.0**) + `target_width` (INT, default 0) + `target_height` (INT, default 0) — `0` = **inherit from source** (probed via ffprobe + rotation-aware dims). Most workflows leave these at 0.
- `codec` / `crf` / `preset` — `codec` smart-defaults to `h264_nvenc` when NVENC is available, else `libx264`. NVENC variants (`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`) auto-detected. CRF range 0–51 (default 18 = visually lossless).
- `keep_audio` (BOOLEAN, default True) — when no audio chain is wired, preserve the source's audio track.

**Optional chain inputs**:
- `overlays` (`MF_COMPOSE_OPS`) — chain output from any combination of `ComposeOverlayText` / `ComposeOverlayImage` / `ComposeWatermark` / `ComposeBurnSubtitle`. List order = z-order (later = on top).
- `audio_ops` (`MF_COMPOSE_AUDIO_OPS`) — chain output from `ComposeVolume` / `ComposeAudioMix` / `ComposeAudioFade` / `ComposeNormalize`. Order = filter chain order.

**Output**: `filename_prefix` (default `MediaForge/composed`) → `output/<prefix>_<NNNNN>.mp4` (or `.mov` for ProRes). Returns the path + the compiled `filter_complex_script` (debug aid). Emits `ui.images` metadata for API `/history` exposure.

**Behavior**: auto-switches to `-filter_complex_script <tempfile>` when the compiled graph exceeds 6000 chars (avoids Windows command-line length limits).

#### ✏️ Compose Overlay Text (`MF_ComposeOverlayText`)

Append a `drawtext` op spec into the overlay chain.

**Widgets**:

- `text` (multiline STRING) — passed via `textfile=` at compile time to safely escape apostrophes, percents, newlines.
- `x_expr` / `y_expr` — FFmpeg drawtext **position expressions**. Strings, not numbers — see the expression reference below.
- `fontsize` / `fontcolor` / `borderw` / `bordercolor` — standard styling.
- `effect` — animation preset (`none` / `slide_in_left|right|top|bottom` / `marquee_horizontal`). When not `none`, treats `x_expr` / `y_expr` as the **final anchor position** and wraps a motion expression around it. Default `none` = no animation (backward compatible).
- `effect_duration` (FLOAT, default 1.5) — `slide_in_*`: seconds to animate in; `marquee_horizontal`: seconds per full scroll cycle. Ignored when `effect=none`.
- `fontfile` — leave empty for ComposeVideo to fallback to a bundled font (Windows native ffmpeg has no fontconfig).
- `start_sec` / `end_sec` — temporal **visibility** window. Both 0 = always visible. (Note: `effect_duration` is measured from `start_sec` — animation begins when the text appears.)

##### `x_expr` / `y_expr` expression language

FFmpeg `drawtext` accepts arithmetic strings, not just numbers — evaluated **per frame** at encode time, so you can position dynamically. Available variables:

| Variable | Meaning |
|---|---|
| `w`, `h` | Video frame width / height (px) |
| `text_w`, `text_h` | Rendered text bounding-box width / height (px) — automatically updates with `fontsize` |
| `t` | Current frame timestamp (seconds, float) |
| `n` | Current frame number (int) |
| `line_h` (alias `lh`) | Single-line text height |

Supported operators: `+ - * /` and built-in functions (`if(cond,a,b)`, `lt(a,b)`, `mod(x,y)`, `sin(x)`, `between(x,lo,hi)`, ...). Full list in the [FFmpeg drawtext docs](https://ffmpeg.org/ffmpeg-filters.html#drawtext).

**Default `(w-text_w)/2` / `h-text_h-40`** = horizontally centered, 40 px above the bottom edge (typical lower-third placement). `text_w` self-updates with font size, so centering stays correct when you change `fontsize`.

**Common recipes**:

| Goal | `x_expr` | `y_expr` |
|---|---|---|
| Centered on screen | `(w-text_w)/2` | `(h-text_h)/2` |
| Top-left, 30 px padding | `30` | `30` |
| Bottom-right, 30 px padding | `w-text_w-30` | `h-text_h-30` |
| Centered horizontally, top third | `(w-text_w)/2` | `h/3` |
| Marquee (scroll right→left, ~100 px/s) | `w-mod(t*100,w+text_w)` | `h-text_h-20` |
| Vertical bob (sine wave, ±10 px) | `(w-text_w)/2` | `h-text_h-40+10*sin(2*t)` |
| Fly-in from left over 2 s, settle to center | `if(lt(t,2),-text_w+(w-text_w)/2*t/2,(w-text_w)/2)` | `(h-text_h)/2` |

The last two recipes show why `x_expr` / `y_expr` are strings rather than numbers — they let you express time-dependent motion. For the common cases (`slide_in_*`, `marquee_horizontal`), prefer the `effect` dropdown over hand-writing these expressions.

##### `effect` preset reference

When `effect != none`, your `x_expr` / `y_expr` become the **anchor** (final resting position) and the preset wraps a motion expression around them. Examples below assume default anchor `(w-text_w)/2` / `h-text_h-40` and `effect_duration=1.5`:

| Preset | Behavior | `effect_duration` semantics |
|---|---|---|
| `none` | Use `x_expr` / `y_expr` verbatim | — (ignored) |
| `slide_in_left` | Text slides in from off-screen-left, settles at anchor at `start_sec + effect_duration` | Slide-in time (sec) |
| `slide_in_right` | Slides in from off-screen-right | Slide-in time |
| `slide_in_top` | Slides in from above the frame | Slide-in time |
| `slide_in_bottom` | Slides in from below the frame | Slide-in time |
| `marquee_horizontal` | Continuous right→left scroll (ignores `x_expr`, uses `y_expr` for vertical position) | Seconds per full traverse |

The animation timeline is relative to `start_sec`: a `slide_in_left` with `start_sec=3` and `effect_duration=2` means the text appears at second 3, slides for 2 seconds, settles at second 5. `start_sec` / `end_sec` clip visibility independently — they don't affect the motion expression.

For motion not covered by the presets (vertical bob, easing curves, fade), drop back to writing `x_expr` / `y_expr` by hand with `effect=none`.

#### 🖼️ Compose Overlay Image (`MF_ComposeOverlayImage`)

Append a generic `overlay` op for arbitrary image placement.

- `image_path` — PNG / JPG / etc.
- `x_expr` / `y_expr` — absolute or expression-based position.
- `scale_w` — width in pixels (0 = original size, height auto-scaled by aspect ratio).
- `start_sec` / `end_sec` — temporal window.

#### 💧 Compose Watermark (`MF_ComposeWatermark`)

Watermark preset on top of overlay — more convenient UI for the most common case.

- `image_path` — PNG with alpha recommended.
- `placement` — `top_left` / `top_right` / `bottom_left` / `bottom_right` / `center` / `tile` (auto-computes rows/cols from image aspect).
- `relative_scale` (0.05–0.5) — watermark width as fraction of frame width. Resolved at compile time using `ComposeVideo.target_width`.
- `opacity` (0–1) — via `colorchannelmixer alpha`, preserves PNG's own alpha.
- `margin_top` / `right` / `bottom` / `left` — per-side margins.
- `visible_start/end_sec` — both 0 = always visible.

#### 🔥 Compose Burn Subtitle (`MF_ComposeBurnSubtitle`)

The headline addition in v2 — subtitles burn **inside the Compose pipeline**, so you can stack `subtitle + watermark + audio mix` and pay only one encode.

Same widget set as `MF_BurnSubtitle` (font dropdown, full ASS styling, alignment, margins, colors). Drop a `.ttf` / `.otf` / `.ttc` into the plugin's `font/` directory; the dropdown auto-populates. `fontTools` is used to auto-detect the TTF's internal Family Name for libass (lazy-imported — `pip install fontTools` recommended).

The standalone `MF_BurnSubtitle` (in the Subtitle category) stays for one-shot subtitle burning when you don't need other overlays.

#### 🔊 Compose Volume (`MF_ComposeVolume`)

Append a `volume=N` audio filter op.

- `scale` (FLOAT 0.0–2.0, default 1.0) — `0.0` mutes, `0.5` halves, `1.0` original, `2.0` doubles (watch for clipping).

#### 🎵 Compose Audio Mix (`MF_ComposeAudioMix`) **(dual-input audio)**

Mix an external BGM track with the source audio — or replace the source audio entirely.

- `audio_path` (STRING) — BGM file path, or wire `audio` pin (AUDIO dict from another node) for tensor-based input. AUDIO dict materializes to a temp WAV, cleaned up after encode.
- `keep_source` (BOOLEAN, default True) — `True` mixes source + BGM via `amix`; `False` discards source audio and uses only the BGM.
- `bgm_volume` (FLOAT 0.0–2.0, default 0.3) — BGM-side volume scaling applied before mix. The default 0.3 keeps voice dominant in podcast/vlog use.
- `duration` — `first` (output length = source audio) / `longest` / `shortest`.

#### 🌅 Compose Audio Fade (`MF_ComposeAudioFade`)

Append an `afade` op (in / out).

- `direction` — `in` (silent → full) or `out` (full → silent).
- `start_sec` / `duration_sec` — fade window. For `out`, set `start_sec = video_duration - duration_sec`.
- `curve` — 10 FFmpeg curve names: `tri` (linear, default) / `qsin` (quarter sine, smoothest perceptually) / `esin` / `hsin` / `log` / `par` / `qua` / `cub` / `squ` / `cbr`.

#### 📏 Compose Normalize (`MF_ComposeNormalize`)

EBU R128 / streaming-grade loudness normalization via `loudnorm` (single pass).

- `target_i` (LUFS, default -16) — Apple Podcasts / Spotify spoken-word target. YouTube / TikTok use -14; broadcast EBU R128 uses -23.
- `target_tp` (dBTP, default -1.0) — true-peak ceiling. -1 dBTP avoids clipping on consumer playback chains.
- `target_lra` (LU, default 11.0) — loudness range; larger = more dynamics preserved.
- `linear` (BOOLEAN, default True) — `True` avoids dynamic-range compression. Set False to forcefully flatten to the target window (sacrifices dynamics for strict LUFS compliance).

> **Single pass** is enough for streaming use. Strict EBU R128 broadcast certification needs two-pass (measure → re-apply), which MediaForge doesn't currently provide.

### Migrating from Compose v1

If you have workflow JSONs containing `MF_ComposeStart` / `MF_ComposeFinalize`, ComfyUI will show "Missing nodes" warnings. To migrate:

1. Drop a new `MF_ComposeVideo`. Copy the old `ComposeStart`'s `video_path` / `target_*` settings + the old `ComposeFinalize`'s `codec` / `crf` / `preset` / `keep_audio` settings into it.
2. Delete the old `MF_ComposeStart` and `MF_ComposeFinalize` nodes.
3. Take the overlay chain's last output (was an `MF_COMPOSE` IR) and wire it into `MF_ComposeVideo`'s new `overlays` pin (the type is now `MF_COMPOSE_OPS` — same chain topology).
4. If you had `MF_BurnSubtitle` running separately, replace with `MF_ComposeBurnSubtitle` in the chain to fold it into the single encode.

### `MediaForge/AI` — provider-agnostic

Schema marked **experimental** — `MF_AI_CONFIG` API may change inside Phase 5 until Whisper / Translate are validated end-to-end across all provider recipes.

#### ⚙️ AI Config (`MF_AIConfig`)
Outputs `AI_CONFIG` dict (`provider` / `base_url` / `api_key` / `model` / `device` / `extra`). All AI nodes consume this — swap provider in one place.

#### 🗣️ Whisper Transcribe (`MF_WhisperTranscribe`)
Audio path or `AUDIO` dict → SRT text. Two backends:
- `openai_compatible` (any `/v1/audio/transcriptions` endpoint — OpenAI, Groq, local OpenAI-compat servers)
- `faster_whisper_local` (lazy-import `faster-whisper`, runs locally on CPU/CUDA)

#### 🌐 Translate Subtitle (`MF_TranslateSubtitle`)
SRT + target language → translated SRT (timestamps preserved). Uses `/v1/chat/completions` with batched line numbering for alignment.

## AI Provider Recipes

`MF_AIConfig` outputs a dict consumed by all AI nodes. Same `provider` / `base_url` / `api_key` / `model` interface — only the values change. Copy-paste-ready combos:

### OpenAI (official)

```
provider   = openai
base_url   = https://api.openai.com/v1
api_key    = sk-...
model      = whisper-1         # for MF_WhisperTranscribe
           = gpt-4o-mini       # for MF_TranslateSubtitle
```

Paid; most reliable. `whisper-1` is the multilingual GA endpoint.

### Groq (fastest hosted Whisper)

```
provider   = openai            # OpenAI-compatible API surface
base_url   = https://api.groq.com/openai/v1
api_key    = gsk_...
model      = whisper-large-v3  # ASR — ~5–10× faster than OpenAI whisper-1 for similar quality
           = llama-3.3-70b-versatile  # translate
```

Free tier with rate limits; useful when you need to subtitle a 1-hour podcast in under a minute.

### faster-whisper (local, no API key)

```
backend    = faster_whisper_local      # on MF_WhisperTranscribe
device     = cuda                      # or "cpu", "auto"
model      = large-v3                  # downloaded to HF cache on first use
```

`pip install faster-whisper` first. Best for privacy / offline workflows. CPU works but slow (real-time × ~0.3 on a modern laptop); CUDA recommended for non-trivial files.

### Ollama / LM Studio (local OpenAI-compatible)

```
provider   = openai
base_url   = http://localhost:11434/v1     # Ollama
           = http://localhost:1234/v1      # LM Studio
api_key    = ollama                         # any non-empty string; not validated
model      = llama3.2                       # whatever you have pulled locally
```

For **translate only** — Ollama / LM Studio don't expose Whisper. Pair with `faster_whisper_local` for a fully-offline transcribe + translate pipeline.

## Hidden Contracts

- **IMAGE**: `torch.Tensor [B, H, W, C], float32, [0, 1]`
- **AUDIO**: `{'waveform': torch.Tensor [B, C, T], 'sample_rate': int}` (ComfyUI core canonical)
- **SILENCE_RANGES**: `list[[float, float]]` — `[start_sec, end_sec]` pairs
- **MF_COMPOSE**: `ComposeIR` dataclass (see `utils/compose_ir.py`). Frozen schema after Phase 4 — only additive changes allowed.
- **AI_CONFIG**: `dict` with keys `provider / base_url / api_key / model / device / extra`. Experimental.

## Architecture

```
comfyui_MediaForge/
├── __init__.py              # pkgutil auto-discover nodes/ — drop a file in, it shows up
├── pyproject.toml
├── requirements.txt         # intentionally empty — optional deps lazy-imported
├── nodes/                   # one file per node, MF_<Verb><Noun>
│   ├── ai_config.py            # MF_AIConfig
│   ├── burn_subtitle.py        # MF_BurnSubtitle  — uses font/ subdir
│   ├── compose_video.py        # MF_ComposeVideo  — Compose v2 endpoint (replaces Start + Finalize)
│   ├── compose_overlay_text.py # MF_ComposeOverlayText
│   ├── compose_overlay_image.py# MF_ComposeOverlayImage
│   ├── compose_watermark.py    # MF_ComposeWatermark
│   ├── compose_burn_subtitle.py# MF_ComposeBurnSubtitle  — subtitle in Compose chain (single encode)
│   ├── compose_volume.py       # MF_ComposeVolume  — audio chain
│   ├── compose_audio_mix.py    # MF_ComposeAudioMix  — BGM mix (dual-input audio)
│   ├── compose_audio_fade.py   # MF_ComposeAudioFade
│   ├── compose_normalize.py    # MF_ComposeNormalize  — loudnorm
│   ├── concat_videos.py        # MF_ConcatVideos
│   ├── convert_chinese.py      # MF_ConvertChinese  — OpenCC simp↔trad
│   ├── detect_silence.py       # MF_DetectSilence
│   ├── load_video_frames.py    # MF_LoadVideoFrames
│   ├── loop_video.py           # MF_LoopVideo
│   ├── probe_media.py          # MF_ProbeMedia
│   ├── save_video_frames.py    # MF_SaveVideoFrames
│   ├── select_video.py         # MF_SelectVideo  — dropdown picker for input/ videos
│   ├── translate_subtitle.py   # MF_TranslateSubtitle
│   ├── trim_by_ranges.py       # MF_TrimByRanges
│   └── whisper_transcribe.py   # MF_WhisperTranscribe
├── utils/
│   ├── ass_style.py         # ASS subtitle style helpers (shared by BurnSubtitle + ComposeBurnSubtitle)
│   ├── audio_mix.py         # amix / afade / volume / loudnorm filter builders
│   ├── color.py             # hex_to_ass_color: #RRGGBB → ASS BGR+alpha
│   ├── compose_ir.py        # ComposeIR + AudioOp + compile_ir + compile_audio_chain
│   ├── compose_ops.py       # MF_COMPOSE_OPS / AUDIO_OPS dispatch + watermark resolver
│   ├── encoder.py           # codec catalog + NVENC probe + pick_default_codec + build_encoder_args
│   ├── ffmpeg.py            # ensure_ffmpeg / run_ffmpeg / probe / probe_has_audio_stream
│   ├── output_path.py       # resolve_output_path + output_path_to_ui_entry (API metadata helper)
│   └── video_io.py          # rawvideo pipe ↔ IMAGE/AUDIO + encode_tensor_to_tempfile
├── font/                    # drop .ttf / .otf here for MF_BurnSubtitle font_file dropdown
├── web/
│   └── dual_input_lock.js   # frontend extension: hide widgets per dual-input mode
│                            #   - lock_widget / hidden_when_connected: hide on tensor mode
│                            #   - linked_widgets: hide on path mode
└── tests/                   # 58 tests, pytest-runnable
    ├── test_compose_ir.py        # 8 IR spike cases (Phase 4 prerequisite)
    ├── test_compose_e2e.py       # 3 real-ffmpeg e2e
    ├── test_video_io_roundtrip.py# 5 PSNR > 38 dB rawvideo roundtrip
    └── test_codex_r*_fixes.py    # 42 regression tests across 10 review rounds
```

**Add a new node:** drop `nodes/<verb>_<noun>.py` with `MF_<Verb><Noun>` class + `NODE_CLASS_MAPPINGS` / `NODE_DISPLAY_NAME_MAPPINGS`. The aggregator picks it up — restart ComfyUI.

> ⚠️ **Silent failure to know about:** module-level import errors **silently exclude** the node from the menu. If your new node doesn't appear, run `python -c "from custom_nodes.comfyui_MediaForge.nodes.<your_file> import *"` to see the real error. Optional deps (`requests`, `faster-whisper`, `yt-dlp`) **must** be lazy-imported inside the FUNCTION method, never at module top.

## Requirements

- ComfyUI
- Python ≥ 3.10
- **FFmpeg + FFprobe in PATH**
- Optional: `requests` (for any AI node hitting an HTTP provider), `faster-whisper` (local Whisper backend) — lazy-imported on first use

FFmpeg install:
- **Windows:** https://www.gyan.dev/ffmpeg/builds/ (essentials build) — extract and add `bin/` to PATH
- **macOS:** `brew install ffmpeg`
- **Linux:** `apt install ffmpeg` / `dnf install ffmpeg` / `pacman -S ffmpeg`

Verify install: `ffmpeg -version && ffprobe -version` should both print version banners.

## Install

### Via ComfyUI Manager (recommended)

1. Open ComfyUI Manager → "Install Custom Nodes"
2. Search **"MediaForge"** → Install → Restart ComfyUI

### Manual git clone

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/leon80148/comfyui_MediaForge.git
```

Restart ComfyUI — nodes appear under `MediaForge/Subtitle | Video | Compose | AI | Analysis`.

### Optional dependencies (lazy — install only what you actually use)

```bash
pip install requests          # any node hitting an HTTP AI provider
pip install faster-whisper    # only for backend="faster_whisper_local"
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Nodes don't appear in menu after restart | Module-level import error (ComfyUI swallows it silently) | `python -c "from custom_nodes.comfyui_MediaForge.nodes.<file> import *"` to surface the real traceback |
| `RuntimeError: ffmpeg ... failed` | FFmpeg returned non-zero exit | Scroll up — the last 30 lines of FFmpeg stderr are printed before the raise |
| FFmpeg complains about filter syntax with `:` in path (Windows) | Path leaked into filter graph unescaped | We auto-handle this via `escape_filter_path()` — only an issue if you wrote a custom node bypassing it |
| Whisper local backend extremely slow | CTranslate2 CPU inference | Set `device = "cuda"` in `MF_AIConfig`; or use a hosted backend (Groq is much faster than local CPU) |
| `loop` filter errors on very long source | Buffered-frame limit (`MAX_LOOP_FRAMES = 32767`, INT16) | Reduce fps or use `crossfade` mode (xfade chain, no single buffer) |
| Translate output line count drifts | Small LLM lost track of numbered batches | Switch to a stronger model (`gpt-4o`, `llama-3.3-70b-versatile`); the prompt does line-number alignment but small models miss it on > 50-line batches |
| `reverse` filter OOM on long source | `reverse` loads the entire stream into RAM | Currently a hard limit; cut to a sub-clip first or split-and-stitch manually |

## FAQ

**Q: Why not use VideoHelperSuite for everything?**
A: VHS is excellent for IMAGE-batch workflows. MediaForge handles **file-level** operations (cross-codec concat, audio-aware trim, broadcast encoders) and **single-encode multi-overlay** Compose. Install both.

**Q: Will you add diffusion / generation nodes?**
A: No — by design. MediaForge is the FFmpeg-side toolkit. AI generation belongs in ComfyUI core / dedicated plugins. The only AI we ship is post-production (Whisper transcribe, LLM translate).

**Q: Why are AI nodes "experimental"?**
A: The `AI_CONFIG` schema may evolve while Phase 5 is in shakedown. Once Whisper + Translate are validated end-to-end across all 4 provider recipes, the schema freezes.

**Q: Can I use my own FFmpeg build (e.g. with extra codecs)?**
A: Yes — we call `ffmpeg` / `ffprobe` from `PATH`. Put your preferred build earlier on `PATH` to override the system one.

**Q: Does this run on Apple Silicon?**
A: Yes — FFmpeg + `faster-whisper` both work on M1/M2/M3. Metal isn't supported by faster-whisper directly, but CPU is workable thanks to CTranslate2 INT8 quantization. For hosted ASR, Groq's recipe runs unchanged.

**Q: How do I share a workflow with someone who doesn't have MediaForge?**
A: They'll see "Missing nodes" warnings. Direct them to ComfyUI Manager → "Install Missing Custom Nodes". Standard ComfyUI plugin behavior.

**Q: Why no Audio domain nodes yet?**
A: Phase 4.5 already ships **Compose-chain** audio ops (Volume / AudioMix / Fade / Normalize — folded into the single-encode pipeline alongside video overlays). The dedicated Phase 6 standalone Audio domain (file-level denoise, normalize-file, cut/trim, ducking) comes after AI shakedown. The `MediaForge/Audio` category name is reserved for those standalone nodes.

## Testing

The repo ships **58 tests** covering IR compilation, real-ffmpeg roundtrips, and 10 rounds of Codex-review regression tests:

```bash
cd ComfyUI/custom_nodes/comfyui_MediaForge
pip install pytest
python -m pytest tests/                      # full suite
python -m pytest tests/test_compose_ir.py    # only IR spike
python -m pytest tests/ -k "video_io"        # only rawvideo roundtrip
```

Real-ffmpeg tests require `ffmpeg` on PATH.

## Roadmap

- **Phase 2** ✅ Foundation bridges — LoadVideoFrames / SaveVideoFrames
- **Phase 3** ✅ Gap-priority — DetectSilence / TrimByRanges / ConcatVideos
- **Phase 4** ✅ Compose pipeline v1 — single-encode multi-overlay (Start + Finalize)
- **Phase 4.5** ✅ Compose pipeline v2 — merged ComposeVideo + subtitle-in-chain + audio chain (Volume / AudioMix / Fade / Normalize)
- **Phase 5** 🚧 AI — WhisperTranscribe / TranslateSubtitle (experimental schema)
- **Phase 6** ⏳ Standalone Audio domain — file-level denoise, normalize-file, audio cut/trim, ducking (the Compose chain audio ops in Phase 4.5 are a subset)
- **Phase 7** ⏳ Net domain — yt-dlp ingest, HTTP fetch (lazy-import)

## License

MIT — © YingLiang Lu (leon80148).

## Acknowledgments

- **FFmpeg** — the actual workhorse; this plugin is 90% argument formatting
- **ComfyUI** — the runtime + node graph engine
- **VideoHelperSuite** — the prior art that defined the IMAGE-batch contracts MediaForge interoperates with
- **faster-whisper / CTranslate2** — local Whisper backend
