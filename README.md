# MediaForge

> **Power-user FFmpeg toolkit for ComfyUI.** Tensor↔media bridge with broadcast-grade codec control. Depth > breadth.

FFmpeg-driven custom_nodes plugin: subtitle burn-in, looped video, audio probe, tensor-native frame I/O, silence detection, multi-clip concat, multi-overlay Compose pipeline, and provider-agnostic AI subtitle/translate. All nodes are thin FFmpeg wrappers — no GPU/AI inference baked in. AI nodes use a `MF_AIConfig` connection so you can swap providers in one place.

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

## Nodes (16)

### `MediaForge/Subtitle`

#### 🔥 Burn Subtitle (`MF_BurnSubtitle`)

SRT → hard-burned overlay with full ASS style control (font, color, outline, shadow, alignment, margins).

### `MediaForge/Video`

#### 🔁 Loop Video (`MF_LoopVideo`)

Loop to target duration with `strict` / `ping_pong` / `crossfade` modes, optional speed and reverse. `xfade` chain capped at 50 loops; `crossfade_sec >= 有效片段長度` falls back to `strict`.

#### 📥 Load Video Frames (`MF_LoadVideoFrames`)

FFmpeg-decode any container/codec → `IMAGE` batch `[B,H,W,C] float32 [0,1]` + `AUDIO` dict (`{'waveform': Tensor[B,C,T], 'sample_rate': int}`) + fps + metadata JSON. Replaces v1's `ExtractFrames` / `ExtractAudio` / `ExtractThumbnail`. Optional `target_fps` resample, `max_frames` cap.

#### 📤 Save Video Frames (`MF_SaveVideoFrames`)

`IMAGE` batch + optional `AUDIO` dict → H.264 / HEVC / AV1 / ProRes file with **CRF (default)** / bitrate / target-size encode modes. Replaces v1's `Convert` / `Resize` / `ChangeFps` / `Compress` / `ReplaceAudio` / `TimelapseFromImages`.

#### ✂️ Trim by Ranges (`MF_TrimByRanges`)

Takes `SILENCE_RANGES` (from `MF_DetectSilence`) or raw JSON `[[s,e],...]`. Modes: `keep` / `remove`. Combine with DetectSilence for auto-cut.

#### 🔗 Concat Videos (`MF_ConcatVideos`)

Path-level concat of multiple files. `copy` mode → FFmpeg concat demuxer (same-codec fast path). `transcode` mode → filter_complex with optional `xfade` transition (`fade` / `wipeleft` / `wiperight` / `slideleft` / `slideright` / `circleopen` / `circleclose` / `dissolve`). Inputs missing audio are auto-filled with anullsrc silence.

### `MediaForge/Analysis`

#### 🔍 Probe Media (`MF_ProbeMedia`)

Returns duration, dimensions, fps, video/audio codec.

#### 🤫 Detect Silence (`MF_DetectSilence`)

FFmpeg `silencedetect` wrapper → `SILENCE_RANGES` list (`[[start_sec, end_sec], ...]`). Tunable `noise_db` threshold and `min_duration_sec`. Pairs with `MF_TrimByRanges` for lecture-timelapse / podcast pre-edit / streaming-highlight workflows.

### `MediaForge/Compose` — single-encode multi-overlay pipeline

`MF_Compose*` nodes chain a `MF_COMPOSE` IR (FFmpeg `filter_complex` graph compiler). Only `MF_ComposeFinalize` runs ffmpeg — all intermediate ops accumulate into the IR and compile to one `filter_complex_script`. **Lossless overlay stacking** vs. N re-encodes.

#### 🎬 Compose Start (`MF_ComposeStart`)
Init the IR. Sets `target_width / target_height / target_fps`.

#### ✏️ Compose Overlay Text (`MF_ComposeOverlayText`)
Append `drawtext` op. Temporal window via `start_sec`/`end_sec` (enable expression). Custom `fontfile` supported.

#### 🖼️ Compose Overlay Image (`MF_ComposeOverlayImage`)
Append generic `overlay` op. Optional `scale_w` for resize; temporal window supported.

#### 💧 Compose Watermark (`MF_ComposeWatermark`)
Preset overlay with full UX: `placement` (TL/TR/BL/BR/center), `relative_scale` (0.05–0.5 of frame width), `opacity` (via colorchannelmixer alpha), per-side margins, `visible_start/end_sec` temporal window.

