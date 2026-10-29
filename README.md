# MediaForge

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg) ![License MIT](https://img.shields.io/badge/license-MIT-green.svg) ![ComfyUI](https://img.shields.io/badge/ComfyUI-custom__nodes-orange.svg)

> **Power-user FFmpeg toolkit for ComfyUI.** Tensor↔media bridge with broadcast-grade codec control. Depth > breadth.

FFmpeg-driven custom_nodes plugin: subtitle burn-in, looped video, audio probe, tensor-native frame I/O, silence detection, multi-clip concat, multi-overlay Compose pipeline, provider-agnostic AI subtitle/translate, and a timeline for long videos generated in overlapping segments. Media nodes are thin FFmpeg wrappers. The Timeline nodes add no model loading, sampling or model patching — they reshape the `LATENT` you pass in and decode it with the `VAE` you connect, then FFmpeg encodes. AI nodes use a `MF_AIConfig` connection so you can swap providers in one place.

📖 **[繁體中文 / Traditional Chinese →](README_ZHTW.md)**

## Highlights

- 🎞️ **30 nodes** across 6 categories — Subtitle / Video / Analysis / Audio (Phase 6 kickoff) / Compose (now with audio chain) / AI (+ Net / Image planned)
- 🔗 **Dual-input bridge** — file-consumer nodes accept *either* a `video_path` string *or* an in-memory `IMAGE + AUDIO + tensor_fps` triplet, so MediaForge chains with VHS / AnimateDiff / any IMAGE-pipeline plugin without a SaveVideoFrames round-trip
- 🚀 **Smart GPU codec default** — `h264_nvenc` is auto-selected when NVENC is available, libx264 fallback on CPU-only systems. No per-workflow switching needed
- 📡 **API-ready output** — every file-producing node emits ComfyUI `ui.images` metadata so `/history/<prompt_id>` exposes the output filename; download via `/view?filename=X&subfolder=Y&type=output` (see [Using via API](#using-via-api))
- 🧪 **Broadcast-grade codec control** — H.264 / HEVC / AV1 / ProRes with CRF / bitrate / target-size encode modes; NVENC variants (`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`) auto-detected via `ffmpeg -encoders` probe
- 🎚️ **Single-encode multi-overlay Compose pipeline** — `filter_complex` graph compiler; lossless overlay stacking vs. N re-encodes
- 🎙️ **Audio mixing built-in** — BurnSubtitle's `keep_source_audio` toggle `amix`-es an external audio pin with the source's native audio (default on); cinematic fps rates (23.976 / 29.97 / 59.94) accepted as FLOAT
- 🤖 **Provider-agnostic AI** — `MF_AIConfig` lets you point Whisper / Translate at OpenAI / Groq / Ollama / local backends in one place
- 🧵 **Timeline & long video** — plan overlapping segments in a visual editor, carry each segment's latent tail into the next, decode and spill one segment at a time, encode once. MiniMax H3 AV supported; LTX-2 / Wan 2.2 experimental (see [Timeline & Long Video](#timeline--long-video))
- 🪶 **Light dependencies** — `requirements.txt` lists four small pure-Python packages plus `imageio-ffmpeg` / `static-ffmpeg`, the fallbacks used when no system `ffmpeg` / `ffprobe` is found. All are imported only when needed; `faster-whisper` stays optional
- 🔁 **Tensor-native** — `IMAGE [B,H,W,C] float32` and ComfyUI-canonical `AUDIO` dict; rawvideo roundtrip at PSNR > 38 dB

## Table of Contents

1. [Quick Start](#quick-start)
2. [Timeline & Long Video](#timeline--long-video)
3. [Using via API](#using-via-api)
4. [Smart GPU codec default](#smart-gpu-codec-default)
5. [Why this plugin (vs VideoHelperSuite)](#why-this-plugin-vs-videohelpersuite)
6. [Nodes (30)](#nodes-30)
7. [AI Provider Recipes](#ai-provider-recipes)
8. [Hidden Contracts](#hidden-contracts)
9. [Architecture](#architecture)
10. [Requirements](#requirements)
11. [Install](#install)
12. [Troubleshooting](#troubleshooting)
13. [FAQ](#faq)
14. [Testing](#testing)
15. [Roadmap](#roadmap)
16. [License & Acknowledgments](#license)

## Quick Start

Four minimal workflows in increasing complexity. Each is described as a node chain — drop them into ComfyUI and wire as shown.

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

### 3. AI auto-subtitle: transcribe → translate → burn (5 nodes)

```
[AIConfig (ASR)]──▶[WhisperTranscribe]──srt──▶[TranslateSubtitle]──srt──▶[BurnSubtitle]──▶ output.mp4
[AIConfig (LLM)]─────────────────────────────────▲
```

| Node | Key inputs |
|---|---|
| `MF_AIConfig` (ASR) | `provider = "openai_compatible"`, `base_url = "https://api.groq.com/openai/v1"`, `model = "whisper-large-v3"` |
| `MF_WhisperTranscribe` | `audio_path = "interview.mp4"` |
| `MF_AIConfig` (LLM) | `provider = "openai_compatible"`, `base_url = "https://api.openai.com/v1"`, `model = "gpt-4o-mini"` |
| `MF_TranslateSubtitle` | `target_lang = "繁體中文"` |
| `MF_BurnSubtitle` | wire translated SRT into `srt_text`, set ASS font/color/outline |

See [AI Provider Recipes](#ai-provider-recipes) below for copy-paste `base_url` / `model` combos.

### 4. H3 long-video loop (5 MediaForge nodes + core Start / End Loop)

Generate a long AV clip as overlapping 107-frame segments and encode it once. Open `example_workflows/timeline_h3_av_loop.json` (ComfyUI lists it in the template browser) and pick your own model files, or wire it by hand:

1. `⏱️ Edit Timeline` — `fps = 24`, `grid = h3_5+17n`, `mode = generate`; three segments `[0,107)`, `[85,192)`, `[153,260)`, one prompt each.
2. Core `Start Loop` — mode `For`, `start_iteration_index = 0`, `max_iteration` ← Edit Timeline `segment_count`.
3. `🎯 Select Segment` — `segment_index` ← Start Loop `iteration_index`; feed its `prompt` and `length` to the core H3 node that builds your conditioning and empty latent (e.g. `MiniMaxH3ImageToVideo`).
4. `🧬 Extend Latent` — `target_latent` = that empty latent, `mask = hard`, `carry_audio = True`, `state` ← Start Loop `current_iteration_value`; its `latent` goes to your sampler.
5. `➕ Append Segment` — sampled `latent` + video `vae` + `audio_vae`, `state` ← Start Loop `current_iteration_value`; its `state` → End Loop `next_iteration_value` **and** `output_value` (`accumulate = False`).
6. `🧵 Assemble Segments` — `state` ← End Loop `outputs` → one `.mp4` (260 frames, 32 kHz audio).

Wiring details, the other models and the editor: [Timeline & Long Video](#timeline--long-video).

## Timeline & Long Video

> Generate overlapping short segments. Latents are the model's compressed working representation. Extend copies the previous tail into the next segment and masks how much may change. Your sampler generates each segment; Append decodes, removes duplicated overlap, and spills to disk. Assemble encodes the final video. This reduces decoded-frame retention but guarantees neither seamless joins nor constant total memory.

Six nodes: `⏱️ Edit Timeline` plans the segments, `🎯 Select Segment` picks one per iteration, `🧬 Extend Latent` writes the previous tail into the new segment's latent, `➕ Append Segment` decodes and spills it, `🧵 Assemble Segments` encodes the result. `📽️ Load Segment Frames` reads the matching source-video frames and audio when you need a reference or a model-free assembly. Model loaders, text encoders and samplers stay your own core nodes.

### Loop wiring

```
Start Loop (mode For: start_iteration_index 0, max_iteration ← Edit Timeline.segment_count)
  ├─ iteration_index ─────────► Select Segment.segment_index
  └─ current_iteration_value ─► Extend Latent.state + Append Segment.state   (None on the first iteration)
Append Segment.state ─► End Loop.next_iteration_value + End Loop.output_value   (accumulate = False)
End Loop.outputs ─► Assemble Segments.state
```

- `Start Loop` / `End Loop` are ComfyUI core nodes (`utilities/looping`). The loop body is Select Segment → your conditioning / empty-latent nodes → Extend Latent → your sampler → Append Segment.
- Start Loop `cache_iterations` (advanced): `True` lets unchanged segments be reused when only Assemble settings change; `False` recomputes every iteration.
- A preview or other side-effect node inside the loop body must also be wired into End Loop `terminations`; otherwise core rejects the prompt with "Loop body is not closed".
- A linear chain without a loop also works: one Select / Extend / sampler / Append group per segment, each Append `state` feeding the next segment's Extend `state` and Append `state` (or wire the previous sampled latent into Extend `previous_latent`).

### Example workflows

ComfyUI lists a plugin's `example_workflows/` folder in its template browser. Model fields use `SELECT_*` placeholders — pick your own files. The codec is `h264 (libx264)`.

| File | Contents |
|---|---|
| `timeline_h3_av_loop.json` | 3-segment MiniMax H3 AV loop with audio carry |
| `timeline_ltx2_video_carry.json` | 2 LTX-2 segments, video-only carry, single-stage sampler |
| `timeline_wan22_experimental.json` | 2 Wan 2.2 segments, high / low samplers with the re-Extend fix |
| `timeline_frames_assembly.json` | No model: source video → Load Segment Frames → Append → Assemble |

### Timeline editor

Edit Timeline shows a visual editor in place of the raw `timeline_data` widget:

- Drag segment edges. In `generate` mode the right edge changes the length and the left edge the incoming overlap, later segments ripple, and edges snap to the grid's legal values; in `cut` mode an edge moves in whole frames between its neighbours.
- Seams: each segment's incoming seam is `cut` or `crossfade` (frame count + audio curve).
- Auto-fill (segment count, length, overlap); in `cut` mode, import `ranges_json` from 🎞️ Detect Scenes.
- Preview of the selected source video; codecs the browser can't play get a small muted H.264 proxy.
- Upload saves the file into `input/mediaforge_timeline/`.
- `timeline_data` JSON is the only state that is saved and queued; the editor's view state (zoom, selected segment, playhead) lives in node properties and never reaches the prompt.
- If the editor fails to load, the raw JSON widget is shown and stays editable.
- UI text is Traditional Chinese; node, widget and enum names stay English.

The editor calls two local routes, `POST /mediaforge/timeline/probe` and `POST /mediaforge/timeline/proxy`: same-origin requests only, files under ComfyUI `input/` only, no absolute paths in responses; limits 8 GiB per file, 30 min, 8192 px per side, 120 fps. Proxies are cached in ComfyUI's temp folder under `mediaforge_timeline_proxy` (4 GiB / 128 entries / 24 h, LRU). No internet access. Like core's own routes they have no authentication and trust the `Host` header (no DNS-rebinding protection is claimed) — on an untrusted LAN, restrict access at the network level.

### Compatibility

Verified with ComfyUI core 0.42.5 and frontend 1.55.18 (Nodes 2.0 and classic) on an RTX 5090 — the test environment, not a minimum version.

| Model / path | Status | Real-run notes |
|---|---|---|
| MiniMax H3 AV — `grid = h3_5+17n`, 24 fps, batch 1 | **Supported** | 3 segments of 107 frames, overlaps 22 and 39 → 260 frames and 346,667 audio samples at 32 kHz (last 267 samples padded). The carried latent tokens survive the sampler exactly (max diff ≤ 5e-7, also with denoise 0.5). At 832×480 the seam thumbnails differed by MAE ≈ 1 on a 0–255 scale. `carry_audio = True` is H3-only |
| LTX-2 AV — `grid = causal_8n+1`, 25 fps | Experimental (`experimental_causal` on) | Video-only carry; decode audio with core `LTXVAudioVAEDecode` (24 kHz output) into Append `audio`. 2 × 97 frames, overlap 17 → 177 frames; carried video tokens preserved exactly. Single-stage sampler (`SamplerCustomAdvanced`) |
| Wan 2.2 T2V 14B — `grid = causal_4n+1`, 16 fps | Experimental (`experimental_causal` on) | The official two-stage high / low `KSamplerAdvanced` chain needs the fix in [Two-stage samplers](#two-stage-samplers). 2 × 81 frames, overlap 9 → 153 frames; external 48 kHz audio on Assemble → 459,000 samples |
| No model (frames path) | Works | Load Segment Frames → Append `frames` / `audio` → Assemble with Start / End Loop, in all four core cache modes (classic / lru / none / ram_pressure, the default) |

Rejected (the run stops with an error): VACE reference extra tokens, uncropped guides, batch > 1, latents whose layout doesn't match the plan, audio carry on non-H3 latents.

"Supported" does not mean conditioning conflicts are checked, character consistency is guaranteed, or seams are invisible.

### Two-stage samplers

The official Wan 2.2 high / low `KSamplerAdvanced` chain does not preserve the carry: the second stage keeps the first stage's leftover noise in the carried region (measured drift 61.3; the frames after each seam turned to noise). Fix: insert a second `🧬 Extend Latent` before the second sampler — `target_latent` = the first sampler's output, same `segment`, `state` and settings, `on_conflict = carry_wins`. The measured drift is then 9.5e-7. Without the fix, Append stops with `E_CARRY_DRIFT`.

### Memory and disk

- Append decodes one segment at a time (or takes `frames`), removes the overlap, applies crossfades, and spills lossless FFV1 video + float32 WAV parts (≈ 0.5 MB per frame at 1344×768) to the plugin's `.mf_tmp/seg_*` folder. It checks estimated RAM before decoding (`E_RAM`) and free disk before spilling (`E_DISK`) — estimates, not guarantees.
- Assemble does the single lossy encode (codec / crf / preset like other MediaForge encoders; ProRes → `.mov`), writes BT.709 tags, cuts the soundtrack to exactly the video's sample count before the AAC encode, then verifies the written frame count, the audio track and the color tags. It is the only output node among the six. Its optional `audio` replaces the whole soundtrack (padded / trimmed to the video length).
- Audio clock: the first segment fixes sample rate and channels (`E_AUDIO_LAYOUT` otherwise). H3 decoded audio is ≈ 8.3 ms shorter per segment and the next segment fills it — only real gaps and the final tail are padded (`W_AUDIO_PAD`).
- Spill parts are released when the cached state is dropped; stale session folders older than 24 h whose owner process is gone are swept at startup.
- This reduces decoded-frame memory; it does not guarantee constant total memory or seamless joins.

### Migrating from Timeline Director

No automatic workflow or preset conversion — rebuild by concept:

| Timeline Director concept | MediaForge rebuild |
|---|---|
| Material Planner | Edit Timeline (single source; no material library) |
| Select / Prepare Loop Segment | Select Segment + Extend Latent |
| Plan Encoder | Load Segment Frames + core model-specific conditioning |
| Initialize / Accumulate / Finish Loop | core Start Loop + Append Segment + End Loop + Assemble Segments |
| Finite / Low-Memory Sampler | your own sampler + the loop above; Assemble already writes the file, no SaveVideo needed |
| Drift-Control / Soft AV | no equivalent; the `hard` / `taper` / `linear` masks don't claim the same effect |
| SelfLift / Omni / presets | not provided or imported; keep them as separate workflows and rebuild manually |
| Original-audio lock | Assemble `audio` replaces the final soundtrack; no lip-sync conditioning |

## Using via API

All file-producing nodes (BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeVideo / ExtractAudio / ConvertChinese / AssembleSegments) emit ComfyUI `ui.images` metadata alongside the STRING path output. API clients can discover and download outputs without parsing paths.

Note: if `filename_prefix` resolves outside `output/` (a legacy-compat case, e.g. `filename_prefix="../input/cleaned"`), the node still writes the file and returns its path normally, but it won't appear in `/history`'s `ui.images` list — ComfyUI's built-in `/view` endpoint only serves files under `output/`, so there's no valid `/view` URL to expose.

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

`MF_ConvertChinese` writes `.srt` or `.txt` (heuristic: presence of `-->` in converted text). `MF_WhisperTranscribe` / `MF_TranslateSubtitle` return SRT **as a string** (not a file) — wire it into `MF_BurnSubtitle`'s `srt_text` input (the node materializes a temp file internally), or pipe through `MF_ConvertChinese` with `filename_prefix` set when you want the SRT saved as a real file.

### Timeline prompts

`MF_EditTimeline` takes `timeline_data` as a JSON **string** input; `video` / `fps` / `grid` / `mode` are separate inputs (`fps` is the combo string, e.g. `"24"` or `"30000/1001"`; `video` is `"(none)"` for `generate` without a source). `plan_json` is a derived output — it is never an input and can't be pasted back into `timeline_data`.

| Field | Rule |
|---|---|
| `version` | integer `1` |
| `source.origin_frame` | source time origin, default `0` |
| `segments` | 1–256 segments |
| `start` / `end` | integer frames, `end` exclusive; must pass the `mode` / `grid` validation |
| `prompt` | string, default empty; not inherited from any global prompt |
| `seam_in.kind` | `cut` / `crossfade`; `crossfade` needs a legal `frames` |
| `seam_in.audio_curve` | `auto` / `linear` / `equal_power` |

Unknown fields, duplicate keys, booleans used as integers and non-finite numbers are rejected (`E_TIMELINE_JSON`).

Core Start Loop in API format: `"mode": "For", "mode.start_iteration_index": 0, "mode.max_iteration": [<edit_timeline_id>, 3], "mode.step": 1, "cache_iterations": false` — `mode.max_iteration` is linked to Edit Timeline's `segment_count` (output index 3). Start Loop's `iteration_index` is output 0 and `current_iteration_value` output 4; End Loop's `outputs` is output 0.

A complete model-free prompt (frames path):

```json
{
  "1": {"class_type": "MF_EditTimeline", "inputs": {"video": "clip.mp4", "fps": "24", "grid": "free", "mode": "generate",
        "timeline_data": "{\"version\": 1, \"source\": {\"origin_frame\": 0}, \"segments\": [{\"start\": 0, \"end\": 40, \"prompt\": \"a\"}, {\"start\": 30, \"end\": 70, \"prompt\": \"b\", \"seam_in\": {\"kind\": \"crossfade\", \"frames\": 4}}]}"}},
  "2": {"class_type": "StartLoop", "inputs": {"mode": "For", "mode.start_iteration_index": 0,
        "mode.max_iteration": ["1", 3], "mode.step": 1, "cache_iterations": false}},
  "3": {"class_type": "MF_SelectSegment", "inputs": {"segment_plan": ["1", 0], "segment_index": ["2", 0]}},
  "4": {"class_type": "MF_LoadSegmentFrames", "inputs": {"segment": ["3", 0], "resize": "none",
        "width": 1344, "height": 768, "shortfall": "error"}},
  "5": {"class_type": "MF_AppendSegment", "inputs": {"segment": ["3", 0], "state": ["2", 4],
        "frames": ["4", 0], "audio": ["4", 1]}},
  "6": {"class_type": "EndLoop", "inputs": {"accumulate": false, "output_value": ["5", 0],
        "next_iteration_value": ["5", 0]}},
  "7": {"class_type": "MF_AssembleSegments", "inputs": {"state": ["6", 0], "filename_prefix": "MediaForge/timeline",
        "codec": "h264 (libx264)", "crf": 18, "preset": "medium"}}
}
```

`clip.mp4` is a file under `input/`. Assemble emits `ui.images` like the other file producers, so `/history` lists its output.

## Smart GPU codec default

All encode-capable nodes (BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeVideo / AssembleSegments) default their `codec` dropdown to **`h264 NVIDIA GPU (h264_nvenc)`** when ffmpeg detects NVENC support at startup. CPU-only systems automatically fall back to **`h264 (libx264)`** — no manual configuration, no broken defaults on machines without NVIDIA cards.

The probe runs once per ComfyUI session via `utils/encoder.py:pick_default_codec()`, which checks `ffmpeg -encoders` for `h264_nvenc` presence. NVENC variants (`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`) all appear in the dropdown when available; `av1_nvenc` requires Ada Lovelace (RTX 4000+).

**CRF-equivalent quality (`cq`)**: when `crf` maps to an NVENC encoder, `utils/encoder.py:build_encoder_args()` emits `-rc vbr -cq <n> -b:v 0`. The `-b:v 0` matters — without it NVENC still applies its default ~2 Mbps bitrate target on top of `-cq`, capping quality no matter how low you set `crf`.

**Same number ≠ same quality across encoder families.** MediaForge passes your `crf` value through verbatim (no auto-conversion — converting silently would make "re-run with another codec at the same crf" unpredictable), and there is no universal equal-quality conversion table: the mapping shifts with encoder generation, preset, and content. What *is* fixed per encoder:

| Encoder | Rate-control flag emitted | Valid range | Encoder's own default |
|---|---|---|---|
| `libx264` | `-crf` | 0–51 | 23 |
| `libx265` | `-crf` | 0–51 | 28 |
| `libsvtav1` | `-crf` | 0–63 native (MediaForge's unified `crf` widget caps at 51 — the unreachable 52–63 tail is lowest-quality territory) | 35 |
| `h264_nvenc` / `hevc_nvenc` / `av1_nvenc` | `-rc vbr -cq <n> -b:v 0` | 1–51 explicit; `0` = automatic (encoder picks — **not** max quality) | (bitrate-driven unless `cq` set) |

Within each family's explicit range, lower = higher quality / bigger file (NVENC's `cq=0` is the auto sentinel, not "better than 1"). Note where the encoders' *own defaults* sit — x265 and SVT-AV1 scales run numerically higher than x264 for comparable intent — and that NVENC `-cq` does not track `libx264 -crf` unit-for-unit. When switching codec families, treat your old value as a starting point only: encode a short representative clip, compare size/quality, and adjust from there.

To override per-node, just pick from the dropdown — existing workflows that hard-coded `"h264 (libx264)"` keep working (default change only affects newly dragged nodes).

## Why this plugin (vs VideoHelperSuite)

| Capability | VHS | MediaForge | Notes |
|---|---|---|---|
| Load video → IMAGE batch | ✅ opencv | ✅ FFmpeg | MediaForge handles AV1 / HEVC 10-bit / ProRes / VP9 / arbitrary colorspaces |
| Save IMAGE batch → video | ✅ H.264 only | ✅ H.264 / HEVC / AV1 / ProRes | Plus CRF / bitrate / target-size modes |
| GIF export | ❌ | ✅ | `MF_SaveVideoFrames` `codec=gif (palette)` — two-pass palette + Bayer dither |
| Audio in/out (canonical dict) | ⚠️ limited | ✅ both directions | `{'waveform': Tensor[B,C,T], 'sample_rate': int}` |
| Trim by ranges | ✅ image-batch | ✅ video + image-batch | MediaForge takes `SILENCE_RANGES` from `MF_DetectSilence` |
| Lossless cut (no re-encode) | ❌ | ✅ | `MF_TrimByRanges` `precision=lossless (stream copy)` |
| **Path-level video concat (cross-codec, with audio)** | **❌** | **✅** | MediaForge-only — VHS Combine only stitches IMAGE batches |
| Silence detection | ❌ | ✅ | |
| Scene-change detection | ❌ | ✅ | `MF_DetectScenes` — wires straight into `MF_TrimByRanges` |
| Subtitle burn | ❌ | ✅ | |
| Multi-overlay Compose pipeline (single re-encode) | ❌ | ✅ | filter_complex graph compiler |
| Watermark preset (opacity / margins / temporal / placement) | ❌ | ✅ | |
| AI subtitle (transcribe + translate) | ❌ | ✅ | provider-agnostic |
| ffprobe metadata | ⚠️ partial | ✅ | |

**TL;DR:** Install both. VHS for fast IMAGE-batch workflows; MediaForge for broadcast encoding, file-level ops, Compose pipeline, and AI-driven subtitle work.

## Nodes (30)

Each node section below follows the same template: **purpose → when to use → required widgets → optional inputs → output → example**. Skim the widget tables to find the knob you need; fall back to the example for typical wiring.

> **Dual-input note**: nodes marked **(dual-input)** accept *either* a file path (the `video_path` STRING widget) *or* an in-memory tensor (wire `frames` + `tensor_fps` + `audio` from VHS / AnimateDiff / `MF_LoadVideoFrames` / etc.). When tensor is wired, MediaForge writes a temp `.mp4` internally and FFmpeg processes that.
>
> The frontend extension `web/dual_input_lock.js` connects widget visibility to wiring state: the path widget hides when `frames` is wired, and `tensor_fps` only shows up in tensor mode. Path-mode-only widgets like `keep_source_audio` hide in tensor mode.
>
> Some dual-input nodes key off an **AUDIO** pin instead of `frames` — `MF_ComposeAudioMix` / `MF_DetectSilence` / `MF_ExtractAudio` hide their `audio_path` / `audio_source` STRING widget when the `audio` pin is wired. Same mechanism (`DUAL_INPUT_NODES` in `web/dual_input_lock.js`), just a different trigger socket.
>
> **Output filename pattern**: every file-producing node uses `filename_prefix` + auto-counter (same pattern as ComfyUI core `SaveImage`). Each run writes `output/<prefix>_00001.mp4` → `_00002.mp4` → ... so repeated workflow runs never silently overwrite earlier results. Subdirectories in the prefix are fine (e.g. `MediaForge/subtitled`).

### `MediaForge/Subtitle`

#### 🀄 Convert Chinese (`MF_ConvertChinese`)

OpenCC simplified ↔ traditional Chinese conversion for plain text or SRT files. Character-level mapping, so SRT timestamps / indices stay intact.

**When to use**: cleaning up a simplified Chinese subtitle for a Taiwanese audience (`s2twp` does 词→詞 vocab swap on top of simp→trad), preprocessing crowdsourced SRTs, normalizing a mixed-encoding subtitle corpus.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `profile` | dropdown | `s2twp (簡→繁台灣詞庫)` | `s2twp` / `s2t` (generic simp→trad) / `tw2sp` (Taiwan→simp) / `t2s` (generic trad→simp) |
| `text` | STRING (multiline) | `""` | Paste text *or* wire from upstream STRING (e.g. `MF_TranslateSubtitle.translated_srt`) |
| `input_path` *(optional)* | STRING | `""` | Used only when `text` is empty. Auto-detects encoding (UTF-8 / GBK / BIG5 / UTF-16) via `charset-normalizer` if installed |
| `filename_prefix` *(optional)* | STRING | `""` | Non-empty → write `output/<prefix>_NNNNN.srt` (or `.txt` if no `-->` present). Empty → in-memory only |

**Output**: `(converted_text: STRING, saved_path: STRING)`. `saved_path` is empty when no `filename_prefix` was given.

**Example chain**: `MF_TranslateSubtitle → MF_ConvertChinese (profile=s2twp) → MF_BurnSubtitle` — translate to simplified, normalize to traditional with Taiwan vocab, then burn.

**Dependencies**: lazy-imports `opencc-python-reimplemented` (`pip install opencc-python-reimplemented`). `charset-normalizer` recommended for non-UTF-8 SRT files.

---

#### 🔥 Burn Subtitle (`MF_BurnSubtitle`) **(dual-input)**

Hard-burn an SRT subtitle file into a video with full ASS style control. Colors are entered as `#RRGGBB` and converted to ASS BGR-with-alpha internally.

**When to use**: shipping a video with permanent (non-toggleable) subtitles — YouTube uploads, social clips, presentation captures. For overlays mixed with watermarks or BGM, prefer `MF_ComposeBurnSubtitle` (single re-encode for the whole chain).

**Required widgets** (organized top-to-bottom in the node body):

| Group | Widget | Default | Notes |
|---|---|---|---|
| Source | `video_path` | `input/sample.mp4` | Hidden in tensor mode |
| Source | `srt_path` | `input/sample.srt` | UTF-8 SRT file |
| Output | `filename_prefix` | `MediaForge/subtitled` | Auto-counter → `output/<prefix>_NNNNN.mp4` |
| Encode | `codec` | smart (NVENC if available, else libx264) | Same catalog as SaveVideoFrames / ComposeVideo |
| Encode | `crf` | `18` (0–51) | Lower = higher quality / bigger file. Passed through verbatim per family (`-crf`, or NVENC `-cq`) — scale meaning differs by codec, see [Smart GPU codec default](#smart-gpu-codec-default) |
| Encode | `preset` | `medium` | `ultrafast` … `veryslow` |
| Font | `font` | `msjh.ttc` if present, else first in `font/` | Reads `<plugin>/font/*.ttf|.otf|.ttc`; `fontTools` auto-detects the Family Name |
| Font | `font_size` | `24` (8–150) | px |
| Font | `font_color_hex` | `#FFFFFF` | Hex RGB |
| Font | `bold` / `italic` | `True` / `False` | ASS Bold / Italic flags |
| Font | `letter_spacing` | `0.0` (0–20) | ASS Spacing in px |
| Outline | `outline_color_hex` | `#000000` | |
| Outline | `outline_width` | `2` (0–10) | px |
| Outline | `shadow_depth` | `1` (0–10) | px |
| Outline | `border_style` | `1` | `1` = outline+shadow, `3` = opaque box with `back_color_hex` |
| Outline | `back_color_hex` | `#000000` | Only visible when `border_style=3` |
| Position | `alignment` | `bottom_center (2)` | 9 named positions (numpad layout 1–9) |
| Position | `margin_v` | `20` (0–500) | Vertical margin from edge in px |
| Position | `margin_l` / `margin_r` | `50` / `50` (0–1000) | Effective subtitle width = play area − `margin_l` − `margin_r` |

**Optional inputs**:

| Input | Type | Default | When used |
|---|---|---|---|
| `srt_text` | STRING (input-only) | — | SRT **content** from `MF_WhisperTranscribe` / `MF_TranslateSubtitle` — written to a plugin-local temp file internally; hides `srt_path` |
| `frames` | IMAGE | — | Wire from VHS / AnimateDiff / LoadVideoFrames; hides `video_path` |
| `tensor_fps` | FLOAT | `30.0` | fps of the temp `.mp4` written from `frames` |
| `audio` | AUDIO | — | External audio pin; merged with source audio by default |
| `keep_source_audio` | BOOLEAN | `True` | Path mode + `audio` pin + source has audio → `amix` both. `False` = external replaces source |
| `target_fps` | FLOAT | `0.0` | `0` = inherit source; FLOAT supports 23.976 / 29.97 / 59.94 cinematic rates |

**Output**: `final_video_path` STRING.

**Example**: `MF_SelectVideo → MF_BurnSubtitle (font=msjh.ttc, font_size=28, alignment=bottom_center (2), outline_width=2)` for a typical 1080p YouTube clip.

---

### `MediaForge/Video`

#### 📂 Select Video (`MF_SelectVideo`)

Dropdown picker for video files under `ComfyUI/input/`. Walks subdirectories recursively, lists `.mp4 / .mov / .mkv / .webm / .avi / .m4v / .mpg / .mpeg / .ts`.

**When to use**: any time you need to pick an input video without typing the path. Wire its `video_path` output into any file-consumer node.

| Widget | Type | Notes |
|---|---|---|
| `video` | dropdown | All matching files under `input/` (rel paths, `/`-normalized). Placeholder shown when empty |

**Output**: `video_path` STRING — absolute path resolved at runtime via `folder_paths.get_input_directory()`.

**Notes**: `IS_CHANGED` uses file mtime, so replacing a file with new content under the same name invalidates downstream caches automatically. Refresh the browser to rescan after adding a new file to `input/`.

---

#### 🔁 Loop Video (`MF_LoopVideo`) **(dual-input)**

Loop a video to a target duration with three seam-handling strategies. Optional speed change and reversal.

**When to use**: padding a short clip to a specific length (intro loops, ambient B-roll, social-media squares that need 15 s / 30 s / 60 s versions of the same clip).

**Loop modes**:
- `strict` — hard repeat-and-cut to exact duration. Fastest, deterministic, visible seam at each repeat.
- `ping_pong` — A → A-reversed → A → A-reversed (seamless back-and-forth, doubles effective length).
- `crossfade` — chained `xfade` between repetitions (capped at 50 loops). Auto-falls back to `strict` if `crossfade_sec >= clip duration`.

**Required widgets**:

| Widget | Default | Range | Notes |
|---|---|---|---|
| `video_path` | `input/sample.mp4` | — | Hidden in tensor mode |
| `filename_prefix` | `MediaForge/looped` | — | → `output/<prefix>_NNNNN.mp4` |
| `target_duration_sec` | `30.0` | 0.1–36000 | Output length in seconds |
| `loop_mode` | `strict` | — | `strict` / `ping_pong` / `crossfade` |
| `crossfade_sec` | `1.0` | 0.1–10 | Used only in `crossfade` mode |
| `speed` | `1.0` | 0.25–4.0 | Uses `setpts` + auto-chained `atempo` |
| `reverse` | `False` | — | Pre-reverse the source before looping |
| `keep_audio` | `True` | — | Mute audio if `False` |
| `audio_volume` | `1.0` | 0.0–1.0 | Attenuate kept audio; `0.0` mutes |
| `codec` / `crf` / `preset` | smart / `18` / `medium` | — | Same encoder family as other producers |

**Optional inputs**: `frames` / `tensor_fps` / `audio` (dual-input triplet).

**Output**: `final_video_path` STRING.

**Limits**: FFmpeg `loop` filter buffers up to `MAX_LOOP_FRAMES = 32767` (INT16). Long sources at high fps hit this — switch to `crossfade` mode (xfade chain has no single buffer) or lower fps.

**Example**: 5-sec clip → 60-sec ambient loop with seamless transitions → `loop_mode=crossfade, target_duration_sec=60, crossfade_sec=0.5`.

---

#### 📥 Load Video Frames (`MF_LoadVideoFrames`)

FFmpeg-decode any container/codec → `IMAGE` batch + `AUDIO` dict + metadata. Replaces VHS's opencv decode for hard-to-decode formats (AV1, HEVC 10-bit, ProRes, VP9).

**When to use**: pulling a video into a tensor-based workflow (frame-by-frame transformation, AI inference, image-batch operations) where you need both video frames and audio in canonical ComfyUI shape.

| Widget | Default | Notes |
|---|---|---|
| `video_path` | `input/sample.mp4` | |
| `target_fps` | `0.0` (= keep original) | `>0` runs `fps` filter to resample |
| `max_frames` | `0` (= unbounded) | Useful for preview / memory cap |
| `load_audio` | `True` | Set `False` to skip audio decode |
| `audio_sr` | `0` (= keep original) | Override sample rate if needed |

**Outputs** (7 ports):

| Output | Type | Shape / meaning |
|---|---|---|
| `frames` | IMAGE | `[B, H, W, C]` float32 [0, 1] |
| `audio` | AUDIO | `{'waveform': Tensor[B, C, T], 'sample_rate': int}` or `None` if no audio stream |
| `fps` | FLOAT | Effective fps (after `target_fps` resample) |
| `width` / `height` | INT | Display dimensions (rotation-aware) |
| `frame_count` | INT | Number of decoded frames |
| `metadata_json` | STRING | Full probe metadata as JSON |

**Notes**: rotation-aware — portrait phone videos render correctly. Memory bounded by `max_frames`. When source has no audio stream, `audio` is `None` (not a fake silent track — that would mislead `MF_SaveVideoFrames`'s `-shortest` mux).

---

#### 📤 Save Video Frames (`MF_SaveVideoFrames`)

Tensor → encoded video file. The canonical tensor→file producer; pairs with `MF_LoadVideoFrames` for symmetric roundtrip (PSNR > 38 dB).

**When to use**: writing the result of a tensor-based pipeline back to disk with broadcast-grade codec control.

| Widget | Default | Notes |
|---|---|---|
| `frames` | (required IMAGE) | `[B, H, W, C]` float32 [0, 1] |
| `filename_prefix` | `MediaForge/video` | Extension auto-picked per codec (.mp4 / .mov / .gif) |
| `fps` | `30.0` | Source fps for the rawvideo pipe — use `LoadVideoFrames.fps` for roundtrip |
| `codec` | smart default | H.264 / HEVC / AV1 / ProRes / `gif (palette)` + NVENC variants |
| `encode_mode` | `crf` | `crf` / `bitrate` / `target_size` |
| `crf` | `18` (0–51) | Used in `crf` mode. Lower = higher quality / bigger file; scale meaning differs by codec family — see [Smart GPU codec default](#smart-gpu-codec-default) |
| `bitrate_kbps` | `4000` | Used in `bitrate` mode (e.g. `4000` = 4 Mbps) |
| `target_size_mb` | `8.0` | Used in `target_size` mode — auto-computes bitrate from duration |
| `preset` | `medium` | `ultrafast` … `veryslow` |
| `pix_fmt_override` | `""` | Leave empty for codec default (yuv420p for x264/x265, yuv422p10le for ProRes) |

**Widget visibility** (frontend-enforced, `web/dual_input_lock.js`): `encode_mode` is a three-way exclusive radio — each value hides the other two rate-control widgets (e.g. `encode_mode=crf` hides `bitrate_kbps` + `target_size_mb`). `codec=gif (palette)` hides the entire rate-control group (`encode_mode` / `crf` / `bitrate_kbps` / `target_size_mb` / `preset` / `pix_fmt_override`) since GIF has no CRF/bitrate/preset/pix_fmt concept. `codec=prores (prores_ks)` hides the same group except `pix_fmt_override` (ProRes still honors a pixel-format override).

**Optional**: `audio` AUDIO dict — muxed into the output if provided. Ignored (printed warning) when `codec=gif (palette)` — GIF has no audio track.

**Output**: `final_video_path` STRING.

**Notes**: ProRes outputs `.mov` (`prores_ks` uses `yuv422p10le` for 10-bit precision). `gif (palette)` outputs `.gif` via a two-pass `palettegen`/`paletteuse` filter (diff-stats palette + Bayer dither) for higher quality than FFmpeg's default GIF encoder. All other codecs → `.mp4`. NVENC variants use `-cq` instead of `-crf` internally but the UI is unified (see [Smart GPU codec default](#smart-gpu-codec-default)).

---

#### ✂️ Trim by Ranges (`MF_TrimByRanges`) **(dual-input)**

Cut a video by time ranges. Primary use case: auto-remove silence (wire from `MF_DetectSilence`). Also accepts manual JSON for hand edits.

**When to use**: removing dead air from a lecture / podcast / livestream recording; keeping only highlighted segments from raw footage.

**Precision modes**:
- `precise (re-encode)` — `trim` + `setpts` re-encode (original behavior, frame-accurate cuts). Default; workflow JSON saved before this widget existed loads unchanged.
- `lossless (stream copy)` — keyframe-seek segment copy (`-c copy`) + concat-demuxer merge. No re-encoding, and much faster than `precise` — but not literally instant: before cutting, it scans the source's keyframe index (`ffprobe -skip_frame nokey`, decoding only keyframes), which takes real time on long sources. Each keep range's start snaps **forward** to the next source keyframe at or after it — never backward, since snapping backward would pull already-removed content back into the output (Codex R6-1). If a keep range contains no keyframe at all (narrower than the source's GOP and unlucky enough to land between two of them), lossless can't represent it and the node **raises** — switch to `precision=precise (re-encode)` or widen the range instead. `crossfade_sec` can't apply (stream copy can't blend pixels) — the widget is hidden in this mode, and any stale non-zero value left over from `precise` mode is **ignored** (with a console warning), not an error. Both the per-segment extraction and the final merge map the main video, audio, and subtitle streams, so multi-track *audio* sources (e.g. English + commentary) keep all their tracks instead of losing the non-default ones — data/timecode streams and attached-picture cover art are skipped instead (with a console warning naming them), since the concat demuxer can't carry codec parameters for arbitrary data streams. Sources with more than one primary *video* stream (e.g. a dual-angle MKV) keep only the first — keyframe alignment is scanned from that stream alone, so mapping the others too would cut them on non-keyframe boundaries and produce undecodable output; the rest are skipped (with a console warning naming the count).

**Range input** (mutually exclusive — pin takes priority):
- `ranges` (pin, optional) — `SILENCE_RANGES` from `MF_DetectSilence` (or `MF_DetectScenes`).
- `ranges_json` (STRING widget, multiline) — manual JSON, e.g. `[[1.5, 3.0], [10.0, 12.5]]`.

| Widget | Default | Notes |
|---|---|---|
| `video_path` | `input/sample.mp4` | |
| `filename_prefix` | `MediaForge/trimmed` | |
| `mode` | `remove` | `remove` = drop ranges, keep rest. `keep` = keep ranges, drop rest |
| `ranges_json` | `"[[0.0, 1.0], [5.0, 7.5]]"` | Used when `ranges` pin not wired |
| `crossfade_sec` | `0.0` (0–2) | `xfade` between adjacent kept segments; `0` = hard cut. Hidden and ignored in `precision=lossless` (stream copy can't blend pixels) |
| `codec` / `crf` / `preset` | smart / `18` / `medium` | Ignored in `precision=lossless` (output container follows the source instead) |
| `precision` | `precise (re-encode)` | `lossless (stream copy)` hides `codec` / `crf` / `preset` / `crossfade_sec` (frontend-enforced) — none of them reach ffmpeg in that mode. Declared as the **last entry of `optional`** (after `tensor_fps`), not in `required`: ComfyUI aligns a saved workflow's widget values positionally across the *entire* required+optional widget list, and `tensor_fps` already occupies a slot there — appending to `required` instead would have shifted it one slot over and corrupted old workflow JSON |

**Mode semantics**:
- `keep` + empty ranges → **raises** (refuses to produce an empty output).
- `remove` + empty ranges → **identity** (returns full clip unchanged).

**Audio sync**: video and audio segments are interleaved-concat (`[v0][a0][v1][a1]…concat=n=N:v=1:a=1`), so audio stays aligned across cuts. Sources without audio produce audio-less output (`-an`) — no silence padding (that's `MF_ConcatVideos`'s behavior, not this node's). (`precision=lossless` instead stream-copies each segment and concat-demuxes them — no filter graph involved.)

**Output container** (`precision=lossless` only): follows the source's extension — `video_path`'s extension in path mode (`.mp4` / `.mov` / `.mkv` / `.webm` / `.avi` / `.m4v`; unrecognized extensions fall back to `.mp4`), or `.mp4` in tensor mode (frames are always staged to a `.mp4` temp file regardless of any downstream container). Exception: if `video_path` is `.webm` and an `audio` AUDIO dict is also wired, the dual-input pre-mux step bakes in AAC audio (WebM can't hold AAC) — the output falls back to `.mkv` instead, with a console warning. `precision=precise` always follows the `codec` choice instead (`.mov` for ProRes, `.mp4` otherwise).

**Example chain**: `MF_LoadVideoFrames → MF_DetectSilence (noise_db=-30) → MF_TrimByRanges (mode=remove, crossfade_sec=0.1)` for podcast pre-edit with subtle fade between segments. For a hard cut with zero quality loss on a long source, wire the same ranges into `MF_TrimByRanges (mode=remove, precision=lossless (stream copy))` instead.

---

#### 🔗 Concat Videos (`MF_ConcatVideos`) **(dual-input, prepend semantics)**

Stitch multiple video files end-to-end at the path level. Two strategies for speed vs. compatibility trade-off.

**When to use**: joining clips from the same camera (use `copy` for instant stream-copy), stitching footage with different codecs or resolutions (use `transcode` with optional transition).

**Modes**:
- `copy` — FFmpeg concat demuxer, no re-encode. Lightning fast. Keeps only the main video, audio, and subtitle streams (`-map 0:V -map 0:a? -map 0:s?`) — data/timecode streams (e.g. GoPro/DJI `tmcd`/`gpmd`) and attached-picture cover art (e.g. from `yt-dlp --embed-thumbnail`) are skipped instead, with a console warning naming the file and stream kind, since the concat demuxer can't carry codec parameters for arbitrary data streams. **Requires** all inputs to match on a preflight probe check scoped to what actually gets mapped: main video (`codec_type=video`, excluding attached pictures) `codec_name`/`width`/`height`/`pix_fmt`/`profile`/`sample_aspect_ratio` per stream, audio `codec_name`/`sample_rate`/`channels`/`channel_layout` per stream, and subtitle `codec_type`/`codec_name` per stream — each compared by stream count and per-stream field equality. Mismatches raise before ffmpeg runs, instead of silently producing a corrupt file (concat demuxer + `-c copy` often exits 0 with glitched output past the first segment, or missing subtitles). Skipped stream kinds (data/attachment/attached-picture) are **not** compared, since they don't affect the output. `time_base` / `level` / `r_frame_rate` are intentionally **not** compared either — the concat demuxer rescales timestamps anyway, and players tolerate level differences, so checking those would over-reject otherwise-safe inputs.
- `transcode` — `filter_complex` graph with optional `xfade` transition. Always works; always re-encodes.

**Required widgets**:

| Widget | Default | Notes |
|---|---|---|
| `video_paths` | multiline — `input/clip1.mp4\ninput/clip2.mp4` | One path per line; ≥ 2 entries required |
| `filename_prefix` | `MediaForge/concat` | |
| `mode` | `transcode` | `copy` (fast) / `transcode` (safe) |
| `transition_sec` | `0.0` (0–5) | xfade duration, only in transcode mode; `0` = hard cut |
| `transition_type` | `fade` | `fade` / `wipeleft` / `wiperight` / `slideleft` / `slideright` / `circleopen` / `circleclose` / `dissolve` |
| `fps` / `width` / `height` | `30.0` / `1920` / `1080` | Target dims for transcode mode |
| `crf` / `codec` / `preset` | `18` / smart / `medium` | Transcode mode only |

**Optional inputs**: `frames` / `tensor_fps` / `audio` — when wired, the tensor becomes path[0] (prepended); `video_paths` lines shift to path[1..N]. You still need ≥ 1 path entry to reach the 2-clip minimum.

**Output**: `final_video_path` STRING.

**Notes**: in `transcode` mode, inputs missing audio are auto-padded with `anullsrc` silence. In `copy` mode, any preflight mismatch (see above) causes a hard fail — pre-normalize with `MF_SaveVideoFrames` if mixing sources. `copy` mode's output container extension follows the **first input's** file extension (not the `codec` widget, which `copy` mode ignores entirely) — e.g. two ProRes `.mov` inputs produce a `.mov` output, not `.mp4` (the `.mp4` muxer has no ProRes tag and would fail to mux).

---

#### ⏱️ Edit Timeline (`MF_EditTimeline`)

Plans a long video as segments on one timeline and outputs the plan the other Timeline nodes read. Edit it in the embedded [timeline editor](#timeline-editor) or send `timeline_data` JSON directly ([Timeline prompts](#timeline-prompts)); the JSON is validated strictly on every run.

**When to use**: the first node of every Timeline workflow — `generate` plans segments for your model to generate; `cut` picks source ranges without latent carry.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `video` | dropdown | `(none)` | Reference source video under `input/`. Optional in `generate`, required in `cut` |
| `fps` | dropdown | `24` | Rational timeline rate: `16` / `24` / `25` / `30` / `24000/1001` / `30000/1001` / `48` / `50` / `60000/1001` / `60`. H3 requires `24` |
| `grid` | dropdown | `h3_5+17n` | Legal segment lengths and overlaps (table below); does not select or load a model. Hidden when `mode = cut` (cut doesn't use it) |
| `mode` | dropdown | `generate` | `generate` plans generated segments; `cut` selects source ranges without latent carry |
| `timeline_data` | STRING (multiline) | `{"version": 1, "source": {"origin_frame": 0}, "segments": [{"start": 0, "end": 107, "prompt": ""}]}` | The plan as a JSON string. Hidden while the visual editor works; shown and editable if the editor fails to load |

| `grid` | Legal lengths (frames) | Legal overlaps (frames) | Carry |
|---|---|---|---|
| `h3_5+17n` | 5 + 17n | 0 or 5 + 17j | video (+ audio with `carry_audio`); fps must be `24` |
| `causal_4n+1` | 1 + 4n | 0 or 1 + 4j | video; needs `experimental_causal` |
| `causal_8n+1` | 1 + 8n | 0 or 1 + 8j | video; needs `experimental_causal` |
| `free` | any ≥ 1 | any | none — the overlap is only trimmed |

`generate`: the first segment starts at 0; starts and ends strictly increase with no gaps, so each overlap is shorter than both neighbours; a `crossfade` seam needs 1 … min(overlap, previous segment's output length) frames. `cut`: needs `video`; segments are sorted, don't overlap (they may touch), stay inside the source and use `cut` seams only.

**Outputs**:

| Output | Type | Notes |
|---|---|---|
| `segment_plan` | MF_SEGMENT_PLAN | Wire to Select Segment |
| `ranges` | SILENCE_RANGES | `cut`: the selected source ranges in seconds (same convention as `MF_DetectScenes`, so it can feed `MF_TrimByRanges`); `generate`: empty list |
| `plan_json` | STRING | The validated plan as JSON, for inspection — never an input |
| `segment_count` | INT | Wire to Start Loop `max_iteration` |
| `total_frames` | INT | Output length in frames |

**Notes**: `IS_CHANGED` fingerprints the selected `video`, so replacing the file re-plans. Prints `W_SOURCE_TIME_UNKNOWN` when the source's start time can't be read.

**Example**: H3 — `fps = 24`, `grid = h3_5+17n`, segments `[0,107)`, `[85,192)`, `[153,260)` (overlaps 22 and 39, both 5 + 17j) → `segment_count = 3`, `total_frames = 260`.

---

#### 🎯 Select Segment (`MF_SelectSegment`)

Picks one segment of the plan. Its `segment` output is the shared spec that Load Segment Frames, Extend Latent and Append Segment read — fps, overlaps and carry sizes are computed once, here.

**When to use**: once per loop iteration, or once per segment in a linear chain.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `segment_index` | INT | `0` (0–255, step 1) | 0-based; in a loop, wire Start Loop `iteration_index`. Out of range → `E_SEGMENT_INDEX` |

**Input**: `segment_plan` (MF_SEGMENT_PLAN) from Edit Timeline.

**Outputs**: `segment` (MF_SEGMENT), `prompt` STRING (this segment's prompt — wire to your text encoder), `length` INT (frames — wire to your empty-latent node), `start_sec` FLOAT (timeline seconds), `segment_count` INT.

---

#### 📽️ Load Segment Frames (`MF_LoadSegmentFrames`)

Decodes the frames and audio of the plan's source video that fall inside one segment, resampled to the timeline fps (frame-exact for same-fps sources, nearest frame otherwise).

**When to use**: reference material for a model (e.g. video-to-video conditioning), or the frames themselves for a model-free assembly (`timeline_frames_assembly.json`). Requires a `video` in Edit Timeline.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `resize` | dropdown | `none` | `none` keeps the source size; `fit` scales with padding; `fill` scales and crops; `stretch` stretches |
| `width` | INT | `1344` (2–7680, step 2) | Hidden when `resize = none` |
| `height` | INT | `768` (2–4320, step 2) | Hidden when `resize = none` |
| `shortfall` | dropdown | `error` | When the source doesn't cover the segment: `error` (`E_SOURCE_SHORTFALL`), `hold_last` (repeat the nearest frame — impossible when there is no frame at all), `black` (pad black) |

**Input**: `segment` (MF_SEGMENT).

**Outputs**: `frames` IMAGE, `audio` AUDIO (`None` when the source has no audio; silence where the segment lies outside the source), `frame_count` INT.

**Notes**: the source is re-resolved and fingerprinted on every run; if it changed since Edit Timeline ran → `E_SOURCE_CHANGED`.

---

#### ➕ Append Segment (`MF_AppendSegment`)

Takes one finished segment, decodes it with your VAE (or takes `frames`), removes the overlap with the previous segment, applies the incoming crossfade and spills lossless parts to disk. Returns a new state; the input state is not modified.

**When to use**: after your sampler, once per segment.

No widgets — sockets only:

| Input | Type | Notes |
|---|---|---|
| `segment` | MF_SEGMENT | Required; from Select Segment |
| `state` *(optional)* | MF_SEGMENT_STATE | Previous segment's state; leave empty for the first segment (in a loop: Start Loop `current_iteration_value`) |
| `latent` *(optional)* | LATENT | This segment's sampled latent; also the carry source for the next segment |
| `vae` *(optional)* | VAE | Decodes the latent's video; required when `frames` is not connected |
| `audio_vae` *(optional)* | VAE | Decodes the audio stream of the same AV latent |
| `frames` *(optional)* | IMAGE | This segment's frames, no VAE involved |
| `audio` *(optional)* | AUDIO | This segment's audio |

**Rules**: `frames` excludes `vae` / `audio_vae` (with `frames`, `latent` may stay connected as the carry source); without `frames`, both `latent` and `vae` are required; `audio` excludes `audio_vae`; `latent` is required when the next segment carries from this one.

**Output**: `state` (MF_SEGMENT_STATE) → End Loop `next_iteration_value` + `output_value`, or the next segment's Extend / Append in a linear chain.

**Checks**: segment order and plan (`E_SEQUENCE`, `E_PLAN`); carry marker on the latent (`W_PROVENANCE` if absent, `E_PROVENANCE` if it doesn't match); carried region unchanged by the sampler (`E_CARRY_DRIFT`); RAM / disk estimates (`E_RAM`, `E_DISK`); the same resolution and audio layout in every segment (`E_AUDIO_LAYOUT`). A crossfade with `audio_curve = auto` uses `linear` when the audio was decoded with `audio_vae` from the same carried H3 latent, otherwise `equal_power`.

---

#### 🧵 Assemble Segments (`MF_AssembleSegments`)

Encodes the spilled parts into the final video — the single lossy encode of the Timeline chain — and is its output node.

**When to use**: after End Loop (wire `outputs` to `state`) or after the last Append of a linear chain.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `filename_prefix` | STRING | `MediaForge/timeline` | → `output/<prefix>_NNNNN.mp4` (`.mov` for ProRes) |
| `codec` | dropdown | smart (NVENC if available, else libx264) | Same catalog as the other encoders |
| `crf` | INT | `18` (0–51) | Hidden for `prores (prores_ks)`. Scale differs by codec — see [Smart GPU codec default](#smart-gpu-codec-default) |
| `preset` | dropdown | `medium` | `ultrafast` … `veryslow`. Hidden for `prores (prores_ks)` |

**Inputs**: `state` (MF_SEGMENT_STATE, required); `audio` (AUDIO, optional) — replaces the whole soundtrack, padded with silence / trimmed to the video length.

**Outputs**: `final_video_path` STRING; `report` STRING (JSON: frames, fps, size, color assumption, audio source / sample rate / samples, padded samples, per-seam diagnostics). Emits `ui.images` for `/history`.

**Notes**: assumes BT.709 (full-range RGB → limited-range YUV) and writes those tags; verifies the frame count, and with internal audio the exact sample count; `E_SPILL_MISSING` if parts were deleted (e.g. `.mf_tmp/` cleaned up, or a cache eviction) — rerun the loop. Spill parts are not deleted here, because the cached state may still be reused.

---

### `MediaForge/Analysis`

#### 🔍 Probe Media (`MF_ProbeMedia`) **(dual-input)**

`ffprobe` wrapper returning structured metadata. Pure read, no FFmpeg encode invoked.

**When to use**: feeding source dimensions into a Compose / Save chain (or just letting the downstream node inherit them via `target_*=0`); inspecting an unfamiliar file before deciding how to process it.

| Widget | Default | Notes |
|---|---|---|
| `media_path` | `input/sample.mp4` | Hidden in tensor mode |
| `frames` / `tensor_fps` / `audio` *(optional)* | — | Dual-input triplet (probes the temp `.mp4`) |

**Outputs** (6 ports):

| Output | Type | Notes |
|---|---|---|
| `duration_sec` | FLOAT | Container `format.duration` — use this as the authoritative timeline |
| `width` / `height` | INT | **Display** dimensions (rotation-aware); portrait phone videos swap here |
| `fps` | FLOAT | Parsed from `r_frame_rate` (`30000/1001` → 29.97) |
| `video_codec` | STRING | `"h264"`, `"hevc"`, `"av1"`, `""` if no video stream |
| `audio_codec` | STRING | `"aac"`, `"opus"`, `""` if no audio stream |

**Example**: probe an unknown clip, then feed `width` / `height` into a `MF_ComposeVideo` workflow (or just leave `target_*=0` and skip the probe — Compose probes internally with the same logic).

---

#### 🤫 Detect Silence (`MF_DetectSilence`)

FFmpeg `silencedetect` wrapper → `SILENCE_RANGES` list. Canonical upstream for `MF_TrimByRanges`.

**When to use**: pre-edit for lectures / podcasts / livestream recordings to remove dead air; identifying speech segments for downstream ASR.

| Widget | Default | Notes |
|---|---|---|
| `audio_source` | `input/sample.mp4` | Path to video or audio file; ignored if `audio` pin wired |
| `noise_db` | `-30.0` (-90 to 0) | dB threshold; less negative (-20) = more aggressive, more negative (-40) = stricter |
| `min_duration_sec` | `0.5` (0.05–60) | Minimum silence length to register a range; shorter gaps treated as natural pauses |

**Optional**: `audio` AUDIO dict — wired-in audio source; takes priority over `audio_source` path.

**Outputs** (3 ports):

| Output | Type | Notes |
|---|---|---|
| `ranges` | SILENCE_RANGES | `[[start_sec, end_sec], ...]` — wire to `MF_TrimByRanges.ranges` |
| `ranges_json` | STRING | Same data as JSON string (for debugging / preview) |
| `count` | INT | Number of detected silence regions |

**Tuning**: typical podcast — `noise_db=-30, min_duration_sec=0.5`. Lecture recording with hum / fan noise — try `noise_db=-25, min_duration_sec=1.0`. Music with quiet passages — go stricter: `noise_db=-50, min_duration_sec=2.0`.

---

#### 🎞️ Detect Scenes (`MF_DetectScenes`)

FFmpeg scene-change detection (`select='gt(scene,threshold)'` + `showinfo`) → scene boundary ranges. Emits the same `SILENCE_RANGES` connection type as `MF_DetectSilence`, so it wires straight into `MF_TrimByRanges` with no adapter node.

**When to use**: splitting raw footage into shots for a highlight reel, feeding scene boundaries into an editing pipeline, or finding cut points in unstructured source video.

| Widget | Default | Notes |
|---|---|---|
| `video_path` | `input/sample.mp4` | |
| `threshold` | `0.4` (0.05–1.0) | FFmpeg `scene` filter score threshold. Lower = more sensitive (flags smaller frame-to-frame changes as cuts) |
| `min_scene_sec` | `1.0` (0–60) | Scenes shorter than this are merged into the previous one, so noisy cuts don't over-fragment the output |

**Outputs** (3 ports):

| Output | Type | Notes |
|---|---|---|
| `scene_ranges` | SILENCE_RANGES | `[[start_sec, end_sec], ...]` covering the entire clip (no gaps) — wire to `MF_TrimByRanges.ranges` |
| `ranges_json` | STRING | Same data as JSON string (for debugging / preview) |
| `count` | INT | Number of scenes after `min_scene_sec` merging |

**Notes**: the `SILENCE_RANGES` type name is a historical artifact — it's a generic `list[[start, end]]` contract shared with `MF_DetectSilence`, not silence-specific. Unlike `MF_DetectSilence` (which only returns the detected silent spans), `MF_DetectScenes` always returns ranges covering the whole clip.

**Example chain**: `MF_DetectScenes (threshold=0.4) → MF_TrimByRanges (ranges pin wired, mode=keep)` — keep only the scenes you want (or `mode=remove` to drop specific ones).

---

### `MediaForge/Audio`

Phase 6's first standalone Audio node. Different from Compose's audio *chain* (which mixes/fades/normalizes *during* a video encode) and from Analysis (which only inspects, never writes) — `MF_ExtractAudio` materializes an audio track to its own file, from either a video/audio path or an in-memory `AUDIO` dict (the only node that can write an `AUDIO` dict to disk).

#### 🎧 Extract Audio (`MF_ExtractAudio`) **(dual-input audio)**

Pull the audio track out of a video (or re-save an audio file) as a standalone audio file. Stream-copies by default (fast, no quality loss); switch `format` to transcode.

**When to use**: producing a standalone audio file for editing/upload from a video source; materializing an in-memory `AUDIO` dict (e.g. from `MF_LoadVideoFrames` or a Compose audio chain) to disk.

| Widget | Default | Notes |
|---|---|---|
| `audio_source` | `input/sample.mp4` | Video or audio file with an audio stream. Hidden when the `audio` pin is wired |
| `format` | `copy` | `copy` (stream copy, no re-encode) / `mp3` / `aac (m4a)` / `wav (pcm_s16le)` / `flac` |
| `filename_prefix` | `MediaForge/audio` | → `output/<prefix>_NNNNN.<ext>` |

**Optional**: `audio` (AUDIO dict) — when wired, `audio_source` is ignored and the dict is materialized to a temp WAV first.

**`format=copy` extension mapping** (source codec → output extension, no re-encode): `aac → .m4a`, `mp3 → .mp3`, `opus → .ogg`, `vorbis → .ogg`, `flac → .flac`, any `pcm_*` → `.wav`. A source codec outside this list **raises**, suggesting a transcode `format` instead.

**Output**: `audio_file_path` STRING.

**Notes**: with an `audio` dict input, `format=copy` has no literal meaning (the dict has no source codec) — it's treated as `wav` and a notice is printed. A source with no audio stream **raises**.

**Example**: `MF_SelectVideo → MF_ExtractAudio (format=mp3)` to pull an MP3 out of a video for a podcast feed; or `MF_LoadVideoFrames → MF_ExtractAudio (audio pin wired)` to materialize a decoded `AUDIO` dict to a WAV file.

---

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

> **Chain semantics**: each overlay / audio op node *appends* to the upstream chain. Wire the previous op's output into this node's `overlays` (or `audio_ops`) optional input; if you leave it unwired, you start a fresh chain. List order = z-order for overlays; for audio it's the filter order.

#### 🎬 Compose Video (`MF_ComposeVideo`) **(dual-input)**

The endpoint of every Compose workflow — replaces the older `ComposeStart` + `ComposeFinalize` two-node pattern with one node.

**When to use**: whenever you have **2+ effects** to apply (overlay + subtitle, watermark + BGM mix, text + audio fade…). For a one-shot single effect, the standalone nodes (`MF_BurnSubtitle`, `MF_LoopVideo`) are usually simpler.

**Required widgets**:

| Widget | Default | Notes |
|---|---|---|
| `video_path` | `input/sample.mp4` | Hidden in tensor mode |
| `filename_prefix` | `MediaForge/composed` | → `output/<prefix>_NNNNN.mp4` (or `.mov` for ProRes) |
| `target_fps` | `0.0` (0–240) | `0` = inherit from source (probed) |
| `target_width` | `0` (0–7680) | `0` = inherit display width (rotation-aware) |
| `target_height` | `0` (0–4320) | `0` = inherit display height |
| `codec` / `crf` / `preset` | smart / `18` / `medium` | NVENC variants auto-detected |
| `keep_audio` | `True` | When no `audio_ops` chain wired, preserve source audio |

**Optional inputs**:

| Input | Type | Notes |
|---|---|---|
| `frames` / `tensor_fps` / `audio` | dual-input | Same triplet as other dual-input nodes |
| `overlays` | `MF_COMPOSE_OPS` | Wire from `ComposeOverlayText` / `OverlayImage` / `Watermark` / `BurnSubtitle` chain head |
| `audio_ops` | `MF_COMPOSE_AUDIO_OPS` | Wire from `ComposeVolume` / `AudioMix` / `AudioFade` / `Normalize` chain head |

**Outputs**: `(final_video_path: STRING, filter_complex_script: STRING)` — the second is the compiled filter graph (useful for debugging). Emits `ui.images` for API `/history` exposure.

**Behavior**: auto-switches to `-filter_complex_script <tempfile>` when the compiled graph exceeds 6000 chars (avoids Windows command-line length limits).

---

#### ✏️ Compose Overlay Text (`MF_ComposeOverlayText`)

Append a `drawtext` op spec into the overlay chain.

**When to use**: titles, lower-thirds, episode markers, animated text reveals. For SRT-driven subtitles, use `MF_ComposeBurnSubtitle` instead.

| Widget | Default | Notes |
|---|---|---|
| `text` | `Hello MediaForge` (multiline) | Passed via `textfile=` so apostrophes / `%` / newlines are safe |
| `x_expr` | `(w-text_w)/2` | FFmpeg drawtext **position expression** (string, not number) |
| `y_expr` | `h-text_h-40` | 40 px above bottom edge — typical lower-third |
| `font` | `msjh.ttc` if present, else first alphabetically | Dropdown of `.ttf` / `.otf` / `.ttc` in plugin `font/`. Empty `font/` → ComposeVideo falls back to a system font (Arial / Helvetica / DejaVu by OS) |
| `fontsize` | `36` (8–300) | px |
| `fontcolor` | `white` | FFmpeg color name *or* hex (`#RRGGBB`) |
| `borderw` | `2` (0–20) | Outline thickness in px |
| `bordercolor` | `black` | Outline color |
| `effect` | `none` | `none` / `slide_in_left|right|top|bottom` / `marquee_horizontal` |
| `effect_duration` | `1.5` (0.1–60) | `slide_in_*`: secs to animate; `marquee_horizontal`: secs per full cycle. Hidden when `effect = none` |
| `start_sec` / `end_sec` | `0.0` / `0.0` | Visibility window. Both `0` = always visible |

**Optional**: `overlays` — upstream chain. Leave unwired to start a fresh chain.

**Output**: `overlays` (`MF_COMPOSE_OPS`) — append to this output's downstream node.

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
| `none` | Use `x_expr` / `y_expr` verbatim | — (widget hidden) |
| `slide_in_left` | Text slides in from off-screen-left, settles at anchor at `start_sec + effect_duration` | Slide-in time (sec) |
| `slide_in_right` | Slides in from off-screen-right | Slide-in time |
| `slide_in_top` | Slides in from above the frame | Slide-in time |
| `slide_in_bottom` | Slides in from below the frame | Slide-in time |
| `marquee_horizontal` | Continuous right→left scroll (ignores `x_expr`, uses `y_expr` for vertical position) | Seconds per full traverse |

The animation timeline is relative to `start_sec`: a `slide_in_left` with `start_sec=3` and `effect_duration=2` means the text appears at second 3, slides for 2 seconds, settles at second 5. `start_sec` / `end_sec` clip visibility independently — they don't affect the motion expression.

For motion not covered by the presets (vertical bob, easing curves, fade), drop back to writing `x_expr` / `y_expr` by hand with `effect=none`.

---

#### 🖼️ Compose Overlay Image (`MF_ComposeOverlayImage`)

Append a generic `overlay` op for arbitrary image placement (absolute position + absolute scale). For preset-based watermarking, prefer `MF_ComposeWatermark`.

**When to use**: a single graphic at a specific position (e.g. logo bug, sticker, lower-third nameplate).

| Widget | Default | Notes |
|---|---|---|
| `image_path` | `input/overlay.png` | PNG / JPG / etc. |
| `x_expr` / `y_expr` | `10` / `10` | Position expression — supports `W`, `H`, `w`, `h`, `t`, etc. |
| `scale_w` | `0` (0–7680) | Width in px; `0` = original size (height auto-scales by aspect ratio) |
| `start_sec` / `end_sec` | `0.0` / `0.0` | Visibility window; both `0` = always visible |

**Optional**: `overlays` — upstream chain.

**Output**: `overlays` (`MF_COMPOSE_OPS`).

---

#### 💧 Compose Watermark (`MF_ComposeWatermark`)

Watermark preset with placement / scale / opacity — convenience wrapper around overlay for the most common case.

**When to use**: branding a video with a logo. `relative_scale` keeps the watermark proportional across different source resolutions.

| Widget | Default | Notes |
|---|---|---|
| `image_path` | `input/watermark.png` | PNG with alpha recommended |
| `placement` | `bottom_right` | `top_left` / `top_right` / `bottom_left` / `bottom_right` / `center` / `tile` (auto rows×cols from image aspect) |
| `relative_scale` | `0.15` (0.05–0.5) | Watermark width as fraction of frame width. Resolved against `ComposeVideo.target_width` |
| `opacity` | `0.7` (0–1) | Applied via `colorchannelmixer alpha` — preserves PNG's own alpha channel |
| `margin_top` / `right` / `bottom` / `left` | `20` each (0–1000) | Per-side margin in px |
| `visible_start_sec` / `visible_end_sec` | `0.0` / `0.0` | Visibility window; both `0` = always visible |

**Optional**: `overlays` — upstream chain.

**Output**: `overlays` (`MF_COMPOSE_OPS`).

**Example**: bottom-right logo bug at 12% width with 60% opacity → `placement=bottom_right, relative_scale=0.12, opacity=0.6, margin_bottom=20, margin_right=20`.

---

#### 🔥 Compose Burn Subtitle (`MF_ComposeBurnSubtitle`)

Burn SRT subtitles **inside the Compose pipeline** — stack `subtitle + watermark + audio mix` and pay only one encode. Same widget set as `MF_BurnSubtitle` (font dropdown, full ASS styling, alignment, margins, colors), minus the encoder controls (those live on `MF_ComposeVideo`).

**When to use**: any time you'd reach for `MF_BurnSubtitle` *and* you're already applying another Compose op. Single-encode for the whole chain.

| Widget | Default | Notes |
|---|---|---|
| `srt_path` | `input/sample.srt` | UTF-8 SRT file |
| `font` … `back_color_hex` | (see `MF_BurnSubtitle` table) | Identical widgets — `font_size`, `bold`, `italic`, `outline_*`, `border_style`, `back_color_hex` |
| `alignment` / `margin_v` / `margin_l` / `margin_r` | `bottom_center (2)` / `20` / `50` / `50` | Same as `MF_BurnSubtitle` |

**Optional**: `overlays` — upstream chain.

**Output**: `overlays` (`MF_COMPOSE_OPS`).

**Notes**: `MF_BurnSubtitle` (in the Subtitle category) stays for one-shot subtitle burning when you don't need other overlays.

---

#### Audio Ops Chain — why four small nodes instead of one bundled node?

Each audio operation is its own append-only node so the chain stays **composable** — the same design as the visual `MF_COMPOSE_OPS` chain (`OverlayText` / `Watermark` / `BurnSubtitle`). One op = one node.

- **Order matters.** `Volume → AudioMix → Fade` and `AudioMix → Volume → Fade` produce different output. Chain order = wiring order = ffmpeg filter order.
- **Same op, multiple times.** Apply `Fade` twice for an intro fade-in *and* an outro fade-out, or step `Volume` down in two stages. A single bundled node would need duplicate widget sets to express the same thing.
- **Clean schema per node.** `Volume` exposes one `scale` slider; `AudioMix` exposes a BGM path + `keep_source` + dual-input `AUDIO` pin. Folded into one node, every "just lower the volume" workflow would stare at unused BGM widgets.
- **Extensible.** Adding EQ / reverb / pitch-shift / ducking later = drop in a new node, zero schema change to the existing four.

Trade-off: a workflow that uses three audio ops needs three audio nodes wired in series. For the simplest "just tweak the volume" case this is one node — the cost only shows up when stacking multiple ops, and the chain order then becomes a feature, not friction.

---

#### 🔊 Compose Volume (`MF_ComposeVolume`)

Append a `volume=N` audio filter op.

**When to use**: dimming source audio before mixing in BGM, or boosting a quiet recording.

| Widget | Default | Notes |
|---|---|---|
| `scale` | `1.0` (0.0–2.0) | `0.0` mutes, `0.5` halves, `1.0` passthrough, `2.0` doubles (watch for clipping) |

**Optional**: `audio_ops` — upstream chain.

**Output**: `audio_ops` (`MF_COMPOSE_AUDIO_OPS`).

---

#### 🎵 Compose Audio Mix (`MF_ComposeAudioMix`) **(dual-input audio)**

Mix an external BGM track with source audio — or replace source entirely.

**When to use**: adding podcast / vlog background music; layering ambient sound under narration.

| Widget | Default | Notes |
|---|---|---|
| `audio_path` | `input/bgm.mp3` | BGM file path; hidden when `audio` AUDIO pin wired |
| `keep_source` | `True` | `True` = `amix` source + BGM; `False` = discard source, use BGM only |
| `bgm_volume` | `0.3` (0.0–2.0) | BGM-side volume *before* mix. `0.3` keeps voice dominant — podcast/vlog default |
| `duration` | `first` | `first` (= source length) / `longest` / `shortest` |

**Optional inputs**:

| Input | Notes |
|---|---|
| `audio` (AUDIO) | Wire from another node — materializes to a WAV under the plugin's `.mf_tmp/` dir. Not deleted right after encode (its path is part of this node's cached output and may be reused by a later `MF_ComposeVideo` re-run on cache-hit); swept automatically once it's older than ~24h. Overrides `audio_path` when wired |
| `audio_ops` | Upstream chain |

**Output**: `audio_ops` (`MF_COMPOSE_AUDIO_OPS`).

---

#### 🌅 Compose Audio Fade (`MF_ComposeAudioFade`)

Append an `afade` op (fade in / fade out).

**When to use**: smooth audio start/stop instead of hard cut — intro fade-in, outro fade-out.

| Widget | Default | Notes |
|---|---|---|
| `direction` | `in` | `in` = silent → full; `out` = full → silent |
| `start_sec` | `0.0` | Fade start time. For `out`, set to `video_duration - duration_sec` |
| `duration_sec` | `2.0` (0.1–60) | Fade length in seconds |
| `curve` | `tri` | `tri` (linear) / `qsin` (quarter sine — perceptually smoothest) / `esin` / `hsin` / `log` / `par` / `qua` / `cub` / `squ` / `cbr` |

**Optional**: `audio_ops` — upstream chain.

**Output**: `audio_ops` (`MF_COMPOSE_AUDIO_OPS`).

---

#### 📏 Compose Normalize (`MF_ComposeNormalize`)

EBU R128 / streaming-grade loudness normalization via `loudnorm` (single pass).

**When to use**: hitting a target loudness for a streaming platform (Spotify, YouTube, Apple Podcasts) before upload.

| Widget | Default | Notes |
|---|---|---|
| `target_i` | `-16.0` LUFS (-70 to -5) | Target integrated loudness. **-14** YouTube / TikTok / Spotify music · **-16** Apple Podcasts / Spotify spoken-word · **-23** EBU R128 broadcast |
| `target_tp` | `-1.0` dBTP (-9 to 0) | True-peak ceiling. `-1` dBTP avoids clipping on consumer playback chains |
| `target_lra` | `11.0` LU (1–50) | Loudness range; larger = more dynamics preserved |
| `linear` | `True` | `True` avoids dynamic-range compression. `False` forcefully flattens to target (strict LUFS compliance, sacrifices dynamics) |

**Optional**: `audio_ops` — upstream chain.

**Output**: `audio_ops` (`MF_COMPOSE_AUDIO_OPS`).

> **Single pass** is fine for streaming. Strict EBU R128 broadcast certification needs two-pass (measure → re-apply), which MediaForge doesn't currently provide.

---

### Migrating from Compose v1

If you have workflow JSONs containing `MF_ComposeStart` / `MF_ComposeFinalize`, ComfyUI will show "Missing nodes" warnings. To migrate:

1. Drop a new `MF_ComposeVideo`. Copy the old `ComposeStart`'s `video_path` / `target_*` settings + the old `ComposeFinalize`'s `codec` / `crf` / `preset` / `keep_audio` settings into it.
2. Delete the old `MF_ComposeStart` and `MF_ComposeFinalize` nodes.
3. Take the overlay chain's last output (was an `MF_COMPOSE` IR) and wire it into `MF_ComposeVideo`'s new `overlays` pin (the type is now `MF_COMPOSE_OPS` — same chain topology).
4. If you had `MF_BurnSubtitle` running separately, replace with `MF_ComposeBurnSubtitle` in the chain to fold it into the single encode.

### `MediaForge/AI` — latent tools and provider-agnostic AI services

Schema marked **experimental** — `AI_CONFIG` API may change inside Phase 5 until Whisper / Translate are validated end-to-end across all provider recipes. `🧬 Extend Latent` (last in this section) is a Timeline latent tool — it takes no `AI_CONFIG` and makes no network requests.

#### ⚙️ AI Config (`MF_AIConfig`)

Centralized provider configuration. Outputs an `AI_CONFIG` dict that all AI nodes consume — swap provider / model / endpoint in one place and the whole chain follows.

**When to use**: any AI workflow. Drop one `MF_AIConfig` per backend (e.g. one for ASR via Groq, one for translation via OpenAI) and fan out into the consumers.

| Widget | Default | Notes |
|---|---|---|
| `provider` | `openai_compatible` | `openai_compatible` (any `/v1/...` HTTP endpoint) / `faster_whisper_local` (in-process) |
| `base_url` | `https://api.openai.com/v1` | Trailing slash stripped automatically |
| `api_key` | `""` | **Recommended: `env:OPENAI_API_KEY`** — the `env:` prefix resolves the named environment variable at runtime, so the secret never enters the workflow JSON (a plaintext key serializes into every exported/shared workflow — the canvas mask cannot protect that path; missing variable → clear error). As an exfiltration guard, an `env:`-resolved key is only allowed toward allowlisted hosts (built-in: `api.openai.com` / `api.groq.com` / localhost family) — extend with the `MF_AI_KEY_ALLOWED_HOSTS` env var (comma-separated hostnames, global) or `MF_AI_KEY_ALLOWED_HOSTS_<VARNAME>` (per-variable), so a malicious shared workflow can't pair `env:SOME_SECRET` with an attacker endpoint. `env:`-resolved keys also require **HTTPS** toward any non-loopback host (plaintext HTTP would expose the Bearer header) — only `localhost` / `127.0.0.1` / `::1` may use `http://`; a private-LAN IP counts as remote, so an allowlisted LAN endpoint still needs HTTPS (or use a plaintext key, which skips both guards). Logged with first 4 chars + `***` mask; node canvas displays plaintext keys masked with `•` (`env:` references shown as-is; click to reveal/edit — see `web/ai_config_mask.js`) |
| `model` | `gpt-4o-mini` | Free-form string. Whisper auto-substitutes if not an STT id (e.g. `gpt-4o-mini` reused for translate; Whisper falls back to `whisper-1`) |
| `device` | `auto` | `cpu` / `cuda` / `auto` — only used by `faster_whisper_local` |

**Output**: `ai_config` (AI_CONFIG dict).

**See [AI Provider Recipes](#ai-provider-recipes)** for copy-paste-ready `provider` / `base_url` / `model` combos for OpenAI / Groq / Ollama / faster-whisper.

---

#### 🗣️ Whisper Transcribe (`MF_WhisperTranscribe`)

Audio file or in-memory `AUDIO` dict → SRT text (string output, not file). Backend is determined by `ai_config.provider`.

**When to use**: generating subtitles from raw recordings (interviews, lectures, podcasts). The SRT can be wired directly into `MF_TranslateSubtitle` and `MF_BurnSubtitle` for end-to-end auto-subtitling.

| Widget | Default | Notes |
|---|---|---|
| `ai_config` | (required AI_CONFIG) | Wire from `MF_AIConfig`. `provider` selects backend |
| `audio_path` | `input/sample.mp4` | Any media file with an audio stream. FFmpeg extracts mono 16 kHz WAV internally |
| `language` | `zh` | ISO 639-1 hint (`en`, `ja`, `zh`, `ko`, ...) — empty = auto-detect |

**Optional**: `audio` (AUDIO dict) — overrides `audio_path` when wired. Downsampled to 16 kHz mono on the client side for consistent results across backends.

**Output**: `srt_text` STRING — well-formed SRT (multi-block) ready to wire into `MF_TranslateSubtitle.srt_text` or `MF_BurnSubtitle.srt_text` directly (use `MF_ConvertChinese` with `filename_prefix` when you also want the SRT saved as a file).

**Backend semantics** (from `ai_config.provider`):
- `openai_compatible` — POSTs to `<base_url>/audio/transcriptions`. Works with OpenAI, Groq, any OpenAI-API-compatible server. Uses `requests` from `requirements.txt`.
- `faster_whisper_local` — lazy-imports `faster-whisper`. Honors `ai_config.device` (`cpu` / `cuda` / `auto`) and `ai_config.model` (`tiny` / `base` / `small` / `medium` / `large-v3`). First run downloads model to HF cache. Requires `pip install faster-whisper`.

**Model auto-substitution**: if `ai_config.model` doesn't look like an STT model id (e.g. it's `gpt-4o-mini` because the same `AI_CONFIG` feeds Translate), Whisper substitutes the provider's default — `whisper-1` for OpenAI-compatible, `base` for faster-whisper-local. Set an explicit STT model id (`whisper-large-v3`, `distil-large-v3`, …) to override.

**Errors raised early**: source missing audio stream → friendly error. WAV extraction yields < 256 bytes (silent / corrupt) → raise before sending to backend.

---

#### 🌐 Translate Subtitle (`MF_TranslateSubtitle`)

SRT text → translated SRT (timestamps preserved). Uses `/v1/chat/completions` with batched numbered prompts to keep line-count alignment.

**When to use**: localizing AI-generated subtitles into another language. Pair with `MF_ConvertChinese` if you need simplified↔traditional normalization after translation.

| Widget | Default | Notes |
|---|---|---|
| `ai_config` | (required AI_CONFIG) | Must be `provider=openai_compatible` (no local LLM mode — wire your local OpenAI-compatible server's URL instead) |
| `srt_text` | `""` (multiline) | Wire from `MF_WhisperTranscribe.srt_text` *or* paste manually |
| `target_lang` | `繁體中文` | Free-form — `English` / `日本語` / `한국어` / `Español` / ... |
| `system_prompt` | (preset, supports `{target_lang}` placeholder) | Edit to constrain tone (technical / colloquial / formal) |
| `batch_size` | `20` (1–200) | Lines per LLM call. Smaller = more reliable alignment but slower; larger = faster but small models may drift |

**Output**: `translated_srt` STRING.

**Behavior**: each batch is sent as numbered lines (`[1] ...`, `[2] ...`); the response is parsed by the same numbering pattern. If the count drifts (LLM merged / skipped a line) the node **raises** rather than producing misaligned subtitles — retry with smaller `batch_size` or stronger model.

**Recommended models**: `gpt-4o-mini` (fast + cheap, fine up to batch 30); `gpt-4o` / `llama-3.3-70b-versatile` (more reliable for long-form / specialized vocabulary, can handle batch 50+).

---

#### 🧬 Extend Latent (`MF_ExtendLatent`)

Copies the previous segment's latent tail into the start of this segment's empty latent and sets `noise_mask` so the sampler keeps (or partly re-generates) that region. Tensor work only — no model is loaded and nothing is sampled.

**When to use**: between your empty-latent node and your sampler, for every segment. The first segment (no incoming overlap) passes through without a carry.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `mask` | dropdown | `hard` | `hard` keeps every carried token; `taper` relaxes the tail of the carried region; `linear` relaxes progressively (the last carried token becomes fully denoisable, except when only 1 token is carried) |
| `carry_audio` | BOOLEAN | `False` | H3 audio carry only (`E_AUDIO_CARRY` on other latents); off leaves audio latents and masks unchanged |
| `video_taper_tokens` *(optional)* | INT | `2` (0–1000000, step 1) | Video tokens relaxed at the tail in `taper` mode; must not exceed the carried token count. Hidden when `mask = hard` or `linear` |
| `audio_taper_tokens` *(optional)* | INT | `2` (0–1000000, step 1) | Same for audio. Hidden when `mask = hard` or `linear`, and when `carry_audio` is off |
| `experimental_causal` *(optional)* | BOOLEAN | `False` | Enables carry on the causal grids (Wan / LTX); joins may be discontinuous. Off → a carry on those grids raises `E_CAUSAL` |
| `on_conflict` *(optional)* | dropdown | `error` | `error` stops when an existing mask conflicts with the carry region (`E_MASK_CONFLICT`); `carry_wins` overwrites samples and mask there |

**Sockets**:

| Input | Type | Notes |
|---|---|---|
| `target_latent` | LATENT | Required. This segment's empty latent, already at final size; its time length must equal the segment's token count (`E_TOKENS` otherwise) |
| `segment` | MF_SEGMENT | Required; from Select Segment |
| `state` *(optional)* | MF_SEGMENT_STATE | Previous Append `state` (loop: Start Loop `current_iteration_value`); either this or `previous_latent` |
| `previous_latent` *(optional)* | LATENT | Previous segment's sampled latent (linear chain); either this or `state` |

**Outputs**: `latent` LATENT (→ your sampler); `report` STRING (JSON: copied ranges, token counts, requested and effective mask values, conflicts, provenance, warnings).

**Notes**: the output latent carries an `mf_carry` marker that Append checks. Conditioning (prompts, keyframes, references) is not inspected — `report` always says `conditioning_check: "unavailable"`. An overlapping segment with neither `state` nor `previous_latent` raises `E_CARRY_SOURCE`.

**Example**: H3 loop — `mask = hard`, `carry_audio = True`, `state` ← Start Loop `current_iteration_value`. For Wan 2.2 high / low see [Two-stage samplers](#two-stage-samplers).

## AI Provider Recipes

`MF_AIConfig` outputs a dict consumed by all AI nodes. Same `provider` / `base_url` / `api_key` / `model` interface — only the values change. Copy-paste-ready combos:

### OpenAI (official)

```
provider   = openai_compatible
base_url   = https://api.openai.com/v1
api_key    = sk-...
model      = whisper-1         # for MF_WhisperTranscribe
           = gpt-4o-mini       # for MF_TranslateSubtitle
```

Paid; most reliable. `whisper-1` is the multilingual GA endpoint.

### Groq (fastest hosted Whisper)

```
provider   = openai_compatible # OpenAI-compatible API surface
base_url   = https://api.groq.com/openai/v1
api_key    = gsk_...
model      = whisper-large-v3  # ASR — ~5–10× faster than OpenAI whisper-1 for similar quality
           = llama-3.3-70b-versatile  # translate
```

Free tier with rate limits; useful when you need to subtitle a 1-hour podcast in under a minute.

### faster-whisper (local, no API key)

```
provider   = faster_whisper_local      # on MF_WhisperTranscribe
device     = cuda                      # or "cpu", "auto"
model      = large-v3                  # downloaded to HF cache on first use
```

`pip install faster-whisper` first. Best for privacy / offline workflows. CPU works but slow (real-time × ~0.3 on a modern laptop); CUDA recommended for non-trivial files.

### Ollama / LM Studio (local OpenAI-compatible)

```
provider   = openai_compatible
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
- **MF_COMPOSE_OPS**: `list[dict]` — plain op-spec dicts (`{"type": "drawtext" | "overlay" | "watermark" | "subtitle", "params": {...}, "image_path"?: ...}`) appended by `MF_ComposeOverlayText` / `MF_ComposeOverlayImage` / `MF_ComposeWatermark` / `MF_ComposeBurnSubtitle`. Compose v2's video-chain wire type.
- **MF_COMPOSE_AUDIO_OPS**: `list[dict]` — same idea for the audio chain (`{"type": "volume" | "amix" | "afade" | "loudnorm", "params": {...}}`), appended by `MF_ComposeVolume` / `MF_ComposeAudioMix` / `MF_ComposeAudioFade` / `MF_ComposeNormalize`. `MF_ComposeVideo` resolves both chains into the internal `ComposeIR` dataclass (`utils/compose_ir.py`) via `utils/compose_ops.py` dispatch at compile time — the IR itself is no longer a cross-node wire type (that was Compose v1's `MF_COMPOSE`, retired with `ComposeStart`/`ComposeFinalize`).
- **AI_CONFIG**: `dict` with keys `provider / base_url / api_key / model / device` (`api_key` arrives resolved — `env:` indirection is expanded by `MF_AIConfig` before the dict leaves the node). Experimental.
- **MF_SEGMENT_PLAN**: versioned, immutable plan dict from `MF_EditTimeline` — `plan_id` / `execution_id`, mode, fps, grid, source reference, segments, `total_frames`. No absolute paths.
- **MF_SEGMENT**: versioned, immutable per-segment spec from `MF_SelectSegment` — index / count, start / end / length, overlaps, carry sizes, seams, source window, prompt. Shared by Load Segment Frames, Extend Latent and Append Segment.
- **MF_SEGMENT_STATE**: immutable dict; every `MF_AppendSegment` returns a new one (session, spill folder, committed frames / samples, pending crossfade tail, carried latent tail). It owns the spill parts in-process, so it can't be serialized into a workflow.
- **LATENT `mf_carry`**: `MF_ExtendLatent` adds `{execution_id, segment_index, carry_spec_id, streams_carried, carry_dtypes}` to its output LATENT dict; core samplers keep it. Append reads it as a consistency hint (consistent / absent / mismatch), not as proof of origin, and uses `carry_dtypes` (the dtype the carry was written in, e.g. `bfloat16`) as the baseline of its `E_CARRY_DRIFT` check.

## Architecture

```
comfyui_MediaForge/
├── __init__.py              # pkgutil auto-discover nodes/ — drop a file in, it shows up
├── pyproject.toml
├── requirements.txt         # ffmpeg-binary fallbacks + small pure-Python deps; heavy optional deps stay lazy-imported
├── nodes/                   # one file per node, MF_<Verb><Noun>
│   ├── ai_config.py            # MF_AIConfig
│   ├── append_segment.py       # MF_AppendSegment  — Timeline: decode, de-overlap, spill to .mf_tmp/
│   ├── assemble_segments.py    # MF_AssembleSegments  — Timeline: final encode (OUTPUT_NODE)
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
│   ├── detect_scenes.py        # MF_DetectScenes  — MediaForge/Analysis
│   ├── detect_silence.py       # MF_DetectSilence
│   ├── edit_timeline.py        # MF_EditTimeline  — Timeline plan (+ web/timeline_editor.js)
│   ├── extend_latent.py        # MF_ExtendLatent  — Timeline latent tail-carry (MediaForge/AI)
│   ├── extract_audio.py        # MF_ExtractAudio  — MediaForge/Audio (Phase 6 first node)
│   ├── load_segment_frames.py  # MF_LoadSegmentFrames  — Timeline: source frames / audio per segment
│   ├── load_video_frames.py    # MF_LoadVideoFrames
│   ├── loop_video.py           # MF_LoopVideo
│   ├── probe_media.py          # MF_ProbeMedia
│   ├── save_video_frames.py    # MF_SaveVideoFrames
│   ├── select_segment.py       # MF_SelectSegment  — Timeline: one segment spec per iteration
│   ├── select_video.py         # MF_SelectVideo  — dropdown picker for input/ videos
│   ├── translate_subtitle.py   # MF_TranslateSubtitle
│   ├── trim_by_ranges.py       # MF_TrimByRanges
│   └── whisper_transcribe.py   # MF_WhisperTranscribe
├── utils/
│   ├── ass_style.py         # ASS subtitle style helpers (shared by BurnSubtitle + ComposeBurnSubtitle)
│   ├── audio_mix.py         # amix / afade / volume / loudnorm filter builders
│   ├── cache_key.py         # path_fingerprint: mtime / size fingerprint for IS_CHANGED
│   ├── color.py             # hex_to_ass_color: #RRGGBB → ASS BGR+alpha
│   ├── compose_ir.py        # ComposeIR + AudioOp + compile_ir + compile_audio_chain
│   ├── compose_ops.py       # MF_COMPOSE_OPS / AUDIO_OPS dispatch + watermark resolver
│   ├── encoder.py           # codec catalog + NVENC probe + pick_default_codec + build_encoder_args
│   ├── ffmpeg.py            # ensure_ffmpeg / run_ffmpeg / probe / probe_has_audio_stream
│   ├── latent_carry.py      # Timeline: latent stream split, tail extract, carry masks, provenance
│   ├── output_path.py       # resolve_output_path + output_path_to_ui_entry (API metadata helper)
│   ├── plugin_tmp.py        # .mf_tmp/ temp files + sweep of stale files and session dirs
│   ├── segment_spill.py     # Timeline: immutable state, FFV1 / WAV spill parts, crossfade, final encode
│   ├── timeline_messages.py # Timeline: message catalog (stable E_* / W_* keys)
│   ├── timeline_plan.py     # Timeline: time base, grids, timeline_data validation, segment specs
│   ├── timeline_routes.py   # Timeline: /mediaforge/timeline/probe + /proxy routes for the editor
│   ├── timeline_source.py   # Timeline: input/ source resolution, safe FFmpeg input flags, fingerprint
│   └── video_io.py          # rawvideo pipe ↔ IMAGE/AUDIO + encode_tensor_to_tempfile
├── font/                    # drop .ttf / .otf here for MF_BurnSubtitle font_file dropdown
├── example_workflows/       # Timeline example workflows (shown in ComfyUI's template browser)
├── web/
│   ├── ai_config_mask.js    # frontend extension: mask MF_AIConfig api_key on the canvas
│   ├── dual_input_lock.js   # frontend extension: hide widgets per dual-input mode
│   │                        #   - lock_widget / hidden_when_connected: hide on tensor mode
│   │                        #   - linked_widgets: hide on path mode
│   ├── timeline_editor.js   # Edit Timeline visual editor (DOM widget)
│   ├── timeline_grid.js     # grid / validation rules mirrored from utils/timeline_plan.py
│   └── timeline_strings.js  # editor UI text (Traditional Chinese)
└── tests/                   # dev-only pytest suite — gitignored, not in the published repo (see Testing)
    ├── test_compose_ir.py        # IR spike cases (Phase 4 prerequisite)
    ├── test_compose_e2e.py       # real-ffmpeg e2e
    ├── test_video_io_roundtrip.py# PSNR > 38 dB rawvideo roundtrip
    ├── test_timeline_*.py        # Timeline plan / nodes / routes / real-core cache (T09)
    ├── js/timeline_grid.test.mjs # grid rules vs. shared golden vectors (node --test)
    └── test_codex_r*_fixes.py    # regression tests across Codex review rounds
```

**Add a new node:** drop `nodes/<verb>_<noun>.py` with `MF_<Verb><Noun>` class + `NODE_CLASS_MAPPINGS` / `NODE_DISPLAY_NAME_MAPPINGS`. The aggregator picks it up — restart ComfyUI.

> ⚠️ **Import failures:** `__init__.py` imports each node module separately — a module that fails to import is skipped with `[MediaForge] 節點模組 <name> 載入失敗：<error>` in the console, and the other nodes still register. For the full traceback run `python -c "from custom_nodes.comfyui_MediaForge.nodes.<your_file> import *"`. Optional deps (`requests`, `faster-whisper`, `yt-dlp`) **must** be lazy-imported inside the FUNCTION method, never at module top.

## Requirements

- ComfyUI
- Python ≥ 3.10
- **FFmpeg + FFprobe** — a system install in PATH is preferred; without one, `imageio-ffmpeg` (ffmpeg only) and `static-ffmpeg` (ffmpeg + ffprobe, downloaded on first use) from `requirements.txt` take over
- Python packages in `requirements.txt` — ComfyUI Manager installs them; after a manual clone run `pip install -r requirements.txt`
- Optional: `faster-whisper` (local Whisper backend) — lazy-imported on first use
- Timeline nodes: no extra pip packages (`psutil`, `torch`, `numpy` come with ComfyUI); tested with ComfyUI core 0.42.5 / frontend 1.55.18 — not a minimum version (see [Compatibility](#compatibility))

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
pip install -r comfyui_MediaForge/requirements.txt
```

Restart ComfyUI — nodes appear under `MediaForge/Subtitle | Video | Analysis | Audio | Compose | AI`.

### Optional dependencies (lazy — install only what you actually use)

```bash
pip install faster-whisper    # only for backend="faster_whisper_local"
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Nodes don't appear in menu after restart | Module-level import error — the console shows `[MediaForge] 節點模組 <name> 載入失敗：…` and the other nodes still load | `python -c "from custom_nodes.comfyui_MediaForge.nodes.<file> import *"` to surface the full traceback |
| `RuntimeError: ffmpeg ... failed` | FFmpeg returned non-zero exit | Scroll up — the last 30 lines of FFmpeg stderr are printed before the raise |
| FFmpeg complains about filter syntax with `:` in path (Windows) | Path leaked into filter graph unescaped | We auto-handle this via `escape_filter_path()` — only an issue if you wrote a custom node bypassing it |
| Filename with `[ ] ' , ;` or other filter-special characters breaks a subtitle/drawtext/overlay node | Same root cause as the `:` case — those characters are also filtergraph syntax | Also auto-handled: `escape_filter_path()` now does the full two-level FFmpeg escape (`: ' \ [ ] , ;`), not just colon. Only affects filter-graph paths (`subtitles=`, `textfile=`, etc.) — plain `-i` arguments never needed escaping |
| `MF_ConcatVideos` `mode=copy` raises listing per-file codec/dims/pix_fmt/audio differences | Preflight probe found the inputs aren't stream-copy compatible | Expected — concat demuxer + `-c copy` on mismatched inputs used to exit 0 but emit a corrupt file after the first segment. Switch to `mode=transcode` (always works, always re-encodes) |
| Whisper local backend extremely slow | CTranslate2 CPU inference | Set `device = "cuda"` in `MF_AIConfig`; or use a hosted backend (Groq is much faster than local CPU) |
| `loop` filter errors on very long source | Buffered-frame limit (`MAX_LOOP_FRAMES = 32767`, INT16) | Reduce fps or use `crossfade` mode (xfade chain, no single buffer) |
| Translate output line count drifts | Small LLM lost track of numbered batches | Switch to a stronger model (`gpt-4o`, `llama-3.3-70b-versatile`); the prompt does line-number alignment but small models miss it on > 50-line batches |
| `reverse` filter OOM on long source | `reverse` loads the entire stream into RAM | Currently a hard limit; cut to a sub-clip first or split-and-stitch manually |

### Timeline messages

Timeline nodes report problems as `[<node>] <KEY>：<message>`. The message text is Traditional Chinese, so search by key; the table gives English translations.

| Key | Node | Message (English translation) | Fix |
|---|---|---|---|
| `E_TIMELINE_JSON` | Edit Timeline | Invalid timeline JSON: {reason}. Fix {location}. | Correct the named field (format: [Timeline prompts](#timeline-prompts)); the editor marks the same error on the segment |
| `E_H3_FPS` | Edit Timeline | The H3 grid supports only 24 fps. Set fps to 24. | Set `fps = 24`, or pick another `grid` |
| `W_SOURCE_TIME_UNKNOWN` | Edit Timeline | Can't read the source's start time; using 0. Preview and source times may be slightly offset. | Warning only; the run continues |
| `E_SEGMENT_INDEX` | Select Segment | segment_index out of range: the plan has {count} segments (0-based), got {index}. | Wire Start Loop `max_iteration` to Edit Timeline `segment_count` |
| `E_SOURCE_CHANGED` | Load Segment Frames | The source video changed; rerun Edit Timeline. | Queue again so Edit Timeline re-reads the file |
| `E_SOURCE_SHORTFALL` | Load Segment Frames | The source video does not cover segment {segment}. Use hold_last / black or adjust the time window. | Set `shortfall = hold_last` / `black`, or move the segment inside the source |
| `E_TOKENS` | Extend Latent | Latent time length mismatch: expected {expected}, got {actual}. Check the grid, VACE reference and guide wiring. | Build the empty latent from Select Segment `length` with the model's `grid`; `target_latent` must not contain VACE reference or guide tokens |
| `E_AUDIO_CARRY` | Extend Latent | This latent does not support audio carry. Turn off carry_audio. | Turn `carry_audio` off (H3 only); for LTX-2, decode the audio into Append `audio` |
| `E_CARRY_SOURCE` | Extend Latent | This segment needs carry data. Connect state or previous_latent (one of them). | Loop: Start Loop `current_iteration_value` → `state`; linear chain: previous sampled latent → `previous_latent` |
| `E_CAUSAL` | Extend Latent | This carry mode is still experimental. Turn on experimental_causal to confirm. | Turn on `experimental_causal` (causal grids: Wan / LTX) |
| `E_MASK_CONFLICT` | Extend Latent | The carry region conflicts with an existing mask. Adjust the wiring or choose carry_wins. | Set `on_conflict = carry_wins`, or remove the other mask |
| `E_PROVENANCE` | Extend Latent / Append Segment | Carry metadata does not match this segment ({field}). Check the segment wiring. | All nodes of one iteration must use the same Select Segment output; the latent must come from that segment's Extend → sampler |
| `W_PROVENANCE` | Extend Latent / Append Segment | Carry metadata is absent; continuing. Check the segment and guide wiring. | Make sure the latent reaching Append (or `previous_latent`) went through Extend Latent |
| `E_SEQUENCE` | Append Segment / Assemble Segments | Expected segment {expected}, got segment {actual}. Connect the previous segment's state. | First segment: leave `state` empty; later segments: the previous Append `state`; Assemble needs the state after the last segment |
| `E_PLAN` | Append Segment | Segment and state belong to different plans. Rerun the whole loop. | Rerun the whole loop after changing the timeline or the source |
| `E_CARRY_DRIFT` | Append Segment | The carried region changed after sampling (max diff {drift}). For two-stage sampling (e.g. Wan 2.2 high / low), add another Extend Latent before the second sampler with on_conflict set to carry_wins. | See [Two-stage samplers](#two-stage-samplers) |
| `E_RAM` | Append Segment | This segment needs an estimated {gib} GiB of RAM, above the preflight threshold. Shorten the segments or lower the resolution. | As stated; the threshold is 80% of available RAM |
| `E_DISK` | Append Segment | Not enough temporary space; at least {gib} GiB is needed. Free up space and retry. | Free space on the drive that holds the plugin's `.mf_tmp/` |
| `E_AUDIO_LAYOUT` | Append Segment | All segments must agree on audio presence, sample rate and channels. Unify the audio format upstream. | Give every segment audio from the same source / VAE, or none |
| `W_AUDIO_PAD` | Append Segment | Inserted {samples} samples of silence at a seam or the end. | Informational (H3 runs pad the final tail, e.g. 267 samples) |
| `E_SPILL_MISSING` | Assemble Segments | Segment spill files no longer exist. Rerun the whole loop, then assemble. | The parts were deleted (e.g. `.mf_tmp/` cleaned up, or a cache eviction); rerun the loop |
| `W_PROXY` | timeline editor | Preview creation failed; the timeline is still editable. Retry. | The editor shows 「預覽失敗（時間軸仍可編輯）」 with a 重試 (Retry) button; editing works without the preview |
| — | core Start / End Loop | `Loop body is not closed` | A preview / side-effect node inside the loop body isn't wired into End Loop `terminations` — wire it there, or move it out of the loop |

## FAQ

**Q: Why not use VideoHelperSuite for everything?**
A: VHS is excellent for IMAGE-batch workflows. MediaForge handles **file-level** operations (cross-codec concat, audio-aware trim, broadcast encoders) and **single-encode multi-overlay** Compose. Install both.

**Q: Will you add diffusion / generation nodes?**
A: No model loaders, samplers or model patches — by design; generation stays in ComfyUI core / dedicated plugins. MediaForge ships post-production AI (Whisper transcribe, LLM translate) and the Timeline latent tools: Extend Latent reshapes the `LATENT` you pass in, Append decodes it with the `VAE` you connect, and FFmpeg does the encoding. You bring your own model workflow (see [Timeline & Long Video](#timeline--long-video)).

**Q: Why are AI nodes "experimental"?**
A: The `AI_CONFIG` schema may evolve while Phase 5 is in shakedown. Once Whisper + Translate are validated end-to-end across all 4 provider recipes, the schema freezes.

**Q: Can I use my own FFmpeg build (e.g. with extra codecs)?**
A: Yes — we call `ffmpeg` / `ffprobe` from `PATH`. Put your preferred build earlier on `PATH` to override the system one.

**Q: Does this run on Apple Silicon?**
A: Yes — FFmpeg + `faster-whisper` both work on M1/M2/M3. Metal isn't supported by faster-whisper directly, but CPU is workable thanks to CTranslate2 INT8 quantization. For hosted ASR, Groq's recipe runs unchanged.

**Q: How do I share a workflow with someone who doesn't have MediaForge?**
A: They'll see "Missing nodes" warnings. Direct them to ComfyUI Manager → "Install Missing Custom Nodes". Standard ComfyUI plugin behavior.

**Q: What's in the standalone Audio domain?**
A: Phase 4.5 shipped **Compose-chain** audio ops (Volume / AudioMix / Fade / Normalize — folded into the single-encode pipeline alongside video overlays). Phase 6 adds standalone file-level Audio nodes on top of that: `MF_ExtractAudio` (pull/materialize a track to its own file) ships first; denoise, normalize-file, cut/trim, and ducking are still planned.

## Testing

`tests/` is a developer-local pytest suite (IR compilation, real-ffmpeg roundtrips, and regression tests accumulated across review rounds) kept out of the published repo and package — it's excluded via `.gitignore`, so a fresh `git clone` or ComfyUI Manager install won't include it (expected, not a broken install). Contributors working from a checkout that still has the directory can run:

```bash
cd ComfyUI/custom_nodes/comfyui_MediaForge
pip install pytest
python -m pytest tests/                      # full suite
python -m pytest tests/test_compose_ir.py    # only IR spike
python -m pytest tests/ -k "video_io"        # only rawvideo roundtrip
```

Real-ffmpeg tests require `ffmpeg` on PATH.

Timeline tests (`test_timeline_*.py`, `test_latent_carry.py`, `test_segment_spill.py`, `test_load_segment_frames.py`) are developer-local like the rest of `tests/`. The real-core cache test `tests/test_timeline_core_cache.py` must run inside the ComfyUI Linux / WSL environment, because it spawns `main.py --cpu` instances with ComfyUI's interpreter. The JS grid tests need Node.js:

```bash
python -m pytest -q tests/test_timeline_core_cache.py   # inside ComfyUI's Linux / WSL environment
node --test tests/js/timeline_grid.test.mjs
```

## Roadmap

- **Phase 2** ✅ Foundation bridges — LoadVideoFrames / SaveVideoFrames
- **Phase 3** ✅ Gap-priority — DetectSilence / TrimByRanges / ConcatVideos
- **Phase 4** ✅ Compose pipeline v1 — single-encode multi-overlay (Start + Finalize)
- **Phase 4.5** ✅ Compose pipeline v2 — merged ComposeVideo + subtitle-in-chain + audio chain (Volume / AudioMix / Fade / Normalize)
- **Phase 5** 🚧 AI — WhisperTranscribe / TranslateSubtitle (experimental schema)
- **Phase 6** 🚧 Standalone Audio domain — `MF_ExtractAudio` ships first; file-level denoise, normalize-file, audio cut/trim, ducking still planned (the Compose chain audio ops in Phase 4.5 are a subset)
- **Timeline** ✅ Timeline & long video — Edit Timeline editor, segment loop, latent tail-carry, spill-to-disk assembly (MiniMax H3 AV supported; LTX-2 / Wan 2.2 experimental)
- **Phase 7** ⏳ Net domain — yt-dlp ingest, HTTP fetch (lazy-import)

## License

MIT — © YingLiang Lu (leon80148).

## Acknowledgments

- **FFmpeg** — the actual workhorse; this plugin is 90% argument formatting
- **ComfyUI** — the runtime + node graph engine
- **VideoHelperSuite** — the prior art that defined the IMAGE-batch contracts MediaForge interoperates with
- **faster-whisper / CTranslate2** — local Whisper backend
- Timeline planning and latent tail-carry concepts were inspired by [ComfyUI MiniMax H3 Timeline Director](https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector); MediaForge implements these features independently, reuses no code from that project, and remains MIT-licensed.
