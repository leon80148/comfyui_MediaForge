# MediaForge

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg) ![License MIT](https://img.shields.io/badge/license-MIT-green.svg) ![ComfyUI](https://img.shields.io/badge/ComfyUI-custom__nodes-orange.svg)

> **Power-user FFmpeg toolkit for ComfyUI.** Tensor↔media bridge with broadcast-grade codec control. Depth > breadth.

FFmpeg-driven custom_nodes plugin: subtitle burn-in, looped video, audio probe, tensor-native frame I/O, silence detection, multi-clip concat, multi-overlay Compose pipeline, and provider-agnostic AI subtitle/translate. All nodes are thin FFmpeg wrappers — no GPU/AI inference baked in. AI nodes use a `MF_AIConfig` connection so you can swap providers in one place.

📖 **[繁體中文 / Traditional Chinese →](README_ZHTW.md)**

## Highlights

- 🎞️ **17 nodes** across 6 categories — Subtitle / Video / Analysis / Compose / AI (+ Audio / Net / Image planned)
- 🔗 **Dual-input bridge** — file-consumer nodes accept *either* a `video_path` string *or* an in-memory `IMAGE + AUDIO + fps` triplet, so MediaForge chains with VHS / AnimateDiff / any IMAGE-pipeline plugin without a SaveVideoFrames round-trip
- 🧪 **Broadcast-grade codec control** — H.264 / HEVC / AV1 / ProRes with CRF / bitrate / target-size encode modes
- 🎚️ **Single-encode multi-overlay Compose pipeline** — `filter_complex` graph compiler; lossless overlay stacking vs. N re-encodes
- 🤖 **Provider-agnostic AI** — `MF_AIConfig` lets you point Whisper / Translate at OpenAI / Groq / Ollama / local backends in one place
- 🪶 **Zero hard dependencies** — only `ffmpeg` + `ffprobe` in PATH. `requests` / `faster-whisper` are lazy-imported on first use
- 🔁 **Tensor-native** — `IMAGE [B,H,W,C] float32` and ComfyUI-canonical `AUDIO` dict; rawvideo roundtrip at PSNR > 38 dB

## Table of Contents

1. [Quick Start](#quick-start)
2. [Why this plugin (vs VideoHelperSuite)](#why-this-plugin-vs-videohelpersuite)
3. [Nodes (16)](#nodes-16)
4. [AI Provider Recipes](#ai-provider-recipes)
5. [Hidden Contracts](#hidden-contracts)
6. [Architecture](#architecture)
7. [Requirements](#requirements)
8. [Install](#install)
9. [Troubleshooting](#troubleshooting)
10. [FAQ](#faq)
11. [Testing](#testing)
12. [Roadmap](#roadmap)
13. [License & Acknowledgments](#license)

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

### 2. Compose: watermark + intro text in **one** re-encode (5 nodes)

```
[ComposeStart]──▶[ComposeWatermark]──▶[ComposeOverlayText]──▶[ComposeFinalize]──▶ output.mp4
```

| Node | Key inputs |
|---|---|
| `MF_ComposeStart` | `video_path = "clip.mp4"`, `target_width = 1920`, `target_height = 1080` |
| `MF_ComposeWatermark` | `image_path = "logo.png"`, `placement = "BR"`, `relative_scale = 0.12`, `opacity = 0.6` |
| `MF_ComposeOverlayText` | `text = "Episode 01"`, `font_size = 64`, `start_sec = 0`, `end_sec = 5` |
| `MF_ComposeFinalize` | `encoder = "h264"`, `crf = 20` |

All overlay ops accumulate into the Compose IR and compile to **one** `filter_complex_script` — the input is decoded once and encoded once. Stack 10 overlays and you still pay one re-encode.

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

## Nodes (17)

> **Dual-input note**: nodes marked **(dual-input)** below accept *either* a file path (existing `video_path` STRING widget) *or* an in-memory tensor (wire `frames` + `fps` + `audio` from VHS / AnimateDiff / `MF_LoadVideoFrames` / etc.). When tensor is wired, MediaForge writes a temp .mp4 internally and FFmpeg processes that. Path mode stays the default fast path — no quality loss when chaining MediaForge-to-MediaForge.

### `MediaForge/Subtitle`

#### 🔥 Burn Subtitle (`MF_BurnSubtitle`) **(dual-input)**

SRT → hard-burned overlay with full ASS style control. Colors are accepted as `#RRGGBB`; internally converted to ASS BGR-with-alpha.

**Output**: `filename_prefix` STRING (default `MediaForge/subtitled`) — ComfyUI `SaveImage`-style counter pattern; each run produces `output/<prefix>_00001.mp4` → `_00002.mp4` → ... so subsequent runs don't silently overwrite earlier results. Subdirectories OK; extension `.mp4` auto-appended.

Styling knobs:
- **Font**: `font` dropdown reads `<plugin>/font/*.ttf|.otf|.ttc`. Drop a TTF in `font/` and pick it from the dropdown — MediaForge lazy-imports `fontTools` to auto-extract the TTF's internal Family Name for libass. Falls back to the filename stem if `fontTools` isn't installed (`pip install fontTools` recommended).
- **Weight / style**: `bold` + `italic` booleans, `letter_spacing` FLOAT (ASS Spacing in pixels).
- **Outline / shadow / box**: `outline_color_hex` + `outline_width` + `shadow_depth` + `border_style` (1=outline, 3=box with semi-transparent `back_color_hex`).
- **Position**: `alignment` dropdown (9 named positions: `bottom_center (2)`, `top_right (9)`, etc.) + `margin_v` + `margin_l` + `margin_r`. Subtitle effective width = play area − `margin_l` − `margin_r` (so asymmetric margins push the text box left / right while controlling its width).

Optional advanced inputs: `video_path` (file path, fallback when no tensor wired), `tensor_fps` (only used when `frames` is wired), `target_fps` (output fps override; `0` = sync to source).

### `MediaForge/Video`

#### 📂 Select Video (`MF_SelectVideo`)

Dropdown picker for `ComfyUI/input/` video files. Walks subdirectories, lists `.mp4 / .mov / .mkv / .webm / .avi / .m4v / .mpg / .mpeg / .ts`. Outputs `STRING video_path` — wire to any file-consumer node. `IS_CHANGED` hashes file mtime so the cache invalidates when you replace the file with a same-named version.

#### 🔁 Loop Video (`MF_LoopVideo`) **(dual-input)**

Loop to target duration with `strict` / `ping_pong` / `crossfade` modes, optional speed and reverse. `xfade` chain capped at 50 loops; `crossfade_sec >= 有效片段長度` falls back to `strict` (graceful degradation, not error). FFmpeg's `loop` filter buffers up to `MAX_LOOP_FRAMES = 32767` (INT16), so very long sources at high fps need `crossfade` mode instead. `audio_volume` (FLOAT 0.0–1.0, default 1.0) attenuates the muxed audio via FFmpeg's `volume` filter — `0.0` mutes, `0.5` halves, `1.0` keeps original.

#### 📥 Load Video Frames (`MF_LoadVideoFrames`)

FFmpeg-decode any container/codec → `IMAGE` batch `[B,H,W,C] float32 [0,1]` + `AUDIO` dict (`{'waveform': Tensor[B,C,T], 'sample_rate': int}`) + fps + metadata JSON. Rotation-aware (portrait phone videos display correctly), memory-bounded. Optional `target_fps` resample, `max_frames` cap.

#### 📤 Save Video Frames (`MF_SaveVideoFrames`)

`IMAGE` batch + optional `AUDIO` dict → H.264 / HEVC / AV1 / ProRes file with **CRF (default)** / bitrate / target-size encode modes. Auto-corrects container extension (ProRes → `.mov`).

#### ✂️ Trim by Ranges (`MF_TrimByRanges`) **(dual-input)**

Takes `SILENCE_RANGES` (from `MF_DetectSilence`) or raw JSON `[[s,e],...]`. Modes: `keep` / `remove`. Chained xfade for seam fade; audio/video interleaved concat; empty-list identity semantics (no-op).

#### 🔗 Concat Videos (`MF_ConcatVideos`) **(dual-input, prepend semantics)**

Path-level concat of multiple files. `copy` mode → FFmpeg concat demuxer (same-codec fast path). `transcode` mode → filter_complex with optional `xfade` transition (`fade` / `wipeleft` / `wiperight` / `slideleft` / `slideright` / `circleopen` / `circleclose` / `dissolve`). Inputs missing audio are auto-filled with `anullsrc` silence. When `frames` is wired, the tensor is materialised as path[0] (prepended), with `video_paths` lines shifting to path[1..N] — needs ≥1 path entry to reach the 2-clip minimum.

### `MediaForge/Analysis`

#### 🔍 Probe Media (`MF_ProbeMedia`) **(dual-input)**

Returns duration, dimensions, fps, video/audio codec via ffprobe. Returns the **video stream's** duration (distinct from container duration — they can differ).

#### 🤫 Detect Silence (`MF_DetectSilence`)

FFmpeg `silencedetect` wrapper → `SILENCE_RANGES` list (`[[start_sec, end_sec], ...]`). Tunable `noise_db` threshold and `min_duration_sec`. Pairs with `MF_TrimByRanges` for lecture-timelapse / podcast pre-edit / streaming-highlight workflows.

### `MediaForge/Compose` — single-encode multi-overlay pipeline

`MF_Compose*` nodes chain a `MF_COMPOSE` IR (FFmpeg `filter_complex` graph compiler). Only `MF_ComposeFinalize` runs ffmpeg — all intermediate ops accumulate into the IR and compile to one `filter_complex_script`. **Lossless overlay stacking** vs. N re-encodes.

#### 🎬 Compose Start (`MF_ComposeStart`) **(dual-input)**
Init the IR. Sets `target_width / target_height / target_fps`. When `frames` is wired, the temp .mp4 is created here but cleanup is deferred to `MF_ComposeFinalize` (via `ComposeIR.tmp_paths_to_cleanup`) so it survives the full Compose chain.

#### ✏️ Compose Overlay Text (`MF_ComposeOverlayText`)
Append `drawtext` op. Temporal window via `start_sec`/`end_sec` (enable expression). Custom `fontfile` supported. Text is passed via `textfile=` to safely escape apostrophes, percents, and newlines.

#### 🖼️ Compose Overlay Image (`MF_ComposeOverlayImage`)
Append generic `overlay` op. Optional `scale_w` for resize; temporal window supported.

#### 💧 Compose Watermark (`MF_ComposeWatermark`)
Preset overlay with full UX: `placement` (TL/TR/BL/BR/center/tile), `relative_scale` (0.05–0.5 of frame width), `opacity` (via colorchannelmixer alpha), per-side margins, `visible_start/end_sec` temporal window.

#### ✅ Compose Finalize (`MF_ComposeFinalize`)
Compile IR → single FFmpeg encode (H.264 / HEVC / AV1 / ProRes). Returns output path + the compiled `filter_complex_script` (debug aid). Auto-switches to `-filter_complex_script <tempfile>` past 6000 chars.

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
│   ├── compose_start.py        # MF_ComposeStart
│   ├── compose_overlay_text.py # MF_ComposeOverlayText
│   ├── compose_overlay_image.py# MF_ComposeOverlayImage
│   ├── compose_watermark.py    # MF_ComposeWatermark
│   ├── compose_finalize.py     # MF_ComposeFinalize
│   ├── concat_videos.py        # MF_ConcatVideos
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
│   ├── color.py             # hex_to_ass_color: #RRGGBB → ASS BGR+alpha
│   ├── compose_ir.py        # ComposeIR + compile_ir() + tmp_paths_to_cleanup hook
│   ├── ffmpeg.py            # ensure_ffmpeg / run_ffmpeg / probe / escape_filter_path
│   └── video_io.py          # rawvideo pipe ↔ IMAGE/AUDIO + encode_tensor_to_tempfile
├── font/                    # drop .ttf / .otf here for MF_BurnSubtitle font_file dropdown
├── web/
│   └── dual_input_lock.js   # frontend extension: hide path widget when `frames` pin is wired
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
A: Phase 6 — coming after AI shakedown. Will cover denoise, normalize, mix, ducking. The `MediaForge/Audio` category name is reserved.

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
- **Phase 4** ✅ Compose pipeline — single-encode multi-overlay
- **Phase 5** 🚧 AI — WhisperTranscribe / TranslateSubtitle (experimental schema)
- **Phase 6** ⏳ Audio domain — denoise, normalize, mix, ducking
- **Phase 7** ⏳ Net domain — yt-dlp ingest, HTTP fetch (lazy-import)

## License

MIT — © YingLiang Lu (leon80148).

## Acknowledgments

- **FFmpeg** — the actual workhorse; this plugin is 90% argument formatting
- **ComfyUI** — the runtime + node graph engine
- **VideoHelperSuite** — the prior art that defined the IMAGE-batch contracts MediaForge interoperates with
- **faster-whisper / CTranslate2** — local Whisper backend