#### ✅ Compose Finalize (`MF_ComposeFinalize`)
Compile IR → single FFmpeg encode (H.264 / HEVC / AV1 / ProRes). Returns output path + the compiled `filter_complex_script` (debug aid). Auto-switches to `-filter_complex_script <tempfile>` past 6000 chars.

### `MediaForge/AI` — provider-agnostic

Schema marked **experimental** — `MF_AI_CONFIG` API may change inside Phase 5 until Whisper / Translate are validated end-to-end.

#### ⚙️ AI Config (`MF_AIConfig`)
Output `AI_CONFIG` dict (`provider` / `base_url` / `api_key` / `model` / `device` / `extra`). All AI nodes consume this — swap provider in one place.

#### 🗣️ Whisper Transcribe (`MF_WhisperTranscribe`)
Audio path or `AUDIO` dict → SRT text. Two backends:
- `openai_compatible` (any `/v1/audio/transcriptions` endpoint — OpenAI, Groq, local OpenAI-compat servers)
- `faster_whisper_local` (lazy-import `faster-whisper`, runs locally on CPU/CUDA)

#### 🌐 Translate Subtitle (`MF_TranslateSubtitle`)
SRT + target language → translated SRT (timestamps preserved). Uses `/v1/chat/completions` with batched line numbering for alignment.

## Hidden contracts

- **IMAGE**: `torch.Tensor [B, H, W, C], float32, [0, 1]`
- **AUDIO**: `{'waveform': torch.Tensor [B, C, T], 'sample_rate': int}` (ComfyUI core canonical)
- **SILENCE_RANGES**: `list[[float, float]]` — `[start_sec, end_sec]` pairs
- **MF_COMPOSE**: `ComposeIR` dataclass (see `utils/compose_ir.py`). Frozen schema after Phase 4 — only additive changes allowed.
- **AI_CONFIG**: `dict` with keys `provider / base_url / api_key / model / device / extra`. Experimental.

## Optional `ai_config` hook on every node

Every MediaForge node accepts an optional `ai_config: AI_CONFIG` input. Most nodes ignore it today — it exists to keep workflows forwards-compatible when Phase 5 wires AI behavior into more nodes.

## Architecture

```
comfyui_MediaForge/
├── __init__.py              # pkgutil auto-discover nodes/
├── pyproject.toml
├── nodes/                   # one file per node, MF_<Verb><Noun>
│   ├── burn_subtitle.py     loop_video.py            probe_media.py
│   ├── load_video_frames.py save_video_frames.py
│   ├── detect_silence.py    trim_by_ranges.py        concat_videos.py
│   ├── compose_start.py     compose_overlay_text.py  compose_overlay_image.py
│   ├── compose_watermark.py compose_finalize.py
│   ├── ai_config.py         whisper_transcribe.py    translate_subtitle.py
├── utils/
│   ├── ffmpeg.py            ensure_ffmpeg / run_ffmpeg / probe / escape_filter_path
│   ├── color.py             ASS color conversion (BGR + alpha)
│   ├── video_io.py          rawvideo pipe ↔ IMAGE/AUDIO tensors
│   └── compose_ir.py        ComposeIR dataclass + compile() pass
└── tests/
    └── test_compose_ir.py   Phase 4 IR Prerequisite Spike acceptance tests
```

**Add a new node:** drop `nodes/<verb>_<noun>.py` with `MF_<Verb><Noun>` class + `NODE_CLASS_MAPPINGS` / `NODE_DISPLAY_NAME_MAPPINGS`. The aggregator picks it up — restart ComfyUI.

## Requirements

- ComfyUI
- Python ≥ 3.10
- **FFmpeg + FFprobe in PATH**
- Optional: `requests` (for any AI node), `faster-whisper` (local Whisper backend) — lazy-imported

Windows: https://www.gyan.dev/ffmpeg/builds/ (essentials build), add `bin/` to PATH.
macOS: `brew install ffmpeg`.
Linux: `apt install ffmpeg` / `dnf install ffmpeg`.

## Install

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/leon80148/comfyui_MediaForge.git
```

Restart ComfyUI — nodes show under `MediaForge/Subtitle | Video | Audio | Compose | AI | Analysis`.

## Run the IR spike tests

```bash
cd ComfyUI/custom_nodes/comfyui_MediaForge
python tests/test_compose_ir.py
```

## License

MIT — © YingLiang Lu (leon80148).
