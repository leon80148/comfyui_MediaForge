# MediaForge

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg) ![License MIT](https://img.shields.io/badge/license-MIT-green.svg) ![ComfyUI](https://img.shields.io/badge/ComfyUI-custom__nodes-orange.svg)

> **Power-user FFmpeg toolkit for ComfyUI.** Tensor↔media bridge with broadcast-grade codec control. Depth > breadth.

FFmpeg-driven custom_nodes plugin: subtitle burn-in, looped video, audio probe, tensor-native frame I/O, silence detection, multi-clip concat, multi-overlay Compose pipeline, provider-agnostic AI subtitle/translate, and a timeline for long videos generated in overlapping segments. Media nodes are thin FFmpeg wrappers. The Timeline nodes load and patch no models — they reshape the `LATENT` you pass in and decode it with the `VAE` you connect; for MiniMax H3 they build conditioning and sample only by running ComfyUI core node classes, and FFmpeg encodes. AI nodes use a `MF_AIConfig` connection so you can swap providers in one place.

📖 **[繁體中文 / Traditional Chinese →](README_ZHTW.md)**

## Highlights

- 🎞️ **30 nodes** across 6 categories — Subtitle / Video / Analysis / Audio (Phase 6 kickoff) / Compose (now with audio chain) / AI (+ Net / Image planned). Three more are registered but internal: Sample Segments builds them when it runs
- 🔗 **Dual-input bridge** — file-consumer nodes accept *either* a `video_path` string *or* an in-memory `IMAGE + AUDIO + tensor_fps` triplet, so MediaForge chains with VHS / AnimateDiff / any IMAGE-pipeline plugin without a SaveVideoFrames round-trip
- 🚀 **Smart GPU codec default** — `h264_nvenc` is auto-selected when NVENC is available, libx264 fallback on CPU-only systems. No per-workflow switching needed
- 📡 **API-ready output** — every file-producing node emits ComfyUI `ui.images` metadata so `/history/<prompt_id>` exposes the output filename; download via `/view?filename=X&subfolder=Y&type=output` (see [Using via API](#using-via-api))
- 🧪 **Broadcast-grade codec control** — H.264 / HEVC / AV1 / ProRes with CRF / bitrate / target-size encode modes; NVENC variants (`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`) auto-detected via `ffmpeg -encoders` probe
- 🎚️ **Single-encode multi-overlay Compose pipeline** — `filter_complex` graph compiler; lossless overlay stacking vs. N re-encodes
- 🎙️ **Audio mixing built-in** — BurnSubtitle's `keep_source_audio` toggle `amix`-es an external audio pin with the source's native audio (default on); cinematic fps rates (23.976 / 29.97 / 59.94) accepted as FLOAT
- 🤖 **Provider-agnostic AI** — `MF_AIConfig` lets you point Whisper / Translate at OpenAI / Groq / OpenRouter / Gemini / Ollama / local backends in one place
- 🎬 **Video Batch** — run a ten-minute video through any IMAGE node (upscaler, masking, H3) in batches, with two nodes and one queue entry. Frames stay on disk, not in RAM, and one finished video comes out. See [Video Batch](#video-batch)
- 🧵 **Timeline & long video** — plan overlapping segments in a visual editor with a material library (reference images / videos / audio, keyframes, guides, audio locks); one sampler node generates them one at a time, carrying each segment's latent tail into the next and spilling each decoded segment to disk, and one node encodes once. MiniMax H3 AV only. The same editor also marks the ranges to cut out of an existing video (see [Timeline & Long Video](#timeline--long-video))
- 🪶 **Light dependencies** — `requirements.txt` lists four small pure-Python packages plus `imageio-ffmpeg` / `static-ffmpeg`, the fallbacks used when no system `ffmpeg` / `ffprobe` is found. All are imported only when needed; `faster-whisper` stays optional
- 🔁 **Tensor-native** — `IMAGE [B,H,W,C] float32` and ComfyUI-canonical `AUDIO` dict; rawvideo roundtrip at PSNR > 38 dB

## Table of Contents

1. [Quick Start](#quick-start)
2. [Video Batch](#video-batch)
3. [Timeline & Long Video](#timeline--long-video)
4. [Using via API](#using-via-api)
5. [Smart GPU codec default](#smart-gpu-codec-default)
6. [Why this plugin (vs VideoHelperSuite)](#why-this-plugin-vs-videohelpersuite)
7. [Nodes (30)](#nodes-30)
8. [AI Provider Recipes](#ai-provider-recipes)
9. [Hidden Contracts](#hidden-contracts)
10. [Architecture](#architecture)
11. [Requirements](#requirements)
12. [Install](#install)
13. [Troubleshooting](#troubleshooting)
14. [FAQ](#faq)
15. [Testing](#testing)
16. [Roadmap](#roadmap)
17. [License & Acknowledgments](#license)

## Quick Start

Four minimal workflows in increasing complexity. Each is described as a node chain — drop them into ComfyUI and wire as shown.

### 1. Auto-cut silence (4 nodes — simplest)

Strip dead air from a lecture recording or podcast.

```
[LoadVideoFrames]──video_path──▶[DetectSilence]──SILENCE_RANGES──▶[TrimByRanges]──▶[PreviewAny]
                                                                       ▲
                                                                       │ mode=remove
```

| Node | Key inputs |
|---|---|
| `MF_LoadVideoFrames` | `video_path = "lecture.mp4"` |
| `MF_DetectSilence` | `noise_db = -30`, `min_duration_sec = 1.5` |
| `MF_TrimByRanges` | `mode = "remove"` |
| `PreviewAny` | Wire Trim by Ranges' `final_video_path` into `source`; shows the result path after Queue |

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
| `MF_AIConfig` (LLM) | `provider = "openai_compatible"`, `base_url = "https://api.openai.com/v1"`, `model = "gpt-6-luna"` |
| `MF_TranslateSubtitle` | `target_lang = "繁體中文"` |
| `MF_BurnSubtitle` | wire translated SRT into `srt_text`, set ASS font/color/outline |

See [AI Provider Recipes](#ai-provider-recipes) below for copy-paste `base_url` / `model` combos.

### 4. H3 long video (3 MediaForge nodes)

Generate a long AV clip as overlapping 107-frame segments and encode it once. Open `example_workflows/timeline_h3_basic.json` (ComfyUI lists it in the template browser) and pick your own model files, or wire it by hand:

1. `⏱️ Edit Timeline` — `fps = 24`, `mode = generate`; three segments `[0,107)`, `[85,192)`, `[153,260)`, one prompt each.
2. `🎛️ Sample Segments` — `segment_plan` ← Edit Timeline; `model`, `clip`, `vae`, `audio_vae`, `sampler` and `sigmas` from your own core loader / sampler nodes; `mask = hard`, `carry_audio = True`.
3. `🧵 Assemble Segments` — `state` ← Sample Segments → one `.mp4` (260 frames, 32 kHz audio).

The editor, materials and limits: [Timeline & Long Video](#timeline--long-video). With reference images, a guide clip and locked original audio: `example_workflows/timeline_h3_director.json`. To cut an existing video instead of generating one: `example_workflows/timeline_cut.json`.

## Video Batch

> Run any IMAGE node over a long video without holding the video in RAM. Frames go through your nodes one batch at a time, every batch is written to disk, and one finished video comes out. There is no loop to wire and no frame count to work out.

```
🎞️ Start Video Batch ── frames ──► your nodes ── frames ──► 🎬 End Video Batch
```

**To try it:** add the two nodes, connect them through your node (an upscaler, say), pick a video on Start and press Run. `frames_per_batch` on End is how many frames go through your nodes at a time, and it sets the memory peak. Lower it if you run out of RAM or VRAM.

**What happens when you press Run.** It stays one queue entry and one history entry.

1. The whole job is checked first: the sources, the wiring and the audio. A problem here stops it before your nodes get any frames.
2. End asks for a batch. Start decodes only that window with FFmpeg and your nodes run on it. The same nodes run again for every batch. With ComfyUI's usual caching a node stays the same instance, so a model it keeps loaded is loaded once. `--cache-none` rebuilds it.
3. End writes the frames to `.mf_tmp/seg_*` as lossless FFV1, drops them from memory and asks for the next batch. After the first batch it also tries a tiny encode, so an odd output size or an unavailable encoder fails at once instead of after the whole video.
4. When the source ends, End encodes the parts into one file, checks it, shows it on the node and deletes the parts.

The End node shows `第 k / 約 N 批` (batch k of about N), then `正在組裝成片…` (assembling), then `完成` (done). The progress bar only fills once the finished file has passed its checks. Under the status, End lists each source with its fps, length and whether the film keeps its soundtrack, and anything it converted, such as `bg.mp4：24 → 25 fps`. In the classic renderer End shows about two lines; make the node taller to see them all. The server log has the full summary too.

**Video Batch or Timeline?**

| | Video Batch | [Timeline](#timeline--long-video) |
|---|---|---|
| For | Processing an existing video: upscaling, masks, restyling | Generating new video in planned segments that continue from each other |
| Length | The source's frames | What you plan in the editor |
| Wiring | 2 nodes plus your own | 6–8 nodes, or Sample Segments for H3 |

**Batches.** The `frames` output always has exactly `frames_per_batch` frames: a short last batch repeats its last frame, and End cuts the result back, so models that need a fixed length work. Your nodes must return as many frames as they get, so anything that changes the frame count, like frame interpolation, goes after End on `final_video_path`. Nothing carries latents or temporal context from one batch to the next. A temporal model can show a join between them; `overlap_frames` crossfades the join, but it only blends the results and can't give the model more context. The result isn't guaranteed to match running the whole video in one go.

**MiniMax H3.** `frames_per_batch` must be 17n+5, for example 124, with `fps = 24`. Other values are refused before the model runs. In a 150-batch test (12.9 minutes) the batches got slower as the run went on, from about 141 s to 183 s each, so it took about 16% longer than its first batches suggest. The cause hasn't been found yet.

**Audio.** `audio` on End and `keep_source_audio` decide the soundtrack:

| `audio` on End | Sources | Soundtrack of the video |
|---|---|---|
| Not linked, `keep_source_audio` off | Any | None |
| Not linked | No source has audio | None |
| Not linked | One source has audio | That source's audio, starting at its first video frame |
| Not linked | Two sources have audio | Error before the model runs: link `audio` or turn off `keep_source_audio` |
| Linked to a node outside the batch (Load Audio) | Any | That audio, read once, from the start of the video. Padded with silence or cut to the video's length |
| Linked to a node inside the batch (Start's `audio`, VAE Decode Audio) | Any | Each batch's audio, read batch by batch |

Start's `audio` output is only decoded if it's linked, even with `keep_source_audio` off. Linking it on a source without audio is an error. Overlaps crossfade audio linearly, so the same source audio doesn't get louder at a join.

**Two or more videos.** Use one Start per video and link them all into your nodes; they all feed the same End. The shortest decides the length. Batch settings live on End, so they can't disagree. If their fps differ and End's `fps` is 0, End uses the fps of the video whose soundtrack the film keeps, or the highest when none is kept, converts the others and says so on the node. A video under 5 fps stops the job instead, because that is nearly always a wrong fps upstream, such as Loop Video's `tensor_fps`; set `fps` on End if you really mean it.

**Disk and memory.** Parts take about 0.5 MB per frame at 1344×768, so ten minutes at 1080p 30 fps is very roughly 20 GB until the final encode. End updates its estimate after every batch and stops with an error if the temp or the output disk looks too small. Before decoding, Start checks RAM for one batch; End checks again after the first batch with the real output size. These are estimates, not guarantees: your nodes' own memory use is theirs.

**Stop.** After Stop no new batch starts. The node that's running stops when it next checks for Stop, and FFmpeg is stopped with it; a source that has stalled can delay this. You don't get a partial video. The parts are deleted when the next Video Batch job starts. After a crash, the startup sweep deletes them once the process that wrote them is gone and they are older than 24 hours.

**Compared with VHS Meta Batch Manager.** Measured on ComfyUI 0.42.5 and VHS commit `4d907be`, with an 11.6-minute 720p clip, 64 frames per batch, x264 CRF 19 and preset medium on both sides. Video Batch kept its parts on the Linux disk.

| | VHS Meta Batch Manager | Start / End Video Batch |
|---|---|---|
| Nodes | Batch Manager, linked to Load Video and Video Combine | Start and End, in place of Load and Combine |
| Queue and history | One entry per batch (261 here) | One |
| Soundtrack | A second file, `-audio.mp4` | In the one output file |
| After Stop | FFmpeg keeps running and the file won't open until the next Run closes it. Then you have the frames written so far, without audio | No new batch starts and FFmpeg is stopped. No partial video |
| Overlap between batches | No | `overlap_frames` |
| Extra disk | None | About 4 GB of parts until the final encode |
| Time, clip with audio, no model | 1191 s | 281 s |
| Time, no audio, no model (VHS: track removed from the file. Video Batch: `keep_source_audio` off) | 236 s | 286 s (earlier build) |
| Time, FlashVSR, 50 batches of 16 frames at 480×270 | 940 s | 1075 s |
| Peak RAM, ComfyUI and FFmpeg | 2.43 + 0.56 GiB | 2.35 + 0.60 GiB |
| Peak RAM with FlashVSR, ComfyUI | 11.54 GiB | 11.30 GiB |

Peak RAM was close in these runs. Without audio VHS is faster, because it encodes while the batches run and Video Batch encodes once more at the end. With audio on this ComfyUI version, every VHS batch decodes the clip's whole audio track, about 3.6 s per batch here and more on longer clips, even if its audio output isn't linked: core's memory scan walks each node's output, and that makes VHS's lazy audio decode. That comes from how the two projects meet, not from how Meta Batch works, so an update to either could remove it. With a real model the model takes nearly all the time. FlashVSR's own time per batch varied by about 17% between days, and the two FlashVSR runs above were on different days, so they don't show which is faster: on the same day as the VHS run, an earlier build of Video Batch took 900 s. In the FlashVSR runs, Video Batch's own work (reading the window and committing the batch) was about 0.4 s per batch. The row marked "earlier build" was measured before the last review fixes.

Choose VHS if you can't spare the temp disk or want the frames written so far after a Stop. Choose Video Batch if you want one queue entry, one file with its sound, no FFmpeg left running after Stop, or an overlap between batches.

**Not supported yet.** Resuming after a restart, a partial video after Stop, more than one End in a workflow, nodes inside the batch that change the frame count, MPEG-TS and MPEG files, HDR and wide-gamut video, core Start / End Loop inside the batch.

**Example workflows** (in `example_workflows/`; model fields use `SELECT_*` placeholders, pick your own files):

| File | Contents |
|---|---|
| `video_batch_upscale.json` | An upscale model between Start and End, keeping the source audio |
| `video_batch_h3.json` | MiniMax H3 redrawing each batch: 124 frames, 24 fps |

## Timeline & Long Video

> Generate overlapping short segments. Latents are the model's compressed working representation. Each segment starts from the previous segment's latent tail, with a mask that says how much of it may change, and the sampler generates the rest. Each finished segment is decoded, its duplicated overlap removed, and spilled to disk; one encode at the end writes the video. This reduces decoded-frame retention but guarantees neither seamless joins nor constant total memory.

One way to generate, one way to cut:

| Task | Nodes | Needs |
|---|---|---|
| Generate a long video | `⏱️ Edit Timeline` (`mode = generate`, with or without materials) → `🎛️ Sample Segments` → `🧵 Assemble Segments` | MiniMax H3 models (the only supported family), 24 fps |
| Cut an existing video | `⏱️ Edit Timeline` (`mode = cut`) → `✂️ Trim by Ranges` (`mode = keep`) → `PreviewAny` | a video under `input/`; no model |

**Generate.** `🎛️ Sample Segments` takes your model, CLIP, VAE, audio VAE, sampler and sigmas and, when it runs, expands into one group per segment: `🧩 Encode H3 Segment` → `🧬 Extend Latent` → core `RandomNoise` + `BasicGuider` (`CFGGuider` when `negative` is connected) + `SamplerCustomAdvanced` → `➕ Append Segment`. It only wires ComfyUI core node classes and MediaForge nodes — no sampling code of its own, no model loading or patching. Those three MediaForge nodes are [internal](#internal-nodes): you never place them, and they are kept out of the node menus. An error inside the group is shown on the Sample Segments node; the message label names the inner node (e.g. `[Encode H3 Segment]`). Model loaders, text encoders, samplers and schedulers stay your own core nodes.

**Cut.** In `cut` mode Edit Timeline needs a source `video`, and you mark the ranges to keep. Wire its `ranges` and `video_path` outputs into `✂️ Trim by Ranges` with `mode = keep`: the ranges are cut out of that same file and joined in order. They are passed in seconds, and the result keeps the source's own frame rate — measured with three ranges of 48 timeline frames at 24 fps: a 24 fps source gives exactly the 144 source frames of those ranges; a 30 fps source gives 180 frames over the same 6.000 s. Trim by Ranges is not an output node, so put an output node after it (the example uses core `PreviewAny`, which shows the result path) or ComfyUI has nothing to run. Cut mode takes no materials and has no prompts. Colour tags follow the source; the 0.12 frames path tagged its RGB round trip as BT.709, so the two paths can differ in colour metadata.

### Example workflows

ComfyUI lists a plugin's `example_workflows/` folder in its template browser. Model fields use `SELECT_*` placeholders — pick your own files. The codec is `h264 (libx264)`.

| File | Contents |
|---|---|
| `timeline_h3_basic.json` | Text only: 3 MiniMax H3 segments of 107 frames, overlaps 22 / 39 → 260 frames. Runs as soon as the model files are picked |
| `timeline_h3_director.json` | 2 MiniMax H3 segments with materials: an image reference, a video reference + guide, an audio lock; Assemble `lock_audio = original`. The three material files are placeholders under `input/mediaforge_timeline/` — use your own |
| `timeline_cut.json` | No model: Edit Timeline (`cut`) → Trim by Ranges (`ranges` + `video_path`, `mode = keep`) → preview. Pick your own video first |

### Timeline editor

Edit Timeline shows a visual editor in place of the raw `timeline_data` widget. The three native widgets you see show plain labels (`來源影片（video）`, `每秒幀數（fps）`, `用途（mode）`, and options such as `生成新影片（generate）`). This is display only: the names and values used by saved workflows and the API do not change. The fourth, `grid`, has one value left and stays hidden.

Layout, top to bottom: preview and library, toolbar, lanes (a left column names 分段 / 參考 / 指定畫面 / 保留原音, and empty lanes say what they are for), status line, inspector. Times show seconds first, such as `4.46 秒（107 幀）`. `？` opens a glossary and the keyboard shortcuts.

What an action applies to: clicking or tabbing to a segment makes it the edit target; a playhead that is elsewhere jumps there (in `generate` to where the opening overlap ends, in `cut` to the source range's start). After that, clicking or dragging the ruler, the arrow keys and playback only move the playhead; they never change the edit target. Buttons say where they act, such as `＋ 用在第 2 段` (use in segment 2) or `＋ 在 3.54 秒指定畫面` (set a frame at 3.54 s).

Segments (`generate`, 生成新影片):

- Drag segment edges. The right edge changes the length and the left edge the incoming overlap, later segments ripple, and edges snap to the legal values (lengths 5 + 17n frames; overlaps 0 or 5 + 17j).
- Inspector, first level: the segment's prompt, the reference tag buttons, the length `[−] 4.46 秒（107 幀） [＋]` (legal values only), and 從這段開始新鏡頭 / 接續前一段 (new shot / continue).
- 進階設定 (advanced): incoming overlap, exact start / end in frames, the seam in the final file (直接切換, or 交叉淡化 with its length and audio curve), seed.
- 自動分段 (auto-fill): segment count, length, overlap. Dropdowns list usable values with seconds and frames and also allow a custom value. Applying keeps each segment's prompt, seed and any crossfade that still fits, and does not move materials. The preview says which segments would be removed, which crossfades reset, how reference ownership changes, and any problem the result would have. One undo step.

Cutting (`cut`, 剪現有影片):

- Play or drag the ruler to a position; 設為起點 I / 設為終點 O (set start / set end) commit directly, one undo step each. A start past the current end moves the whole range and keeps its length; where the next range or the source end is in the way it gets shorter, and the new length is reported.
- 在目前位置新增一段 (add at this position) inserts in source order; inside a range or at the source end it refuses and says why. 刪除這段 (or `Delete`) removes any range except the last one. Ranges always stay in source order and never overlap.
- Dragging an edge moves it in whole frames between its neighbours; `ranges_json` from 🎞️ Detect Scenes can be imported.
- There are no prompt fields in `cut`: nothing reads a prompt when a video is only cut.
- With materials left in the data the timeline cannot run; the status line offers 移除素材 (remove materials: files stay in `input/`, prompts are untouched) and 切回生成新影片 (back to generate).

Source video: preview of the selected source; codecs the browser can't play get a small muted H.264 proxy. 上傳來源影片 (upload source) saves it into `input/mediaforge_timeline/`. With nothing to play the play button is disabled and says so. It plays the source or the selected material, not a generated result.

Materials (`generate` mode):

- **Library** — 加入素材 (add materials, multi-select) or drop image / video / audio files from your desktop onto the editor. They upload into `input/mediaforge_timeline/` two at a time; a batch lands as one undo step; a failed file stays as a card with 重試 / 刪除 (retry / remove). A card shows a thumbnail or icon, the name, the length once probed, and the status (上傳中 / 讀取中 / 已讀取 / 讀取失敗 — uploading / probing / read / failed). Selecting a card previews that material in the preview slot. The name is the prompt alias. New uploads are named by kind — `img1`, `vid1`, `aud1` — numbered one past the highest in use (a gap left by a deleted material is not refilled); renaming it in the inspector rewrites the prompts. Deleting a material removes its references and placements but not the uploaded file.
- **Lanes** — drag a card onto one of three lanes under the segments: while dragging, usable lanes light up and the others dim, and whether the exact spot is legal is still judged under the pointer, with the reason shown. Or select the card and use the inspector's buttons: the time on a button is where it really lands, and it follows the playhead. Every card needs to finish probing first; other combinations are refused.

  | Card | 參考 (references) | 指定畫面 (set frames) | 保留原音 (keep original audio) |
  |---|---|---|---|
  | image | reference over the segment under the drop point, minus its opening overlap | keyframe at the drop frame | — |
  | video | `follow` reference from where that segment's opening overlap ends, with the file's soundtrack if it has one | guide at the drop frame (H3: up to 22 frames, kept inside one segment and clear of its opening overlap) | — |
  | audio | reference over that segment, minus its opening overlap | — | lock from the drop frame, up to the material's length or the timeline end |

- **Bars** — drag to move, drag an end to trim, `S` (or 分割) splits at the playhead, `Delete` / `Backspace` removes, `Alt + ←/→` nudges by 1 frame (`Shift` ×10), `Escape` cancels a drag. Bars and segments take keyboard focus (`Tab`). Bars snap within 8 px to segment boundaries, then the playhead, then other bars' edges; an edit that would make the timeline invalid (for example 接續前一段 pushing a guide across a segment boundary) is refused with the reason and not applied. References split at the segment boundary nearest the playhead, never inside a segment; keyframes and H3 guides cannot be split (two 17k + 5 lengths never add up to another legal length), so they show no split button. Numbers are edited in the inspector: a reference's range, `follow` / `fixed`, content start / length, soundtrack and order (排序上移 / 下移 — the order can change the tag numbers); a placement's position, content start / length and soundtrack. A lock's inspector also states its real range, whether it was cut short, and when the final file gets the original sound back.
- **`@` material list** — type `@` (or the full-width `＠` an IME gives) in the segment prompt or the shared prompt: a list of the materials opens at the caret, and typing on filters it by name. Each row says what picking it does here — `✓ 本段 <Picture 1>` (already this segment's reference, and the tag it resolves to), `＋` (it becomes this segment's reference too), or, in the shared prompt, `✓ N 段都有` / `＋ 會補上第 2、4 段的參考` (every segment that uses the shared prompt gets the reference it lacks). A video with a soundtrack also has a `name:audio` row. ↑↓ choose, Enter / Tab insert, Esc closes the list, a click inserts; nothing reacts while an IME is composing. The tag replaces the `@` text at the caret and the caret stays in the box. Text typed since the last save is saved first as its own undo step; the tag and the references it adds are the next one, and Ctrl+Z right after a pick undoes the pick (not the typing). Rows that cannot work are greyed out with the reason.
- **Tag buttons** — `✓ name`: already a reference of this segment, a click inserts `<@name>`. `＋ name`: a click makes it this segment's reference and inserts the tag, as one undo step. The tag goes where the caret was when the prompt box last lost focus; if the box was never used, it is appended, and if the prompt already has that tag it only adds the reference. The shared prompt has its own row, with the same states and behaviour as its `@` list. A button that cannot work is disabled and its tooltip says why (several references to the same material, that category is full, the material is not probed yet). To add a reference without a tag, drag the card onto the lane.
- **Prompts** — the shared prompt (共用 prompt) is used by segments without their own; a segment's own prompt replaces it, they are not merged. The form keeps showing which one applies and offers 清空，改用共用 prompt (clear, use shared). When a button writes a tag into an empty segment prompt, the shared text is copied into it first, with a notice; typing `@` in an empty segment box starts its own prompt without copying the shared text. when ＋ only adds the reference for a tag the shared prompt already has, the segment keeps using the shared prompt. Write `<@name:audio>` for a video reference's soundtrack. Each segment resolves its aliases to its own `<Picture N>` / `<Video N>` / `<Audio N>` tags. Hand-written numbered tags still work, but their meaning changes when references are reordered; 轉成 <@名稱> rewrites them as aliases when the mapping is unambiguous, and adding a reference does the same first, refusing with a reason if it cannot.
- **這段送給模型的內容 (what this segment sends to the model)** — collapsed to one line, such as `1 張圖、1 支影片、保留原音 3.96 秒`. Expanded, once every material is probed: the tag table, the seconds and frames each reference video really uses (and how many are unused), keyframe and guide times, kept-audio ranges, the resolved prompt, and H3 technical details (token ranges as `[start, end)`, the token map, the carry boundary offset). Errors and the warning that a lock overlaps the audio carried from the previous segment stay visible when the section is collapsed.
- **Seed** — optional per-segment seed with 換一個 (new one); empty uses Sample Segments' `noise_seed`. Only Sample Segments applies it.
- **Shot cut / continue** — 從這段開始新鏡頭 (new shot) sets a segment's incoming overlap to 0 with a straight cut, so it no longer continues from the previous segment; it, later segments, and the references and placements that belong to them shift together, and items crossing the old boundary are flagged. 接續前一段 (continue) restores the default overlap (22 frames, or the largest that fits a shorter segment). Each is one undo step.

Status line:

- First line: the lasting state. `✓ 時間軸檢查通過` covers the timeline itself (structure, segments, material placement, each segment's prompt and limits), not the downstream wiring or the models. A problem names its object and reason in plain words; click to locate it, and 詳情 (details) holds the raw location and code.
- A few problems come with a fix button: a length or overlap that does not fit (for example in hand-edited JSON) → 調整段長與重疊 (fixes only the illegal values in order, later segments shift along, crossfades that no longer fit get shorter; this is not a global optimum); an fps other than 24 in `generate` → 改成 24 fps; `cut` without a source → 上傳來源影片; `cut` with materials → see above. One undo step, and the total length change is reported.
- Second line: notices about the last action, such as which segments now send something different to the model. When a reference moved to another segment or a numbered tag now names another material it says so by name, because such changes are legal and still pass the check. While the state is fine and this node's outputs are not wired, it suggests the next step (`generate` → 🎛️ Sample Segments; `cut` → `ranges` and `video_path` into ✂️ Trim by Ranges).

General:

- `timeline_data` JSON is the only state that is saved and queued; the editor's view state (zoom, selection, playhead, open sections) lives only in the editor and is not written into the workflow, so selecting or moving the playhead is not an undo step and never reaches the prompt. When the same graph is reloaded (undo / redo, or a workflow with the same graph id, such as the same file opened again) the view carries over; any other graph starts from the default view. Probe results live only in the editor. Prompt boxes can be dragged taller from their corner; the height (one for the shared prompt, one for segment prompts) is kept in the browser's storage, so it survives rebuilds and page reloads and applies to every workflow.
- After typing you can go straight to a button or another field: the text is saved and the click still lands; after a keyboard action focus stays on the same control. Letter shortcuts leave `Ctrl` / `Cmd` combinations alone.
- If the editor fails to load, the raw JSON widget is shown and stays editable.
- UI text is Traditional Chinese; technical names and stored values stay English, and display labels may show both.

The editor calls two local routes, `POST /mediaforge/timeline/probe` (videos, images and audio) and `POST /mediaforge/timeline/proxy` (videos only): same-origin requests only, files under ComfyUI `input/` only, no absolute paths in responses; limits 8 GiB per file, 30 min, 8192 px per side, 120 fps. Proxies are cached in ComfyUI's temp folder under `mediaforge_timeline_proxy` (4 GiB / 128 entries / 24 h, LRU). No internet access. Like core's own routes they have no authentication and trust the `Host` header (no DNS-rebinding protection is claimed) — on an untrusted LAN, restrict access at the network level.

### Lock audio

An audio card on the 保留原音 lane (`role = lock`; runtime messages call it 鎖定原音) keeps that original audio at that spot of the timeline, in two places:

- **While sampling** (inside Sample Segments, by Encode H3 Segment; needs `audio_vae`): in every segment the lock touches, the audio tokens (40 per second) that lie completely inside the lock are encoded from the original audio with your audio VAE — using a finite context window of up to 4 s before and 0.4 s after, an approximation of encoding the whole clip — and written into the audio latent with `noise_mask = 0`. Partial tokens at the lock's edges are generated normally. Before decoding, Append checks that the locked tokens still hold the written values (`E_LOCK_DRIFT` when the latent lost its `mf_lock` metadata or a value moved by more than 1e-3). This keeps the original audio in the latent the model samples; it makes no lip-sync claim.
- **In the final file** — Assemble `lock_audio = original` (default) builds a 48 kHz stereo float master: the generated soundtrack with each lock's range `[at, at + length)` replaced by the original audio, sample-exact before the AAC encode. `generated` keeps the generated soundtrack. A connected `audio` replaces everything and hides `lock_audio`. Without locks the setting has no effect; the report's `effective` entry says what was applied and why.
- **With carry** — when a lock overlaps the audio tokens a segment carries over from the previous one, that segment's audio carry is skipped (`W_LOCK_CARRY`, also in Sample Segments' report); video carry is unaffected. Audio continuity at that seam is then not guaranteed.

### Incremental re-render

Sample Segments passes each segment's spec into its group as constants, so ComfyUI's cache re-runs only the inner nodes whose inputs changed. A segment whose incoming overlap is 0 (the first one, or a shot cut) starts a new sampling chain: changing an earlier shot does not re-sample it. Verified on the real core executor with `--cache-classic` (CPU, stand-ins for the H3 model nodes):

| Change | Re-runs |
|---|---|
| nothing | nothing — Sample Segments itself is cached |
| only Assemble settings | Assemble only |
| segment k's prompt | k's encode, guider, extend and sampler; extend + sampler of the following segments up to the next shot cut; Append from k to the end |
| segment k's seed | k's noise, extend and sampler, and the following segments' extend + sampler up to the next shot cut; Append from k to the end; no encode |
| sampler / sigmas | every sampler and every Append; extend except at shot cuts; no encode |
| one more segment at the end | the new segment's group and the old last segment's Append |
| a run interrupted midway | inner nodes that had finished are reused |

With the other cache modes (`--cache-lru N`, `--cache-none`, and the default RAM-pressure cache) the result after a change was verified to be complete and correct, but more inner nodes may re-run and evicted results are recomputed. Reuse also needs the spill files of the cached states to still exist.

### Compatibility

Verified with ComfyUI core 0.42.5 and frontend 1.55.18 (Nodes 2.0 and classic) on an RTX 5090 — the test environment, not a minimum version.

| Path | Status | Real-run notes |
|---|---|---|
| Generate — MiniMax H3 AV through Sample Segments, 24 fps, batch 1 | **Supported** | 832×480 and 320×192, turbo 4-step. 3 segments of 107 frames, overlaps 22 and 39 → 260 frames and 346,667 audio samples at 32 kHz (last 267 samples padded). Core received the resolved `<Picture N>` / `<Video N>` / `<Audio N>` prompt, image order and cropped reference frames from the timeline; keyframe / guide frame indices matched. Locked audio tokens stayed within 2.4e-7 of the encoded original after sampling; the `lock_audio = original` master equals the original PCM sample for sample in the lock ranges; envelope correlation of the decoded generated audio vs. the original 0.997–1.000. 2 → 6 segments at 832×480: observed peak RSS +0.15 GiB (one segment of RGB is 0.48 GiB). Changing one segment's seed re-sampled only that segment and the next; adding a segment sampled only the new one |
| Cut — Edit Timeline (`cut`) → Trim by Ranges | Works | No model. Same-rate source: the kept frames are exactly the source frames of the ranges; a source at another rate keeps its own rate (see [Timeline & Long Video](#timeline--long-video)) |

Measured on 2026-10-30 (RTX 5090, ComfyUI 0.42.5, H3 fl2va fp8 + turbo 4-step LoRA) with the real H3 models. Two more results come from the loop wiring that existed up to 0.12 and gave bit-identical sampled latents (max diff 0) for the same seed, sampler and sigmas: the carried latent tokens survive the sampler exactly (max diff ≤ 5e-7), and at 832×480 the seam thumbnails differed by MAE ≈ 1 on a 0–255 scale. Besides that, Encode H3 Segment is covered by contract tests against core's `MiniMaxH3ReferenceToVideo` / `MiniMaxH3AddGuide` classes, and Sample Segments' expansion and caching by tests on the real core executor with stand-ins for the H3 model nodes (every invalidation case, eviction in LRU / RAM-pressure modes, interruption during encode / sampling / append).

Rejected (the run stops with an error): batch > 1, latents whose layout doesn't match the plan, a sampler that changes the carried or locked region, Sample Segments on anything but a `generate` plan at 24 fps.

"Supported" does not mean conditioning conflicts are checked, character consistency is guaranteed, or seams are invisible.

LTX-2 and Wan 2.2 are not supported; their experimental carry was removed in 0.13. See [Upgrading from 0.12](#upgrading-from-012).

0.13.0 integration verification (2026-11-14): core 0.44.2, frontend 1.57.6, classic / Nodes 2.0. With the same seed, old main and 0.13 H3 runs (three text segments; references + anchors + audio locks) had a maximum video/audio latent difference of 0 per segment. Text output: 260 frames, 32 kHz / 346,667 samples; locked master audio was sample-exact. Cut output on 24 / 30 / 29.97 fps sources matched the baseline decoded video and audio exactly.

### Memory and disk

- Append decodes one segment at a time, removes the overlap, applies crossfades, and spills lossless FFV1 video + float32 WAV parts (≈ 0.5 MB per frame at 1344×768) to the plugin's `.mf_tmp/seg_*` folder. It checks estimated RAM before decoding (`E_RAM`) and free disk before spilling (`E_DISK`) — estimates, not guarantees.
- Assemble does the single lossy encode (codec / crf / preset like other MediaForge encoders; ProRes → `.mov`), writes BT.709 tags, cuts the soundtrack to exactly the video's sample count before the AAC encode, then verifies the written frame count, the audio track and the color tags. It is the only output node among the Timeline nodes. Its optional `audio` replaces the whole soundtrack (padded / trimmed to the video length).
- Audio clock: the first segment fixes sample rate and channels (`E_AUDIO_LAYOUT` otherwise). H3 decoded audio is ≈ 8.3 ms shorter per segment and the next segment fills it — only real gaps and the final tail are padded (`W_AUDIO_PAD`).
- Spill parts are released when the cached state is dropped; stale session folders older than 24 h whose owner process is gone are swept at startup. A pending crossfade tail is also kept on disk, not in RAM.
- Generated frames are decoded only inside Append and spilled at once. Reference materials are decoded only inside Encode H3 Segment and never become cached outputs.
- What Sample Segments leaves in core's cache per segment is mainly the conditioning — estimated at ≈ 5 MB for a text-only prompt, about +10 MB per reference image (`ref_image_size = match`) and about +20 MB per second of reference video; nine images plus three 15 s videos ≈ 1 GB — plus a few latents of ≈ 12 MB each at 1344×768 × 107 frames. Twenty text-only segments ≈ 1.2 GB; heavy materials can reach several GB. These are estimates, not GPU measurements.
- To keep it down: start ComfyUI with `--cache-lru N`, lower `ref_video_short_edge`, keep `ref_image_size = match`. Don't count on the default RAM-pressure cache to free it — its scan may not look inside H3's nested latents and can underestimate.
- Total memory still depends on segment length, materials, crossfades, the model's working set and core's cache. This reduces decoded-frame memory; it does not guarantee constant total memory or seamless joins.
- Audio is cut sample-exactly from every source. Two cases are decoded from the start of the file up to the slice instead of seeking: containers whose timestamps are coarser than one sample (Matroska / WebM store milliseconds), and audio that has to be resampled (H3 references and locks at 32 kHz, the 48 kHz master from other rates) — a resampler started at a seek point runs at another phase than one started at the file start. That costs about 0.5 s per minute of position on a 720p MKV and less on audio-only files; same-rate MP4, MOV, WAV and MPEG-TS keep the fast seek.

### Migrating from Timeline Director

No automatic workflow or preset conversion — rebuild by concept:

| Timeline Director concept | MediaForge rebuild |
|---|---|
| Material Planner | Edit Timeline material library: references, keyframes / guides, audio locks, `<@name>` aliases |
| Select / Prepare Loop Segment | handled inside Sample Segments (one group of nodes per segment) |
| Plan Encoder | Sample Segments (it runs Encode H3 Segment for every segment) |
| Initialize / Accumulate / Finish Loop | Sample Segments + Assemble Segments; no loop nodes |
| Finite / Low-Memory Sampler | Sample Segments; Assemble already writes the file, no SaveVideo needed |
| Drift-Control / Soft AV | no equivalent; the `hard` / `taper` / `linear` masks don't claim the same effect |
| SelfLift / Omni / presets | not provided or imported; keep them as separate workflows and rebuild manually |
| Original-audio lock | audio-lock lane (latent lock in Encode H3 Segment) + Assemble `lock_audio = original`; no lip-sync claim |

### Upgrading from 0.12

0.13 keeps one way to generate (Edit Timeline → Sample Segments → Assemble Segments, MiniMax H3) and one way to cut (Edit Timeline → Trim by Ranges). The hand-wired paths are gone; a workflow that used them has to be rebuilt:

| In 0.12 | In 0.13 |
|---|---|
| `🎯 Select Segment`, `📽️ Load Segment Frames`, `🗂️ Load Segment Media` | Removed. ComfyUI reports them as missing nodes when an old workflow is opened |
| Loop or linear-chain wiring (core Start / End Loop, your own sampler per segment) | Use `🎛️ Sample Segments`. For the same seed, sampler and sigmas it sampled the same latents as the loop did |
| LTX-2 / Wan 2.2 (`grid = causal_8n+1` / `causal_4n+1`, `experimental_causal`) | Not supported. Stay on 0.12.x for those models |
| `grid` on Edit Timeline | One value, `h3_5+17n`. The widget is hidden and an old workflow is set back to it on load; an API prompt that sends another value is refused by ComfyUI (`Value not in list`) |
| Model-free assembly (Load Segment Frames → Append `frames` / `audio`) | `cut` mode → `✂️ Trim by Ranges` (`timeline_cut.json`). It keeps the source's frame rate instead of resampling to the timeline rate |
| Prompts in `cut` mode | No longer shown or read |
| `🧬 Extend Latent`, `🧩 Encode H3 Segment`, `➕ Append Segment` in the node menus | Still registered, because Sample Segments builds them, but hidden: see [Internal nodes](#internal-nodes). Their `state` (Extend), `frames` / `audio` (Append) and `experimental_causal` inputs are gone |
| Examples `timeline_h3_av_loop`, `timeline_ltx2_video_carry`, `timeline_wan22_experimental`, `timeline_frames_assembly` | `timeline_h3_basic`, `timeline_h3_director`, `timeline_cut` |

Edit Timeline keeps its widget order and its first five outputs, so a 0.12 workflow built like `timeline_h3_director.json` loads unchanged; `video_path` is a new sixth output.

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

`type` is always `output` for MediaForge results. Each run produces a new auto-counter file (`_00001.mp4` → `_00002.mp4` → ...), so multiple workflow runs never silently overwrite. 💾 Save Video writes a new `<prefix>-<id>/video.mp4` directory per run instead.

### Subtitle / text outputs

`MF_ConvertChinese` writes `.srt` or `.txt` (heuristic: presence of `-->` in converted text). `MF_WhisperTranscribe` / `MF_TranslateSubtitle` return SRT **as a string** (not a file) — wire it into `MF_BurnSubtitle`'s `srt_text` input (the node materializes a temp file internally), or pipe through `MF_ConvertChinese` with `filename_prefix` set when you want the SRT saved as a real file.

### Timeline prompts

`MF_EditTimeline` takes `timeline_data` as a JSON **string** input; `video` / `fps` / `grid` / `mode` are separate inputs (`fps` is the combo string, e.g. `"24"` or `"30000/1001"`; `grid` is always `"h3_5+17n"`; `video` is `"(none)"` for `generate` without a source). `plan_json` is a derived output — it is never an input and can't be pasted back into `timeline_data`.

| Field | Rule |
|---|---|
| `version` | integer `2` (`1` is still accepted and upgraded; it has no `prompt`, materials or seeds) |
| `source.origin_frame` | source time origin, default `0` |
| `prompt` | global prompt, default empty; used by every segment whose own `prompt` is empty |
| `segments` | 1–256 segments |
| `segments[].start` / `end` | integer frames, `end` exclusive; `generate`: lengths 5 + 17n, overlaps 0 or 5 + 17j; `cut`: inside the source, no overlap |
| `segments[].prompt` | string, default empty |
| `segments[].seed` | optional, `null` or 0 … 2⁵³ − 1; applied only by Sample Segments |
| `segments[].seam_in.kind` | `cut` / `crossfade`; `crossfade` needs a legal `frames` |
| `segments[].seam_in.audio_curve` | `auto` / `linear` / `equal_power` |
| `materials` | ≤ 64 of `{id, kind, ref, name}`: `kind` = `image` / `video` / `audio`; `ref` = a file under `input/`; `name` = the alias, 1–32 ASCII letters / digits / `_` / `-` / CJK / kana / Hangul, NFC, unique |
| `refs` | ≤ 256; array order = tag order. image `{id, material, from, to}`; video adds `mode` (`follow`, default, or `fixed`), `in`, `audio` and, for `fixed`, `length`; audio adds `in`, `length` |
| `anchors` | ≤ 512. image: `{id, material, role: "keyframe", at}`; video: `{…, role: "guide", at, in, length, audio}`; audio: `{…, role: "lock", at, in, length}` |

`id`s are 1–32 characters of `A–Z a–z 0–9 _ -`, unique within their list. All positions are timeline frames; `in` / `length` count timeline frames from the material's start. A reference belongs to every segment whose output range it overlaps; a guide must lie inside one segment's output range (H3: length 5 + 17k); `cut` mode takes no materials. `<@name>` / `<@name:audio>` aliases and `<Picture N>` / `<Video N>` / `<Audio N>` tags are for `generate` mode.

Unknown fields, duplicate keys, booleans used as integers and non-finite numbers are rejected (`E_TIMELINE_JSON`).

A minimal v2 timeline — an image reference for both segments, an audio lock at frames 12–72 and a fixed seed for segment 2, which uses the global prompt (send it as a JSON string in `timeline_data`):

```json
{
  "version": 2,
  "source": {"origin_frame": 0},
  "prompt": "<@nurse> stands in a bright clinic room.",
  "materials": [
    {"id": "m1", "kind": "image", "ref": "mediaforge_timeline/nurse.png", "name": "nurse"},
    {"id": "m2", "kind": "audio", "ref": "mediaforge_timeline/line1.wav", "name": "line1"}
  ],
  "refs": [{"id": "r1", "material": "m1", "from": 0, "to": 192}],
  "anchors": [{"id": "a1", "material": "m2", "role": "lock", "at": 12, "in": 0, "length": 60}],
  "segments": [
    {"start": 0, "end": 107, "prompt": "<@nurse> walks into the clinic and greets the patient."},
    {"start": 85, "end": 192, "prompt": "", "seed": 7}
  ]
}
```

With `fps = "24"`, `grid = "h3_5+17n"`, `mode = "generate"` both prompts resolve to `<Picture 1> …`. For Sample Segments in API format, `noise_seed` is a plain integer (no `control_after_generate`).

A complete prompt that cuts an existing video (no model):

```json
{
  "1": {"class_type": "MF_EditTimeline", "inputs": {"video": "clip.mp4", "fps": "24", "grid": "h3_5+17n", "mode": "cut",
        "timeline_data": "{\"version\": 2, \"source\": {\"origin_frame\": 0}, \"segments\": [{\"start\": 0, \"end\": 48}, {\"start\": 96, \"end\": 144}]}"}},
  "2": {"class_type": "MF_TrimByRanges", "inputs": {"video_path": ["1", 5], "ranges": ["1", 1], "mode": "keep",
        "filename_prefix": "MediaForge/timeline_cut", "ranges_json": "[]", "crossfade_sec": 0.0,
        "codec": "h264 (libx264)", "crf": 18, "preset": "medium"}},
  "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 0]}}
}
```

`clip.mp4` is a file under `input/`. Edit Timeline's `ranges` is output 1 and `video_path` output 5. Trim by Ranges is not an output node, so the prompt needs one after it — here core `PreviewAny`.

To generate instead, set `"mode": "generate"` and replace nodes 2–3 with `MF_SampleSegments` (`"segment_plan": ["1", 0]`, links to your model / clip / vae / sampler / sigmas nodes, `noise_seed`, `width`, `height`, `mask`, `carry_audio`) and `MF_AssembleSegments` (`"state": [<sample_segments_id>, 0]`, `filename_prefix`, `codec`, `crf`, `preset`). Assemble emits `ui.images` like the other file producers, so `/history` lists its output.

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
| Long video through any IMAGE node, batch by batch | ✅ Meta Batch Manager | ✅ Video Batch | [Compared here](#video-batch), with measurements |
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
> **Output filename pattern**: every file-producing node uses `filename_prefix` + auto-counter (same pattern as ComfyUI core `SaveImage`). Each run writes `output/<prefix>_00001.mp4` → `_00002.mp4` → ... so repeated workflow runs never silently overwrite earlier results. Subdirectories in the prefix are fine (e.g. `MediaForge/subtitled`). Exception: 💾 Save Video writes one directory per run, `output/<prefix>-<id>/video.mp4`.

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

#### 💾 Save Video · Serial H.264 (`MF_SaveVideo`)

ComfyUI `VIDEO` → H.264 MP4, AAC when the video has audio. The decoders of a loaded file, the RGB→YUV conversion (swscale), libx264 and the AAC encoder each run on one thread. A ComfyUI V3 output node: it shows a video preview and passes the input on unchanged.

**When to use**: saving any `VIDEO` (core Load Video, Create Video, other `VIDEO` nodes) when a serial, repeatable export matters more than encode speed — on a machine that is busy sampling, or to rule threading out when a save misbehaves.

| Widget | Default | Notes |
|---|---|---|
| `video` | (required VIDEO) | Core `VideoFromFile` (Load Video) keeps core's trim / crop / rotation / audio-track rules and decodes with single-threaded decoders. Already-decoded videos (`VideoFromComponents`, e.g. Create Video) are used as they are. Other `VIDEO` types provide their own components; the report marks their decode threads as unknown. |
| `filename_prefix` | `MediaForge/serial_video` | Relative to `output/`, may contain subfolders. Absolute paths, `..` and symlinks / junctions that lead out of `output/` are refused before anything is written. Each run writes `output/<prefix>-<32-hex id>/video.mp4`. |

**Outputs**: `video` (the input object itself), `video_path` (absolute path of the finished MP4), `report_json`. The node shows the finished MP4 as its preview.

**Encoding**: libx264 CRF 23, preset `medium` (x264's defaults); `yuv420p` for 8-bit sources, `yuv420p10le` for 10-bit (the 10-bit path keeps 16-bit RGB until the YUV conversion). Colour follows ComfyUI's tags: `sRGB` → BT.709 / sRGB transfer, `HDR` → BT.2020 / HLG, `HDR PQ` → BT.2020 / PQ; a source that does not say is saved as `sRGB`. fps is kept to three decimals like core (`24000/1001` → `23.976`); frames are never dropped or duplicated. Audio keeps its sample rate and channel layout (1 = mono, 2 = stereo, 6 = 5.1) and is cut at `ceil(sample_rate × frames / fps)`. The MP4 uses `faststart`.

**Refused with an error, no file written**: alpha (H.264 has none; core gives GIFs an alpha channel, so a GIF from Load Video needs Get Video Components → Create Video without alpha first), sources that are not 8- or 10-bit, odd width or height, NaN / ±Infinity in frames or audio, audio with other channel counts or a sample rate AAC does not support, and HDR on PyAV 17 (its colour conversion has no BT.2020 matrix; needs `av` 18 or newer).

**Writing**: encodes to `video.partial.mp4`, then flushes, closes, `fsync`s and renames it to `video.mp4` in the same directory. Cancelling (checked before decoding, at every frame and before the rename) or a failure never leaves a `video.mp4` and returns no path or preview; a partial file that already holds data stays for diagnosis. Runs with the same prefix at the same time each get their own directory.

**Metadata**: follows `--disable-metadata`. Otherwise each top-level `extra_pnginfo` field (e.g. `workflow`) and the running `prompt` become one strict-JSON MP4 tag each. `NaN` / `±Infinity` — such as the `is_changed: [NaN]` cache marker that nodes like 🎬 End Video Batch leave in the running prompt — are written as `null`; the prompt and workflow themselves are not changed, and Unicode is written as-is.

**`report_json`**: `status`, `path`, `subfolder`, `width`, `height`, `frames`, `fps` / `fps_fraction`, `source_fps` / `source_fps_fraction`, `bit_depth`, `pix_fmt`, `color_space`, `video` (encoder settings and the thread counts read back from the codec and from x264's own settings string), `audio` (or `null`), `color_conversion`, `source` (its type and how it was decoded), `metadata`, and `checks` — only the checks that actually ran.

**Notes**: a loaded file is decoded into memory in full, like core's Get Video Components. Core itself converts the decoded YUV to RGB with PyAV's automatic thread count; that step is outside this node, and the report says so. PyAV feeds AAC through a small libavfilter graph whose thread pool cannot be configured; it stays idle.

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

Plans a long video as segments on one timeline, or marks the ranges to cut out of an existing one. Edit it in the embedded [timeline editor](#timeline-editor) or send `timeline_data` JSON directly ([Timeline prompts](#timeline-prompts)); the JSON is validated strictly on every run.

**When to use**: the first node of every Timeline workflow — `generate` plans the segments 🎛️ Sample Segments generates with MiniMax H3; `cut` picks source ranges for ✂️ Trim by Ranges.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `video` | dropdown | `(none)` | Shown as 來源影片（video）. Source video under `input/`. Optional in `generate`, required in `cut`; `(none)` reads 不使用來源影片 or 尚未選擇（必選） depending on the mode |
| `fps` | dropdown | `24` | Shown as 每秒幀數（fps）. Rational timeline rate: `16` / `24` / `25` / `30` / `24000/1001` / `30000/1001` / `48` / `50` / `60000/1001` / `60`. `generate` requires `24` |
| `grid` | dropdown | `h3_5+17n` | One value, hidden in the UI. It stays in the schema so that saved workflows keep their widget order; a value saved by an older version is set back on load |
| `mode` | dropdown | `generate` | Shown as 用途（mode）: 生成新影片（generate） / 剪現有影片（cut）. `generate` plans generated segments; `cut` selects source ranges, with no model involved |
| `timeline_data` | STRING (multiline) | `{"version": 1, "source": {"origin_frame": 0}, "segments": [{"start": 0, "end": 107, "prompt": ""}]}` | The plan as a JSON string (v2 with materials — see [Timeline prompts](#timeline-prompts); the editor saves v2). Hidden while the visual editor works; shown and editable if the editor fails to load |

| `mode` | Legal lengths (frames) | Legal overlaps (frames) | Carry |
|---|---|---|---|
| `generate` | 5 + 17n | 0 or 5 + 17j | video (+ audio with `carry_audio`); fps must be `24` |
| `cut` | any ≥ 1 | none — ranges may touch but not overlap | none |

`generate`: the first segment starts at 0; starts and ends strictly increase with no gaps, so each overlap is shorter than both neighbours; a `crossfade` seam needs 1 … min(overlap, previous segment's output length) frames. `cut`: needs `video`; segments are sorted, don't overlap (they may touch), stay inside the source and use `cut` seams only.

**Outputs**:

| Output | Type | Notes |
|---|---|---|
| `segment_plan` | MF_SEGMENT_PLAN | `generate`: wire to Sample Segments |
| `ranges` | SILENCE_RANGES | `cut`: the selected source ranges in seconds (same convention as `MF_DetectScenes`) — wire to `MF_TrimByRanges`; `generate`: empty list |
| `plan_json` | STRING | The validated plan as JSON, for inspection — never an input |
| `segment_count` | INT | Number of segments |
| `total_frames` | INT | Output length in frames |
| `video_path` | STRING | Absolute path of the selected `video`, empty when none is selected. `cut`: wire to `MF_TrimByRanges` `video_path`, so both nodes read the same file |

**Notes**: `IS_CHANGED` fingerprints the selected `video` and every material file, so replacing a file re-plans (when `timeline_data` or `video` is a linked input, Edit Timeline re-runs every time). Every run probes the materials (`E_MATERIAL`), checks references and anchors against them (`E_REF`, `E_ANCHOR`, `E_ANCHOR_OVERLAP`), and in `generate` applies the per-segment limits and tags (`E_REF_LIMIT`, `E_REF_SHORT`) and resolves the prompts (`E_PROMPT_REF`). Prints `W_SOURCE_TIME_UNKNOWN` when the source's start time can't be read.

**Example**: `fps = 24`, segments `[0,107)`, `[85,192)`, `[153,260)` (overlaps 22 and 39, both 5 + 17j) → `segment_count = 3`, `total_frames = 260`.

---

#### 🧵 Assemble Segments (`MF_AssembleSegments`)

Encodes the spilled parts into the final video — the single lossy encode of the Timeline chain — and is its output node.

**When to use**: after 🎛️ Sample Segments — wire its `state` here.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `filename_prefix` | STRING | `MediaForge/timeline` | → `output/<prefix>_NNNNN.mp4` (`.mov` for ProRes) |
| `codec` | dropdown | smart (NVENC if available, else libx264) | Same catalog as the other encoders |
| `crf` | INT | `18` (0–51) | Hidden for `prores (prores_ks)`. Scale differs by codec — see [Smart GPU codec default](#smart-gpu-codec-default) |
| `preset` | dropdown | `medium` | `ultrafast` … `veryslow`. Hidden for `prores (prores_ks)` |
| `lock_audio` *(optional)* | dropdown | `original` | `original`: the [locked](#lock-audio) ranges get the original audio (48 kHz stereo master); `generated`: keep the generated soundtrack. No effect without locks. Hidden when `audio` is connected |

**Inputs**: `state` (MF_SEGMENT_STATE, required) — from Sample Segments; `audio` (AUDIO, optional) — replaces the whole soundtrack, padded with silence / trimmed to the video length.

**Outputs**: `final_video_path` STRING; `report` STRING (JSON: frames, fps, size, color assumption, audio source (`internal` / `override` / `locked_original` / `none`) / sample rate / samples, padded samples, per-seam diagnostics, `effective` lock-audio entry). Emits `ui.images` for `/history`.

**Notes**: assumes BT.709 (full-range RGB → limited-range YUV) and writes those tags; verifies the frame count, and with internal audio the exact sample count; `E_SPILL_MISSING` if parts were deleted (e.g. `.mf_tmp/` cleaned up, or a cache eviction) — queue the workflow again. Spill parts are not deleted here, because the cached state may still be reused.

---

#### 🎞️ Start Video Batch (`MF_StartVideoBatch`)

Feeds a video into your nodes one batch at a time. Pairs with `🎬 End Video Batch`; see [Video Batch](#video-batch).

**When to use**: to run an IMAGE node over a video that's too long to hold in memory. The file picker is built in, so the flow stays two nodes; this is the one exception to using `📂 Select Video`.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `video` | dropdown + upload | | A video under `input/`. Hidden with its upload button when `video_path` is linked |
| `resize` | dropdown | `none` | `none` / `fit` (letterbox) / `fill` (crop) / `stretch` |
| `width`, `height` | INT | `1280`, `720` | Hidden for `resize = none` |
| `video_path` *(optional)* | STRING socket | | Takes priority over `video`. Must not depend on a node inside the batch |

**Outputs**: `frames` IMAGE — this batch, always `frames_per_batch` frames; `audio` AUDIO — this batch's audio, decoded only if linked; `fps` FLOAT; `frame_count` INT — frames in this batch including repeated ones, not the length of the video.

**Notes**: mp4 / mov / mkv / webm / avi. MPEG-TS and MPEG have no keyframe index to seek by, and HDR and wide-gamut video isn't handled yet; both are refused with the command to convert them. The fps comes from the source unless End sets one; variable frame rate is resampled to it. If the source file changes during the job, it stops instead of mixing two videos.

---

#### 🎬 End Video Batch (`MF_EndVideoBatch`)

Runs the job: asks for each batch, writes it to disk, and encodes the finished video. Its output node.

**When to use**: paired with `🎞️ Start Video Batch`. All the job's settings are here.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `frames_per_batch` | INT | `64` | Frames sent to your nodes at once. Lower uses less RAM and VRAM. H3 needs 17n+5, for example 124 |
| `fps` | FLOAT | `0` | Convert to this fixed fps; `0` uses the source's. When sources differ, `0` picks one (see Two or more videos) |
| `max_frames` | INT | `0` | Process only the first frames, for a test run; `0` is all |
| `codec` | dropdown | smart (NVENC if available, else libx264) | Same catalog as the other encoders |
| `keep_source_audio` | BOOLEAN | `true` | Keep the source's soundtrack. Hidden when `audio` is linked |
| `filename_prefix` *(advanced)* | STRING | `MediaForge/video_batch` | → `output/<prefix>_NNNNN.mp4` (`.mov` for ProRes) |
| `overlap_frames` *(advanced)* | INT | `0` | Frames two neighbouring batches share and crossfade over; at most half of `frames_per_batch` |
| `crf` *(advanced)* | INT | `18` (0–51) | Hidden for `prores (prores_ks)` |
| `preset` *(advanced)* | dropdown | `medium` | Hidden for `prores (prores_ks)` |

**Inputs**: `frames` (IMAGE, required) — the processed frames; `audio` (AUDIO, optional) — see the [audio table](#video-batch). Every widget here is settings-only, with no socket, because Start has to read them before End runs.

**Outputs**: `final_video_path` STRING; `report` STRING (JSON: frames, fps, size, batches, sources, audio source (`internal` / `override` / `none`), sample rate and samples, per-seam diagnostics).

**Notes**: nodes after End may read `final_video_path`, but not the frames inside the batch, because they would only get one batch. Errors are explained under [Video Batch messages](#video-batch-messages).

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

Schema marked **experimental** — `AI_CONFIG` API may change inside Phase 5 until Whisper / Translate are validated end-to-end across all provider recipes. `🎛️ Sample Segments` and the [internal nodes](#internal-nodes) it builds (last in this section) are Timeline nodes — they take no `AI_CONFIG` and make no network requests.

#### ⚙️ AI Config (`MF_AIConfig`)

Centralized provider configuration. Outputs an `AI_CONFIG` dict that all AI nodes consume — swap provider / model / endpoint in one place and the whole chain follows.

**When to use**: any AI workflow. Drop one `MF_AIConfig` per backend (e.g. one for ASR via Groq, one for translation via OpenAI) and fan out into the consumers.

**On the node**: the `服務` (service) and `模型` (model) dropdowns at the top fill in the fields below, so you don't have to look up URLs and model names, and fields a service doesn't use are hidden. Service `自訂` (custom) shows every field; model `自訂…` lets you type a name. Both dropdowns exist only in the frontend and never enter the workflow JSON, so old workflows and API calls keep using the same five inputs. Switching service replaces `api_key` with the new service's default, so a plaintext key is never carried over to another service. The model list ships with the plugin and needs no network; for Ollama / LM Studio you type the name of a model you have downloaded.

| Widget | Default | Notes |
|---|---|---|
| `provider` | `openai_compatible` | `openai_compatible` (any `/v1/...` HTTP endpoint) / `faster_whisper_local` (in-process) |
| `base_url` | `https://api.openai.com/v1` | Trailing slash stripped automatically |
| `api_key` | `""` | **Recommended: `env:OPENAI_API_KEY`** — the `env:` prefix resolves the named environment variable at runtime, so the secret never enters the workflow JSON (a plaintext key serializes into every exported/shared workflow — the canvas mask cannot protect that path; missing variable → clear error). As an exfiltration guard, an `env:`-resolved key is only allowed toward allowlisted hosts (built-in: `api.openai.com` / `api.groq.com` / localhost family, plus `openrouter.ai` for `OPENROUTER_API_KEY` only and `generativelanguage.googleapis.com` for `GEMINI_API_KEY` only) — extend with the `MF_AI_KEY_ALLOWED_HOSTS` env var (comma-separated hostnames, global) or `MF_AI_KEY_ALLOWED_HOSTS_<VARNAME>` (per-variable), so a malicious shared workflow can't pair `env:SOME_SECRET` with an attacker endpoint. `env:`-resolved keys also require **HTTPS** toward any non-loopback host (plaintext HTTP would expose the Bearer header) — only `localhost` / `127.0.0.1` / `::1` may use `http://`; a private-LAN IP counts as remote, so an allowlisted LAN endpoint still needs HTTPS (or use a plaintext key, which skips both guards). Logged with first 4 chars + `***` mask; node canvas displays plaintext keys masked with `•` (`env:` references shown as-is; click to reveal/edit — see `web/ai_config_mask.js`) |
| `model` | `gpt-6-luna` | String; any model outside the list works too. Whisper substitutes the service's speech model if this isn't an STT id (see Model auto-substitution under Whisper Transcribe) |
| `device` | `auto` | `cpu` / `cuda` / `auto` — only used by `faster_whisper_local` |

**Output**: `ai_config` (AI_CONFIG dict).

**See [AI Provider Recipes](#ai-provider-recipes)** for copy-paste-ready `provider` / `base_url` / `model` combos for OpenAI / Groq / OpenRouter / Gemini / Ollama / faster-whisper.

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
- `openai_compatible` — POSTs to `<base_url>/audio/transcriptions`, asks for `verbose_json` and builds the SRT itself, so the endpoint must return timed `segments` (documented by OpenAI `whisper-1` and Groq; anything else raises instead of returning empty subtitles). Uses `requests` from `requirements.txt`.
- `faster_whisper_local` — lazy-imports `faster-whisper`. Honors `ai_config.device` (`cpu` / `cuda` / `auto`) and `ai_config.model` (`tiny` / `base` / `small` / `medium` / `large-v3`). First run downloads model to HF cache. Requires `pip install faster-whisper`.

**Model auto-substitution**: if `ai_config.model` doesn't look like an STT model id (e.g. it's `gpt-6-luna` because the same `AI_CONFIG` feeds Translate), Whisper substitutes the service's speech model — `whisper-large-v3-turbo` for Groq, `whisper-1` for other OpenAI-compatible endpoints, `base` for faster-whisper-local — and logs the model it used. Set an explicit STT model id (`whisper-large-v3`, `distil-large-v3`, …) to override. OpenRouter and Gemini have no speech support here, so wiring them to Whisper raises an error.

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

**Recommended models**: `gpt-6-luna` (OpenAI's cheapest text model today); try `gpt-6.1-terra` / `llama-3.3-70b-versatile` for long-form or specialized vocabulary.

---

#### 🎛️ Sample Segments (`MF_SampleSegments`)

Generates a MiniMax H3 timeline one segment at a time, in one node. When it runs it expands into one group per segment — `🧩 Encode H3 Segment` → `🧬 Extend Latent` → core `RandomNoise` + `BasicGuider` / `CFGGuider` + `SamplerCustomAdvanced` → `➕ Append Segment` — and returns the last Append's state. See [Timeline & Long Video](#timeline--long-video), [Lock audio](#lock-audio) and [Incremental re-render](#incremental-re-render).

**When to use**: between ⏱️ Edit Timeline (`generate`, 24 fps; anything else raises before expanding) and 🧵 Assemble Segments.

| Widget | Type | Default | Notes |
|---|---|---|---|
| `noise_seed` | INT | `0` (+ `control_after_generate`) | Seed of every segment that has no seed of its own in the timeline |
| `width` | INT | `1344` (32–8192, step 32) | Generated size |
| `height` | INT | `768` (32–8192, step 32) | |
| `mask` | dropdown | `hard` | How much of the carried region the sampler may change: `hard` keeps every carried token; `taper` relaxes the tail of the carried region; `linear` relaxes progressively (the last carried token becomes fully denoisable, except when only 1 token is carried) |
| `carry_audio` | BOOLEAN | `True` | Carry the audio tail as well as the video tail; skipped per segment where a lock sits on the carried audio (`W_LOCK_CARRY`). Off leaves audio latents and masks unchanged |
| `cfg` *(optional)* | FLOAT | `1.0` (0–100, step 0.1) | Used only with `negative` (CFG guider); hidden while `negative` isn't connected |
| `ref_image_size` *(optional)* | dropdown | `match` | `match`: reference images scaled to the generated area (faster); `max`: short edge 2048 (closer to the source, several times slower). Images with a short edge above 2048 are scaled down on load either way |
| `ref_video_short_edge` *(optional)* | INT | `0` (0–4096, step 32) | Reference videos are decoded at most this short edge (32-aligned), which cuts reference tokens and memory. `0`: videos with a short edge above 768 are decoded at 768, then core resizes as usual |
| `video_taper_tokens` *(optional)* | INT | `2` (0–1000000, step 1) | Video tokens relaxed at the tail in `taper` mode; must not exceed the carried token count. Hidden when `mask = hard` or `linear` |
| `audio_taper_tokens` *(optional)* | INT | `2` (0–1000000, step 1) | Same for audio. Hidden when `mask = hard` or `linear`, and when `carry_audio` is off |
| `on_conflict` *(optional)* | dropdown | `error` | `error` stops when an existing mask conflicts with the carry region (`E_MASK_CONFLICT`); `carry_wins` overwrites samples and mask there |

**Sockets**:

| Input | Type | Notes |
|---|---|---|
| `model` | MODEL | H3 model (e.g. UNet + turbo LoRA) |
| `clip` | CLIP | H3 text encoder |
| `vae` | VAE | H3 video VAE — encodes references, decodes each segment |
| `segment_plan` | MF_SEGMENT_PLAN | From Edit Timeline |
| `sampler` | SAMPLER | e.g. core `KSamplerSelect` |
| `sigmas` | SIGMAS | e.g. core `BasicScheduler` |
| `audio_vae` *(optional)* | VAE | H3 audio VAE — decodes the audio and encodes audio materials; required when the timeline has audio references, soundtracks or locks. Without it the result has no audio track |
| `negative` *(optional)* | CONDITIONING | Connect to sample with `CFGGuider` and `cfg`; without it `BasicGuider` (turbo models) |

**Outputs**: `state` (MF_SEGMENT_STATE) → Assemble Segments; `report` STRING (JSON per segment: effective seed, whether it starts a shot, its three identities, tags, reference-video requested / effective frames, anchors, latent lock tokens, whether audio carry was skipped, material fingerprints, each lock's encoding policy (`ctx_v1`) and planned context tokens, a planned per-segment `effective` list (seed, audio carry, taper, lock — verified later by Append's drift check — and reference crops) and `estimates` (decoded RGB bytes, spill bytes, reference-material decode sizes); plus `effective` entries for `cfg` and audio decoding). Estimates are sizes, not a memory guarantee.

**Notes**: every model input is wired from your own nodes into the group; each segment's spec goes in as a constant, so unchanged segments come from the cache. Errors inside the group show on this node, labelled with the inner node's name. Batch 1 only. Per-segment limits and crops are applied by Edit Timeline: ≤ 9 pictures, ≤ 3 videos, ≤ 3 standalone audio references (`E_REF_LIMIT`); a reference video is cut to the segment length and down to 5 + 17k frames (< 5 → `E_REF_SHORT`).

---

#### Internal nodes

Three nodes exist only because Sample Segments builds them. They are registered (graph expansion needs that), flagged deprecated so that the node search and the node library skip them, and taken out of the right-click Add Node menu. "Deprecated" is just the flag ComfyUI offers for this; they are not going away. You meet their names in two places: as the label of an error raised inside Sample Segments, and in the API's `/object_info`.

| Node | What it does for one segment | Messages it can raise |
|---|---|---|
| `🧩 Encode H3 Segment` (`MF_EncodeH3Segment`) | Builds the MiniMax H3 conditioning and the empty AV latent from the segment's prompt and materials: references through core `MiniMaxH3ReferenceToVideo`, keyframes and guides through core `MiniMaxH3AddGuide` in frame order, then the [audio locks](#lock-audio) written into the latent (`noise_mask` and the `mf_lock` metadata). Materials are decoded here only for that call | `E_H3_NODES`, `E_SOURCE_CHANGED`, `E_SOURCE_SHORTFALL`, `W_AUDIO_PAD` |
| `🧬 Extend Latent` (`MF_ExtendLatent`) | Copies the previous segment's sampled latent tail into the start of this segment's latent and sets `noise_mask` so the sampler keeps (or partly re-generates) that region. Tensor work only: no model is loaded and nothing is sampled. Adds the `mf_carry` marker Append checks | `E_TOKENS`, `E_AUDIO_CARRY`, `E_CARRY_SOURCE`, `E_MASK_CONFLICT`, `E_PROVENANCE`, `W_PROVENANCE`, `W_LOCK_CARRY` |
| `➕ Append Segment` (`MF_AppendSegment`) | Decodes the sampled latent with your VAE (and audio VAE), removes the overlap with the previous segment, applies the incoming crossfade and spills lossless parts to disk. Returns a new state; the input state is not modified | `E_SEQUENCE`, `E_PLAN`, `E_PROVENANCE`, `W_PROVENANCE`, `E_CARRY_DRIFT`, `E_LOCK_DRIFT`, `E_RAM`, `E_DISK`, `E_AUDIO_LAYOUT`, `W_AUDIO_PAD` |

A crossfade with `audio_curve = auto` uses `linear` when the segment's audio was carried from the previous one, otherwise `equal_power`. Extend Latent does not inspect conditioning (prompts, keyframes, references); its report always says `conditioning_check: "unavailable"`.

## AI Provider Recipes

`MF_AIConfig` outputs a dict consumed by all AI nodes. Same `provider` / `base_url` / `api_key` / `model` interface — only the values change. Picking a service on the node fills these in; the values below are for API calls or the custom service:

### OpenAI (official)

```
provider   = openai_compatible
base_url   = https://api.openai.com/v1
api_key    = sk-...
model      = whisper-1         # for MF_WhisperTranscribe
           = gpt-6-luna        # for MF_TranslateSubtitle
```

Paid; most reliable. OpenAI has announced that `whisper-1` shuts down on 2027-02-26; its recommended `gpt-transcribe` is documented with JSON output only, without timestamps, so it can't produce subtitles today.

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

### OpenRouter (translate only)

```
provider   = openai_compatible
base_url   = https://openrouter.ai/api/v1
api_key    = env:OPENROUTER_API_KEY
model      = ~openai/gpt-sol-latest           # a leading `~` is an alias for the newest model in that family
           = ~anthropic/claude-sonnet-latest
```

A different `env:` variable name also needs `MF_AI_KEY_ALLOWED_HOSTS_<VARNAME>=openrouter.ai` in the environment that starts ComfyUI.

### Gemini (translate only)

```
provider   = openai_compatible
base_url   = https://generativelanguage.googleapis.com/v1beta/openai
api_key    = env:GEMINI_API_KEY
model      = gemini-3.8-flash
```

A different `env:` variable name also needs `MF_AI_KEY_ALLOWED_HOSTS_<VARNAME>=generativelanguage.googleapis.com` in the environment that starts ComfyUI.

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
- **MF_SEGMENT_PLAN** (v2): immutable plan dict from `MF_EditTimeline` — `plan_id` / `execution_id`, mode, fps, grid, source reference, global prompt, material snapshots (relative `ref`, fingerprint, probed length / size / audio layout), refs, anchors, segments, per-segment specs, `total_frames`. No absolute paths.
- **MF_SEGMENT** (v2): immutable per-segment spec — index, start / end / length, overlaps, carry sizes, seams, source window, resolved prompt, the segment's materials, audio locks and three identities: `encode_id` (what goes into the encoder), `sampling_id` (chained back to the last shot cut, plus the seed), `assembly_id` (chained through every earlier segment). The full spec lives in the plan only; Sample Segments gives each inner node a reduced `encode` / `extend` / `assemble` view, the one form a node accepts (`E_VIEW` otherwise).
- **MF_SEGMENT_STATE** (v2): immutable dict; every `MF_AppendSegment` returns a new one (session, spill folder, committed frames / samples, `assembly_id`, locks, pending crossfade tail on disk, carried latent tail). It owns the spill parts in-process, so it can't be serialized into a workflow.
- **LATENT `mf_carry`**: `MF_ExtendLatent` adds `{sampling_id, segment_index, streams_carried, carry_dtypes, mask, taper}` to its output LATENT dict; core samplers keep it. It is written before sampling: it stops latents whose planned content or seed differ from being joined, but can't prove a sampler actually used that seed, model or sigmas, and it doesn't cover the main source video (Edit Timeline's fingerprint covers that). Append reads it as a consistency hint (consistent / absent / mismatch), not as proof of origin, and uses `carry_dtypes` (the dtype the carry was written in, e.g. `bfloat16`) as the baseline of its `E_CARRY_DRIFT` check.
- **LATENT `mf_lock`**: `MF_EncodeH3Segment` adds `{policy, tokens: [[K0, K1], …], target: [tensor, …]}` when it writes locked audio — the locked audio-token ranges and independent copies of the written values. Append compares the sampled tokens against `target` (`E_LOCK_DRIFT`).

## Architecture

```
comfyui_MediaForge/
├── __init__.py              # pkgutil auto-discover nodes/ — drop a file in, it shows up
├── pyproject.toml
├── requirements.txt         # ffmpeg-binary fallbacks + small pure-Python deps; heavy optional deps stay lazy-imported
├── nodes/                   # one file per node, MF_<Verb><Noun>
│   ├── ai_config.py            # MF_AIConfig
│   ├── append_segment.py       # MF_AppendSegment  — Timeline, internal: decode, de-overlap, spill to .mf_tmp/
│   ├── assemble_segments.py    # MF_AssembleSegments  — Timeline: final encode (OUTPUT_NODE)
│   ├── start_video_batch.py    # MF_StartVideoBatch  — Video Batch: decodes one batch per run
│   ├── end_video_batch.py      # MF_EndVideoBatch  — Video Batch: drives the batches, final encode (OUTPUT_NODE)
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
│   ├── encode_h3_segment.py    # MF_EncodeH3Segment  — Timeline, internal: H3 conditioning + empty AV latent via core MiniMaxH3* nodes
│   ├── extend_latent.py        # MF_ExtendLatent  — Timeline, internal: latent tail-carry
│   ├── extract_audio.py        # MF_ExtractAudio  — MediaForge/Audio (Phase 6 first node)
│   ├── load_video_frames.py    # MF_LoadVideoFrames
│   ├── loop_video.py           # MF_LoopVideo
│   ├── probe_media.py          # MF_ProbeMedia
│   ├── sample_segments.py      # MF_SampleSegments  — Timeline: H3 single-node sampler (graph expansion of core nodes)
│   ├── save_video.py           # MF_SaveVideo — serial H.264 VIDEO export
│   ├── save_video_frames.py    # MF_SaveVideoFrames
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
│   ├── h3_adapter.py        # Timeline: core MiniMax H3 node adapter + latent audio lock
│   ├── latent_carry.py      # Timeline: latent stream split, tail extract, carry masks, provenance, lock check
│   ├── output_path.py       # resolve_output_path + output_path_to_ui_entry (API metadata helper)
│   ├── plugin_tmp.py        # .mf_tmp/ temp files + sweep of stale files and session dirs
│   ├── segment_spill.py     # Timeline: immutable state, FFV1 / WAV spill parts, crossfade, lock-audio master, final encode
│   ├── serial_h264.py       # single-threaded VIDEO decode, libx264 encode and metadata
│   ├── video_batch.py       # Video Batch: graph rules, batch math, source checks, the job that re-runs the same node IDs
│   ├── timeline_materials.py# Timeline: materials / refs / anchors, geometry, per-segment manifest, H3 tags, aliases
│   ├── timeline_media.py    # Timeline: material snapshots, image / video / audio slice decoding
│   ├── timeline_messages.py # Timeline: message catalog (stable E_* / W_* keys)
│   ├── timeline_plan.py     # Timeline: time base, grids, timeline_data validation, segment specs, views, identities
│   ├── timeline_routes.py   # Timeline: /mediaforge/timeline/probe + /proxy routes for the editor
│   ├── timeline_source.py   # Timeline: input/ source resolution, safe FFmpeg input flags, fingerprint
│   └── video_io.py          # rawvideo pipe ↔ IMAGE/AUDIO + encode_tensor_to_tempfile
├── font/                    # drop .ttf / .otf here for MF_BurnSubtitle font_file dropdown
├── example_workflows/       # Timeline example workflows (shown in ComfyUI's template browser)
├── web/
│   ├── ai_config_mask.js    # frontend extension: mask MF_AIConfig api_key on the canvas
│   ├── ai_config_services.js # MF_AIConfig service and model catalog
│   ├── ai_config_ui.js      # frontend extension: MF_AIConfig service / model dropdowns
│   ├── dual_input_lock.js   # frontend extension: hide widgets per dual-input mode
│   │                        #   - lock_widget / hidden_when_connected: hide on tensor mode
│   │                        #   - linked_widgets: hide on path mode
│   ├── internal_nodes.js    # frontend extension: keep the three expansion-only nodes out of the Add Node menu
│   ├── timeline_edit.js     # editor edit commands (pure functions: create / move / trim / split, shot cut)
│   ├── timeline_editor.js   # Edit Timeline visual editor (DOM widget)
│   ├── timeline_grid.js     # grid / validation rules mirrored from utils/timeline_plan.py
│   ├── timeline_materials.js# material rules mirrored from utils/timeline_materials.py
│   ├── timeline_mention.js  # prompt material picker, caret placement and IME handling
│   └── timeline_strings.js  # editor UI text (Traditional Chinese)
└── tests/                   # dev-only pytest suite — gitignored, not in the published repo (see Testing)
    ├── test_compose_ir.py        # IR spike cases (Phase 4 prerequisite)
    ├── test_compose_e2e.py       # real-ffmpeg e2e
    ├── test_video_io_roundtrip.py# PSNR > 38 dB rawvideo roundtrip
    ├── test_timeline_*.py        # Timeline plan / materials / nodes / routes / examples / single-path guards
    ├── test_sample_segments_core.py # Sample Segments expansion + cache on the real core executor (T19)
    ├── js/*.test.mjs             # grid / material / edit-command rules vs. shared golden vectors (node --test)
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
- Timeline nodes: no extra pip packages (`psutil`, `torch`, `numpy`, `Pillow` come with ComfyUI); Encode H3 Segment / Sample Segments need core's `MiniMaxH3ReferenceToVideo` / `MiniMaxH3AddGuide` nodes; tested with ComfyUI core 0.42.5 / frontend 1.55.18 — not a minimum version (see [Compatibility](#compatibility))

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
| Translate output line count drifts | Small LLM lost track of numbered batches | Switch to a stronger model (`gpt-6.1-terra`, `llama-3.3-70b-versatile`); the prompt does line-number alignment but small models miss it on > 50-line batches |
| `reverse` filter OOM on long source | `reverse` loads the entire stream into RAM | Currently a hard limit; cut to a sub-clip first or split-and-stitch manually |

### Timeline messages

Timeline nodes report problems as `[<node>] <KEY>：<message>`. The message text is Traditional Chinese, so search by key; the table gives English translations. Inside Sample Segments the error is shown on the Sample Segments node, while `<node>` names the inner node that raised it.

| Key | Node | Message (English translation) | Fix |
|---|---|---|---|
| `E_TIMELINE_JSON` | Edit Timeline | Invalid timeline JSON: {reason}. Fix {location}. | Correct the named field (format: [Timeline prompts](#timeline-prompts)); the editor marks the same error on the segment |
| `E_H3_FPS` | Edit Timeline | Generating a new video (MiniMax H3) supports only 24 fps. Set fps to 24. | Set `fps = 24` |
| `W_SOURCE_TIME_UNKNOWN` | Edit Timeline | Can't read the source's start time; using 0. Preview and source times may be slightly offset. | Warning only; the run continues |
| `E_SOURCE_CHANGED` | Encode H3 Segment / Assemble Segments | The source video changed; rerun Edit Timeline. | The source or a material file changed after Edit Timeline ran — queue again so Edit Timeline re-reads it |
| `E_SOURCE_SHORTFALL` | Encode H3 Segment | The material video's frames do not cover the range segment {segment} uses. Shorten the reference or anchor range, or use a longer material. | The video decodes fewer frames than probed — re-encode it or use a shorter content range |
| `E_MATERIAL` | Edit Timeline | Material "{name}" can't be used: {reason}. Make sure the file is inside input/ and in a supported format. | Fix or replace the file (select the card, change its file), or delete the material |
| `E_REF` | Edit Timeline | Reference {ref} is invalid: {reason}. Adjust it on the timeline. | The reference starts past the timeline end, needs frames past the end of the material, or has `audio` on a file without audio — move or shorten it, or turn `audio` off |
| `E_ANCHOR` | Edit Timeline | Anchor {anchor} is invalid: {reason}. Adjust it on the timeline. | An anchor must start before the timeline end; a guide must lie inside one segment's output range and, with H3, be 5, 22, 39 … (17k + 5) frames long; the content must fit in the material; `audio` needs an audio track |
| `E_ANCHOR_OVERLAP` | Edit Timeline | {stream} anchors {first} and {second} overlap in time. Move them apart. | video: keyframes and guides; audio: locks and guides with `audio` on — move one of the two |
| `E_REF_LIMIT` | Edit Timeline | Segment {segment} has {count} {kind}, above the H3 limit {limit}. Narrow the reference ranges or move them to other segments. | H3 allows per segment ≤ 9 pictures, ≤ 3 videos, ≤ 3 standalone audio references |
| `E_REF_SHORT` | Edit Timeline | Reference video "{name}" in segment {segment} has only {frames} frames (at least 5). Extend the reference or move it out of this segment. | A reference video needs ≥ 5 frames inside each segment it belongs to |
| `E_PROMPT_REF` | Edit Timeline | Material reference in segment {segment}'s prompt is invalid: {reason}. | `<@name>`: no such material; the material isn't referenced in this segment (or only anchored); it has two references there — write the tag instead; `:audio` on a reference without soundtrack; a tag number above this segment's count |
| `E_H3_NODES` | Encode H3 Segment | Can't find usable core MiniMax H3 nodes ({nodes}). Update ComfyUI. | Core's `MiniMaxH3ReferenceToVideo` / `MiniMaxH3AddGuide` are missing or changed (or the audio VAE returned an unexpected shape) — update ComfyUI; tested with core 0.42.5 |
| `E_VIEW` | internal nodes | Segment format mismatch: {reason}. | An old segment — rerun Edit Timeline. Otherwise an internal node was wired by hand: use Sample Segments, which hands each one the view it accepts |
| `E_TOKENS` | Extend Latent | Latent time length mismatch: expected {expected}, got {actual}. Re-run the whole flow; if it persists, report it with your ComfyUI version. | Not reachable through Sample Segments unless core's H3 latent layout changed — report it with your ComfyUI version |
| `E_AUDIO_CARRY` | Extend Latent | This latent does not support audio carry. Turn off carry_audio. | Turn `carry_audio` off on Sample Segments (the latent has no H3 audio stream) |
| `E_CARRY_SOURCE` | Extend Latent | This segment is missing carry data. Re-run the whole flow; if it persists, report it with your ComfyUI version. | Not reachable through Sample Segments, which wires it for every overlapping segment |
| `E_MASK_CONFLICT` | Extend Latent | The carry region conflicts with an existing mask. Set on_conflict to carry_wins, or move the anchor out of the overlap with the previous segment. | As stated: `on_conflict = carry_wins` on Sample Segments, or move the keyframe / guide |
| `W_LOCK_CARRY` | Extend Latent | Segment {segment}'s lock audio lies in the carried region; skipping this segment's audio carry (video carry continues). | Warning only. Move the lock past the overlap if that seam needs audio carry |
| `E_PROVENANCE` | Extend Latent / Append Segment | Carry metadata does not match this segment ({field}). Rerun the whole workflow. | Queue again; if it persists, a custom sampler is rewriting the latent's `mf_carry` |
| `W_PROVENANCE` | Extend Latent / Append Segment | Carry metadata (the latent's mf_carry) is absent; continuing, but the carry can't be confirmed. Re-run the whole flow; if it persists, report it with your ComfyUI version. | Sample Segments preserves this marker; if a full re-run still warns, report it with your ComfyUI version |
| `E_SEQUENCE` | Append Segment / Assemble Segments | Expected segment {expected}, got segment {actual}. Rerun the whole workflow. | Queue again. Assemble needs the state Sample Segments returns |
| `E_PLAN` | Append Segment | Segment and state belong to different plans. Rerun the whole workflow. | Queue again after changing the timeline or the source |
| `E_CARRY_DRIFT` | Append Segment | The carried region changed after sampling (max diff {drift}). It must stay as it is: check the sampler and sigmas wired into Sample Segments. | Use a sampler that honours `noise_mask` |
| `E_LOCK_DRIFT` | Append Segment | Lock-audio check failed ({reason}). Make sure the sampler keeps noise_mask and the latent's mf_lock, or use a full schedule. | The sampled latent lost `mf_lock` (a node between Encode H3 Segment and Append dropped it) or the locked tokens changed — use a core sampler that honours `noise_mask`, with a complete schedule |
| `E_RAM` | Append Segment | This segment needs an estimated {gib} GiB of RAM, above the preflight threshold. Shorten the segments or lower the resolution. | As stated; the threshold is 80% of available RAM |
| `E_DISK` | Append Segment | Not enough temporary space; at least {gib} GiB is needed. Free up space and retry. | Free space on the drive that holds the plugin's `.mf_tmp/` |
| `E_AUDIO_LAYOUT` | Append Segment | All segments must agree on audio presence, sample rate and channels. Unify the audio format upstream. | Give every segment audio from the same source / VAE, or none |
| `W_AUDIO_PAD` | Append Segment / Encode H3 Segment / Assemble Segments | Inserted {samples} samples of silence at a seam or the end. | Append: informational (H3 runs pad the final tail, e.g. 267 samples). Encode H3 Segment: the material's audio ends earlier than its probed length — re-encode the file or use a shorter range |
| `E_SPILL_MISSING` | Assemble Segments | Segment spill files no longer exist. Rerun the whole workflow, then assemble. | The parts were deleted (e.g. `.mf_tmp/` cleaned up, or a cache eviction); queue again |
| `W_PROXY` | timeline editor | Preview creation failed; the timeline is still editable. Retry. | The editor shows 「預覽失敗（時間軸仍可編輯）」 with a 重試 (Retry) button; editing works without the preview |

### Video Batch messages

Video Batch nodes report problems as `[Start Video Batch] …` or `[End Video Batch] …`. The text is Traditional Chinese; the table gives what it means.

| Message (English translation) | Fix |
|---|---|
| Start needs an End Video Batch | Add End and link the processed frames to its `frames` |
| A workflow can only have one End Video Batch | Delete the extra one |
| A Start isn't connected to End | Link it or delete it |
| `frames` must come from Start | Link End's `frames` to Start, through your nodes if you have any |
| A node after End can't read frames inside the batch | That node would get only one batch. Link it to `final_video_path` |
| No Start / End Loop inside the batch | Move the loop outside |
| A Start input can't depend on a node inside the batch | Use a source outside the batch |
| A setting on End can't be linked | Set it on the node |
| `frames_per_batch` must be at least 1; `overlap_frames` must be between 0 and half of it | Change the value |
| With MiniMax H3 in the batch, `frames_per_batch` must be 17n+5 | Use a value it suggests, such as 124 |
| No video selected; video not found | Upload or pick one, or link `video_path` |
| The format isn't verified for batch decoding (TS, MPG) | Convert: `ffmpeg -i in.ts -c copy out.mkv` |
| HDR or wide-gamut video isn't supported yet | Convert to SDR / BT.709 first |
| Can't find the video's start time; can't find its fps | Convert the file; for fps, set `fps` on End |
| A video is under 5 fps while the others are faster | Usually a wrong fps upstream, such as Loop Video's `tensor_fps`. If you mean it, set `fps` on End |
| This batch needs X GiB of RAM (Start, before decoding) | Lower `frames_per_batch`, or shrink the frames with `resize` |
| No frames from frame N | Check that the video isn't damaged or empty |
| The source changed during the job | Don't replace the file while it runs; run again |
| Batch N got no frames; batch N was blocked by a node upstream | Check the node feeding `frames`. A batch can't be skipped |
| `frames` must be `[frames, height, width, 3]` RGB; must be 0–1 float | Convert the output, for example drop alpha |
| Batch N went in with B frames and came out with M | The node changes the frame count. Move it after End |
| Frames contain NaN or infinity | The node upstream probably failed |
| Batch N has only L frames, not more than the overlap | The source changed during the job; run again |
| `audio` must be a single track; `audio` was blocked; `audio` is empty | Link one track, or unlink `audio` |
| Batch N's audio has a different sample rate or channel count | Make every batch's audio the same format |
| The source has no audio, so `audio` can't be used | Unlink Start's `audio`, or use a video with audio |
| Two sources have audio | Link the one you want to End's `audio`, or turn off `keep_source_audio` |
| Odd audio format; the track is too big (temp WAV limit about 4 GiB); not enough RAM to decode it | Convert it to 48 kHz stereo: `ffmpeg -i video -c:v copy -ac 2 -ar 48000 -c:a aac out.mp4`, or turn off `keep_source_audio` |
| The frames are W×H and this codec needs even sizes | Change your node's scale, or add a resize before End |
| The trial encode failed with this codec | Pick another codec, such as `h264 (libx264)` |
| The trial encode didn't finish in 60 seconds | The GPU may be busy. Run again, or pick `h264 (libx264)` |
| The disk may not have enough room | Free some space, or use `max_frames` or `resize` |
| After your nodes, each batch needs X GiB of RAM | Lower `frames_per_batch`, or shrink the frames with `resize` |
| This ComfyUI version didn't pass the compatibility check | The job depends on core internals, verified on core 0.42.5. Update MediaForge or tell us your version |

## FAQ

**Q: Why not use VideoHelperSuite for everything?**
A: VHS is excellent for IMAGE-batch workflows. MediaForge handles **file-level** operations (cross-codec concat, audio-aware trim, broadcast encoders) and **single-encode multi-overlay** Compose. Install both.

**Q: Will you add diffusion / generation nodes?**
A: No model loaders or model patches, and no sampling code of our own — by design; generation stays in ComfyUI core / dedicated plugins. MediaForge ships post-production AI (Whisper transcribe, LLM translate) and the Timeline tools: Extend Latent reshapes the `LATENT` you pass in, Append decodes it with the `VAE` you connect, and FFmpeg does the encoding. For MiniMax H3, Encode H3 Segment builds conditioning only through core's `MiniMaxH3*` nodes, and Sample Segments samples only by expanding into core node classes (`RandomNoise`, `BasicGuider` / `CFGGuider`, `SamplerCustomAdvanced`) fed by your own model, sampler and sigmas (see [Timeline & Long Video](#timeline--long-video)).

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

Timeline tests (`test_timeline_*.py`, `test_latent_carry.py`, `test_segment_spill.py`, `test_h3_adapter_core.py`, `test_sample_segments_core.py`) are developer-local like the rest of `tests/`. The real-core tests `tests/test_h3_adapter_core.py` and `tests/test_sample_segments_core.py` must run inside the ComfyUI Linux / WSL environment, because they use ComfyUI's interpreter and core classes (the cache tests spawn `main.py --cpu` instances). The JS tests need Node.js:

Video Batch tests: `test_video_batch_plan.py` (rules and batch math), `test_video_batch_decode.py` (each batch window against a continuous decode; run it on every FFmpeg version you support), `test_video_batch_core.py` (the real core executor in five cache modes; needs ComfyUI, set `COMFYUI_ROOT`), `test_video_io_streaming.py` (peak memory of Load / Save Video Frames).

Save Video tests: `test_serial_h264.py` (paths, metadata, the encoded files checked with ffprobe and a full decode, cancel and failure, same-prefix runs in parallel, thread counts; runs on Linux and Windows), `test_save_video_core.py` (the real core executor, plus a server with `--disable-metadata`; needs ComfyUI, set `COMFYUI_ROOT`).

```bash
python -m pytest -q tests/test_h3_adapter_core.py tests/test_sample_segments_core.py   # inside ComfyUI's Linux / WSL environment
node --test tests/js/timeline_grid.test.mjs tests/js/timeline_edit.test.mjs
```

## Roadmap

- **Phase 2** ✅ Foundation bridges — LoadVideoFrames / SaveVideoFrames
- **Phase 3** ✅ Gap-priority — DetectSilence / TrimByRanges / ConcatVideos
- **Phase 4** ✅ Compose pipeline v1 — single-encode multi-overlay (Start + Finalize)
- **Phase 4.5** ✅ Compose pipeline v2 — merged ComposeVideo + subtitle-in-chain + audio chain (Volume / AudioMix / Fade / Normalize)
- **Phase 5** 🚧 AI — WhisperTranscribe / TranslateSubtitle (experimental schema)
- **Phase 6** 🚧 Standalone Audio domain — `MF_ExtractAudio` ships first; file-level denoise, normalize-file, audio cut/trim, ducking still planned (the Compose chain audio ops in Phase 4.5 are a subset)
- **Timeline** ✅ Timeline & long video — Edit Timeline editor, latent tail-carry, spill-to-disk assembly (MiniMax H3 AV; the loop wiring and the LTX-2 / Wan 2.2 experiments were removed in 0.13)
- **Timeline v2** ✅ Director — material library, references / keyframes / guides / lock audio, single-node H3 Sample Segments (GPU-verified with H3 turbo 4-step, see [Compatibility](#compatibility))
- **Phase 7** ⏳ Net domain — yt-dlp ingest, HTTP fetch (lazy-import)

## License

MIT — © YingLiang Lu (leon80148).

## Acknowledgments

- **FFmpeg** — the actual workhorse; this plugin is 90% argument formatting
- **ComfyUI** — the runtime + node graph engine
- **VideoHelperSuite** — the prior art that defined the IMAGE-batch contracts MediaForge interoperates with
- **faster-whisper / CTranslate2** — local Whisper backend
- Timeline planning, the material timeline and latent tail-carry concepts were inspired by [ComfyUI MiniMax H3 Timeline Director](https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector); MediaForge implements these features independently, reuses no code from that project, and remains MIT-licensed.
- 💾 Save Video · Serial H.264 follows the export behaviour of the serial video saver in [MiniMax H3 Audio T8](https://github.com/T8mars/comfyui-minimax-h3-audio-T8); it was written from a behaviour spec, reuses no code from that project, and is MIT-licensed like the rest of MediaForge.
