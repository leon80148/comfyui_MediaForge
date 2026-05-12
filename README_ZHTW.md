# MediaForge

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg) ![License MIT](https://img.shields.io/badge/license-MIT-green.svg) ![ComfyUI](https://img.shields.io/badge/ComfyUI-custom__nodes-orange.svg)

> **給進階使用者的 ComfyUI FFmpeg 工具集。** Tensor↔影音橋接，帶廣播級編碼控制。深度 > 廣度。

FFmpeg 驅動的 custom_nodes plugin：字幕燒入、影片循環、媒體 probe、tensor-native 影格 I/O、靜音偵測、多片段拼接、單次編碼多層 overlay 的 Compose pipeline，以及 provider-agnostic 的 AI 字幕／翻譯。**所有節點都是 FFmpeg 的薄包裝，不混 GPU / AI 模型推論**。AI 節點靠 `MF_AIConfig` 連線傳遞 provider 設定，一處切換即可整批換 backend。

📖 **[English README →](README.md)**

## 亮點

- 🎞️ **17 個節點** 分散在 6 個分類 — Subtitle / Video / Analysis / Compose / AI（Audio / Net / Image 規劃中）
- 🔗 **Dual-input bridge** — file-consumer node 同時接受 `video_path` 字串 *或* in-memory 的 `IMAGE + AUDIO + fps` 三件套，VHS / AnimateDiff / 任意 IMAGE-pipeline plugin 都能直接 wire 進 MediaForge、不必 SaveVideoFrames 來回 round-trip
- 🧪 **廣播級編碼控制** — H.264 / HEVC / AV1 / ProRes，支援 CRF / bitrate / target-size 三種編碼模式
- 🎚️ **單次編碼、多層 overlay 的 Compose pipeline** — `filter_complex` graph 編譯器，N 層 overlay 仍只走一次 re-encode
- 🤖 **Provider-agnostic AI** — `MF_AIConfig` 讓 Whisper / Translate 在 OpenAI / Groq / Ollama / 本地 backend 之間一處切換
- 🪶 **零硬性 Python 相依** — 只要 PATH 有 `ffmpeg` + `ffprobe`。`requests` / `faster-whisper` 用到才 lazy import
- 🔁 **Tensor-native** — `IMAGE [B,H,W,C] float32` 跟 ComfyUI canonical 的 `AUDIO` dict；rawvideo roundtrip PSNR > 38 dB

## 目錄

1. [Quick Start](#quick-start)
2. [為什麼選這個 plugin（vs VideoHelperSuite）](#為什麼選這個-pluginvs-videohelpersuite)
3. [節點清單（16）](#節點清單16)
4. [AI Provider Recipes](#ai-provider-recipes)
5. [Hidden Contracts（內部型別契約）](#hidden-contracts內部型別契約)
6. [Architecture](#architecture)
7. [系統需求](#系統需求)
8. [安裝](#安裝)
9. [疑難排解](#疑難排解)
10. [常見問題](#常見問題)
11. [測試](#測試)
12. [Roadmap](#roadmap)
13. [License & 致謝](#license)

## Quick Start

三個逐漸複雜的最小 workflow。每個都以節點鏈描述 — 在 ComfyUI 裡照圖接線即可。

### 1. 自動裁靜默（3 個節點，最簡單）

從演講錄影或 podcast 抽掉死氣沉沉的空白片段。

```
[LoadVideoFrames]──video_path──▶[DetectSilence]──SILENCE_RANGES──▶[TrimByRanges]──▶ output.mp4
                                                                       ▲
                                                                       │ mode=remove
```

| 節點 | 關鍵設定 |
|---|---|
| `MF_LoadVideoFrames` | `video_path = "lecture.mp4"` |
| `MF_DetectSilence` | `noise_db = -30`, `min_duration_sec = 1.5` |
| `MF_TrimByRanges` | `mode = "remove"` |

### 2. Compose 聯合：浮水印 + 開場文字，**只走一次** re-encode（5 個節點）

```
[ComposeStart]──▶[ComposeWatermark]──▶[ComposeOverlayText]──▶[ComposeFinalize]──▶ output.mp4
```

| 節點 | 關鍵設定 |
|---|---|
| `MF_ComposeStart` | `video_path = "clip.mp4"`, `target_width = 1920`, `target_height = 1080` |
| `MF_ComposeWatermark` | `image_path = "logo.png"`, `placement = "BR"`, `relative_scale = 0.12`, `opacity = 0.6` |
| `MF_ComposeOverlayText` | `text = "Episode 01"`, `font_size = 64`, `start_sec = 0`, `end_sec = 5` |
| `MF_ComposeFinalize` | `encoder = "h264"`, `crf = 20` |

所有 overlay 操作累積到 Compose IR、最後編譯成**一份** `filter_complex_script` — 輸入只 decode 一次、輸出只 encode 一次。疊 10 層 overlay 也只付一次 re-encode 成本。

### 3. AI 自動字幕：轉錄 → 翻譯 → 燒入（6 個節點，最進階）

```
[AIConfig (ASR)]──▶[WhisperTranscribe]──srt──▶[TranslateSubtitle]──srt──▶[BurnSubtitle]──▶ output.mp4
[AIConfig (LLM)]─────────────────────────────────▲
```

| 節點 | 關鍵設定 |
|---|---|
| `MF_AIConfig` (ASR) | `provider = "openai"`, `base_url = "https://api.groq.com/openai/v1"`, `model = "whisper-large-v3"` |
| `MF_WhisperTranscribe` | `audio_path = "interview.mp4"`, `backend = "openai_compatible"` |
| `MF_AIConfig` (LLM) | `provider = "openai"`, `base_url = "https://api.openai.com/v1"`, `model = "gpt-4o-mini"` |
| `MF_TranslateSubtitle` | `target_language = "繁體中文"` |
| `MF_BurnSubtitle` | 接 SRT path，設定 ASS 字型、顏色、外框 |

具體 `base_url` / `model` 組合可直接複貼 — 見下方 [AI Provider Recipes](#ai-provider-recipes)。

## 為什麼選這個 plugin（vs VideoHelperSuite）

| 能力 | VHS | MediaForge | 註 |
|---|---|---|---|
| 影片載入 → IMAGE batch | ✅ opencv | ✅ FFmpeg | MediaForge 可處理 AV1 / HEVC 10-bit / ProRes / VP9 / 任意 colorspace |
| IMAGE batch → 影片儲存 | ✅ 只 H.264 | ✅ H.264 / HEVC / AV1 / ProRes | 加上 CRF / bitrate / target-size 模式 |
| 音訊 in/out（canonical dict） | ⚠️ 有限 | ✅ 雙向 | `{'waveform': Tensor[B,C,T], 'sample_rate': int}` |
| 按 ranges 裁切 | ✅ image-batch | ✅ 影片 + image-batch | MediaForge 直接吃 `MF_DetectSilence` 輸出 |
| **路徑級影片拼接（跨 codec、含音軌）** | **❌** | **✅** | MediaForge 獨有 — VHS Combine 只能拼 IMAGE batch |
| 靜音偵測 | ❌ | ✅ | |
| 字幕燒入 | ❌ | ✅ | |
| 多層 overlay Compose pipeline（單次 re-encode） | ❌ | ✅ | filter_complex graph 編譯器 |
| 浮水印預設（透明度／邊距／時間窗／擺位） | ❌ | ✅ | |
| AI 字幕（轉錄 + 翻譯） | ❌ | ✅ | provider-agnostic |
| ffprobe metadata | ⚠️ 部分 | ✅ | |

**結論**：兩個都裝。VHS 用於快速 IMAGE batch workflow；MediaForge 用於廣播級編碼、檔案級操作、Compose pipeline、AI 字幕。

## 節點清單（17）

> **Dual-input 註記**：下列標 **(dual-input)** 的 node 同時接受兩種 input：(a) 既有的 `video_path` STRING 欄位，(b) 新加的 `frames` + `fps` + `audio` 三件套 optional 輸入。連 tensor 時 MediaForge 內部會寫一個 temp .mp4 給 FFmpeg 吃。Path 模式仍是預設的 fast path — MediaForge 之間串接時不會被迫多走一次無謂的 decode/encode。

### `MediaForge/Subtitle`

#### 🔥 Burn Subtitle (`MF_BurnSubtitle`) **(dual-input)**

SRT → 硬燒字幕，完整 ASS 風格控制。顏色輸入 `#RRGGBB`，內部轉成 ASS BGR-with-alpha。

可調樣式：
- **字型**：`font_name` (Family Name STRING) + 可選 `font_file` dropdown 讀 `<plugin>/font/*.ttf|.otf`。選 `font_file` 時，MediaForge lazy-import `fontTools` 自動讀 TTF 內部的 Family Name — 丟一個 TTF 進 `font/` 就直接能用，不必再去查字型內部叫什麼名字。沒裝 `fontTools` 時退用你打的 `font_name`。
- **粗細**：`bold` + `italic` boolean。
- **位置**：`alignment` dropdown（9 個命名位置：`bottom_center (2)`、`top_right (9)`…）+ `margin_v` + `margin_l` + `margin_r`。字幕實際可用寬 = 播放區寬 − `margin_l` − `margin_r`。
- **字距**：`letter_spacing` FLOAT（ASS Spacing，像素為單位）做緊湊或寬鬆的視覺。
- **外框 / 陰影 / 底色塊**：`outline_color_hex` + `outline_width` + `shadow_depth` + `border_style`（1=outline 描邊、3=box 半透明底色配 `back_color_hex`）。

### `MediaForge/Video`

#### 📂 Select Video (`MF_SelectVideo`)

`ComfyUI/input/` 的影片檔 dropdown picker。會遞迴掃子目錄、列 `.mp4 / .mov / .mkv / .webm / .avi / .m4v / .mpg / .mpeg / .ts`。輸出 `STRING video_path` — 直接 wire 給任何 file-consumer node。`IS_CHANGED` 用 file mtime 做 cache key，同檔名換內容也會自動 invalidate 下游 cache。

#### 🔁 Loop Video (`MF_LoopVideo`) **(dual-input)**

循環至目標時長，支援 `strict` / `ping_pong` / `crossfade` 模式，可加速、可反向。`xfade` chain 上限 50 圈；`crossfade_sec >= 有效片段長度` 自動退階回 `strict`（合理退階，不報錯）。FFmpeg `loop` filter 的 frame 緩衝上限是 `MAX_LOOP_FRAMES = 32767`（INT16），超長素材高 fps 要改用 `crossfade` mode。

#### 📥 Load Video Frames (`MF_LoadVideoFrames`)

FFmpeg decode 任意容器／codec → `IMAGE` batch `[B,H,W,C] float32 [0,1]` + `AUDIO` dict（`{'waveform': Tensor[B,C,T], 'sample_rate': int}`）+ fps + metadata JSON。**旋轉感知**（直拍手機影片會正確顯示），記憶體有上界。可選 `target_fps` 重採樣、`max_frames` 截斷。

#### 📤 Save Video Frames (`MF_SaveVideoFrames`)

`IMAGE` batch + 可選 `AUDIO` dict → H.264 / HEVC / AV1 / ProRes 檔，三種編碼模式：**CRF（預設）** / bitrate / target-size。容器副檔名自動修正（ProRes → `.mov`）。

#### ✂️ Trim by Ranges (`MF_TrimByRanges`) **(dual-input)**

吃 `SILENCE_RANGES`（從 `MF_DetectSilence`）或原始 JSON `[[s,e],...]`。Mode：`keep` / `remove`。Seam 處可走 xfade chain 做接縫淡入淡出；音影 interleaved concat；empty list 視為 identity（no-op）。

#### 🔗 Concat Videos (`MF_ConcatVideos`) **(dual-input, prepend 語意)**

多檔路徑級拼接。`copy` mode → FFmpeg concat demuxer（同 codec 快路徑）。`transcode` mode → filter_complex 加可選 `xfade` 過場（`fade` / `wipeleft` / `wiperight` / `slideleft` / `slideright` / `circleopen` / `circleclose` / `dissolve`）。沒音軌的輸入自動補 `anullsrc` 靜音。`frames` 連線時 tensor 寫成 path[0] (prepend)、`video_paths` 列表 shift 到 path[1..N] — 需要至少 1 條 path 才能湊到 2 段才能 concat。

### `MediaForge/Analysis`

#### 🔍 Probe Media (`MF_ProbeMedia`) **(dual-input)**

ffprobe 拿到時長、尺寸、fps、影／音 codec。回傳的是**影片串流的時長**（跟容器時長可能不一樣 — 故意分開）。

#### 🤫 Detect Silence (`MF_DetectSilence`)

FFmpeg `silencedetect` 包裝 → `SILENCE_RANGES` list（`[[start_sec, end_sec], ...]`）。`noise_db` 閾值與 `min_duration_sec` 可調。配 `MF_TrimByRanges` 可做演講縮時／podcast 預剪／直播精華 workflow。

### `MediaForge/Compose` — 單次編碼多層 overlay pipeline

`MF_Compose*` 系列串接 `MF_COMPOSE` IR（FFmpeg `filter_complex` graph 編譯器）。只有 `MF_ComposeFinalize` 會跑 ffmpeg — 所有中間操作累積進 IR，最後編譯成一份 `filter_complex_script`。**多層 overlay 無損疊加**，不再 N 次 re-encode。

#### 🎬 Compose Start (`MF_ComposeStart`) **(dual-input)**
初始化 IR，設 `target_width / target_height / target_fps`。連 `frames` 時 temp .mp4 在這裡產生，但清理延後到 `MF_ComposeFinalize`（透過 `ComposeIR.tmp_paths_to_cleanup`）— 這樣 temp 檔能活過整條 Compose chain。

#### ✏️ Compose Overlay Text (`MF_ComposeOverlayText`)
追加 `drawtext` 操作。`start_sec`/`end_sec` 走 enable expression 控時間窗。支援自訂 `fontfile`。文字透過 `textfile=` 傳入，安全處理單引號、百分號、換行等難 escape 字元。

#### 🖼️ Compose Overlay Image (`MF_ComposeOverlayImage`)
追加通用 `overlay` 操作。可選 `scale_w` 縮放；可加時間窗。

#### 💧 Compose Watermark (`MF_ComposeWatermark`)
完整 UX 的浮水印 preset：`placement`（TL/TR/BL/BR/center/tile）、`relative_scale`（畫面寬度的 0.05–0.5）、`opacity`（走 colorchannelmixer alpha）、各邊邊距、`visible_start/end_sec` 時間窗。

#### ✅ Compose Finalize (`MF_ComposeFinalize`)
IR 編譯 → 單次 FFmpeg encode（H.264 / HEVC / AV1 / ProRes）。回傳輸出路徑 + 編譯後的 `filter_complex_script`（debug 用）。超過 6000 字元自動切到 `-filter_complex_script <tempfile>`。

### `MediaForge/AI` — provider-agnostic

Schema 標記為 **experimental** — `MF_AI_CONFIG` API 在 Phase 5 內可能改，直到 Whisper / Translate 在 4 種 provider 都 e2e 驗證完才會凍結。

#### ⚙️ AI Config (`MF_AIConfig`)
輸出 `AI_CONFIG` dict（`provider` / `base_url` / `api_key` / `model` / `device` / `extra`）。所有 AI 節點吃這個 — 一處切 provider 整批換。

#### 🗣️ Whisper Transcribe (`MF_WhisperTranscribe`)
音訊路徑或 `AUDIO` dict → SRT 文字。兩種 backend：
- `openai_compatible`（任意 `/v1/audio/transcriptions` endpoint — OpenAI、Groq、本地 OpenAI-compat server）
- `faster_whisper_local`（lazy import `faster-whisper`，本機 CPU/CUDA 跑）

#### 🌐 Translate Subtitle (`MF_TranslateSubtitle`)
SRT + 目標語言 → 翻譯後 SRT（時間戳保留）。走 `/v1/chat/completions`，prompt 帶批次行號對齊。

## AI Provider Recipes

`MF_AIConfig` 輸出的 dict 由所有 AI 節點消費。同一個 `provider` / `base_url` / `api_key` / `model` 介面 — 只是值換。可直接複貼的組合：

### OpenAI（官方）

```
provider   = openai
base_url   = https://api.openai.com/v1
api_key    = sk-...
model      = whisper-1         # 給 MF_WhisperTranscribe
           = gpt-4o-mini       # 給 MF_TranslateSubtitle
```

要錢；最穩定。`whisper-1` 是多語 GA endpoint。

### Groq（市場最快的 hosted Whisper）

```
provider   = openai            # OpenAI 相容 API 介面
base_url   = https://api.groq.com/openai/v1
api_key    = gsk_...
model      = whisper-large-v3  # ASR — 同模型家族下比 OpenAI whisper-1 快 ~5–10 倍
           = llama-3.3-70b-versatile  # 翻譯
```

有免費額度（受限速）；要在 1 分鐘內字幕 1 小時 podcast 時就靠它。

### faster-whisper（本地，不需要 API key）

```
backend    = faster_whisper_local      # 設在 MF_WhisperTranscribe
device     = cuda                      # 或 "cpu", "auto"
model      = large-v3                  # 首次使用會下載到 HF cache
```

要先 `pip install faster-whisper`。隱私 / 離線 workflow 首選。CPU 可跑但慢（現代筆電大約 real-time × 0.3）；非小檔建議 CUDA。

### Ollama / LM Studio（本地 OpenAI 相容）

```
provider   = openai
base_url   = http://localhost:11434/v1     # Ollama
           = http://localhost:1234/v1      # LM Studio
api_key    = ollama                         # 任意非空字串，不會驗
model      = llama3.2                       # 你本地 pull 過的任何 model
```

**只能拿來翻譯** — Ollama / LM Studio 目前都不開 Whisper endpoint。要全離線就配 `faster_whisper_local` 做轉錄 + 本地 LLM 翻譯。

## Hidden Contracts（內部型別契約）

- **IMAGE**: `torch.Tensor [B, H, W, C], float32, [0, 1]`
- **AUDIO**: `{'waveform': torch.Tensor [B, C, T], 'sample_rate': int}`（ComfyUI core canonical）
- **SILENCE_RANGES**: `list[[float, float]]` — `[start_sec, end_sec]` 配對
- **MF_COMPOSE**: `ComposeIR` dataclass（見 `utils/compose_ir.py`）。Phase 4 後 schema 凍結 — 只能加新欄位，不能改既有。
- **AI_CONFIG**: `dict`，keys 為 `provider / base_url / api_key / model / device / extra`。Experimental。

## Architecture

```
comfyui_MediaForge/
├── __init__.py              # pkgutil 自動發現 nodes/ — 丟檔進去就出現
├── pyproject.toml
├── requirements.txt         # 故意留空 — 選用相依走 lazy import
├── nodes/                   # 一節點一檔，類別命名 MF_<Verb><Noun>
│   ├── ai_config.py            # MF_AIConfig
│   ├── burn_subtitle.py        # MF_BurnSubtitle  — 使用 font/ 子目錄
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
│   ├── select_video.py         # MF_SelectVideo  — input/ 影片 dropdown picker
│   ├── translate_subtitle.py   # MF_TranslateSubtitle
│   ├── trim_by_ranges.py       # MF_TrimByRanges
│   └── whisper_transcribe.py   # MF_WhisperTranscribe
├── utils/
│   ├── color.py             # hex_to_ass_color：#RRGGBB → ASS BGR+alpha
│   ├── compose_ir.py        # ComposeIR + compile_ir() + tmp_paths_to_cleanup hook
│   ├── ffmpeg.py            # ensure_ffmpeg / run_ffmpeg / probe / escape_filter_path
│   └── video_io.py          # rawvideo pipe ↔ IMAGE/AUDIO + encode_tensor_to_tempfile
├── font/                    # 丟 .ttf / .otf 進來給 MF_BurnSubtitle 的 font_file dropdown
└── tests/                   # 58 個測試，pytest 跑
    ├── test_compose_ir.py        # 8 個 IR spike case（Phase 4 prerequisite）
    ├── test_compose_e2e.py       # 3 個 real-ffmpeg e2e
    ├── test_video_io_roundtrip.py# 5 個 PSNR > 38 dB rawvideo roundtrip
    └── test_codex_r*_fixes.py    # 10 輪 codex review 留下的 42 個回歸測試
```

**加新節點**：丟一個 `nodes/<verb>_<noun>.py`，內含 `MF_<Verb><Noun>` 類別 + `NODE_CLASS_MAPPINGS` / `NODE_DISPLAY_NAME_MAPPINGS`。Aggregator 會自動撿到 — 重啟 ComfyUI 即可。

> ⚠️ **要知道的 silent failure**：module-level import 錯誤會讓節點**靜靜地**不出現在 menu。新節點沒出現的第一步檢查：`python -c "from custom_nodes.comfyui_MediaForge.nodes.<your_file> import *"` 看真正的 traceback。選用相依（`requests`、`faster-whisper`、`yt-dlp`）**必須** lazy import 在 FUNCTION method 裡，不能放 module top。

## 系統需求

- ComfyUI
- Python ≥ 3.10
- **PATH 上要有 FFmpeg + FFprobe**
- 選用：`requests`（任何打 HTTP AI provider 的節點）、`faster-whisper`（本地 Whisper backend） — 第一次用到才會 lazy import

FFmpeg 安裝：
- **Windows**：https://www.gyan.dev/ffmpeg/builds/（essentials build） — 解壓後把 `bin/` 加進 PATH
- **macOS**：`brew install ffmpeg`
- **Linux**：`apt install ffmpeg` / `dnf install ffmpeg` / `pacman -S ffmpeg`

驗證：`ffmpeg -version && ffprobe -version` 兩個都印出版本即可。

## 安裝

### 透過 ComfyUI Manager（推薦）

1. 打開 ComfyUI Manager → "Install Custom Nodes"
2. 搜尋 **"MediaForge"** → Install → 重啟 ComfyUI

### 手動 git clone

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/leon80148/comfyui_MediaForge.git
```

重啟 ComfyUI — 節點會出現在 `MediaForge/Subtitle | Video | Compose | AI | Analysis`。

### 選用相依（lazy — 你會用到才裝）

```bash
pip install requests          # 任何打 HTTP AI provider 的節點
pip install faster-whisper    # 只給 backend="faster_whisper_local" 用
```

## 疑難排解

| 症狀 | 可能原因 | 修法 |
|---|---|---|
| 重啟後 menu 找不到節點 | Module-level import 錯（ComfyUI 會吞掉訊息） | `python -c "from custom_nodes.comfyui_MediaForge.nodes.<file> import *"` 把真正的 traceback 逼出來 |
| `RuntimeError: ffmpeg ... failed` | FFmpeg exit code 非 0 | 往上滑 — raise 前面已經印了 FFmpeg stderr 最後 30 行 |
| FFmpeg 在 filter 階段抱怨路徑裡的 `:`（Windows） | 路徑沒 escape 就進 filter graph | 我們會自動走 `escape_filter_path()` — 只有自製節點繞過它才會中招 |
| Whisper local backend 超慢 | CTranslate2 在 CPU 上推論 | 在 `MF_AIConfig` 設 `device = "cuda"`；或改 hosted backend（Groq 比本地 CPU 快非常多） |
| `loop` filter 在超長素材報錯 | Frame 緩衝上限是 `MAX_LOOP_FRAMES = 32767`（INT16） | 降低 fps，或改 `crossfade` mode（走 xfade chain，沒有單一緩衝上限） |
| 翻譯輸出行數對不上原文 | 小 LLM 把編號批次搞混了 | 換大模型（`gpt-4o`、`llama-3.3-70b-versatile`）；prompt 已加行號對齊，但小模型超過 50 行還是會漏 |
| `reverse` filter 在長片爆記憶體 | `reverse` 會把整段串流載入 RAM | 目前是硬上限；先裁短或自己 split-and-stitch |

## 常見問題

**Q：為什麼不全用 VideoHelperSuite 就好？**
A：VHS 在 IMAGE-batch workflow 很強。MediaForge 補的是**檔案級**操作（跨 codec 拼接、音訊感知裁切、廣播級編碼器）和**單次編碼多層 overlay** Compose。兩個一起裝。

**Q：會加 diffusion / 生成節點嗎？**
A：不會 — 這是設計決策。MediaForge 是 FFmpeg side 的工具集。AI 生成屬於 ComfyUI 核心 / 專門 plugin。我們只做後製端 AI（Whisper 轉錄、LLM 翻譯）。

**Q：為什麼 AI 節點標 "experimental"？**
A：`AI_CONFIG` schema 在 Phase 5 shakedown 期間可能會改。等到 Whisper + Translate 在 4 種 provider 都 e2e 驗證完，schema 才會凍結。

**Q：可以用自己編的 FFmpeg（例如帶額外 codec）嗎？**
A：可以 — 我們直接呼叫 `PATH` 上的 `ffmpeg` / `ffprobe`。要覆蓋系統版本，把你的版本放到 `PATH` 較前面即可。

**Q：在 Apple Silicon 上跑得起來嗎？**
A：可以 — FFmpeg + `faster-whisper` 都支援 M1/M2/M3。faster-whisper 沒直接支援 Metal，但 CTranslate2 INT8 量化讓 CPU 也堪用。要 hosted ASR 直接套 Groq recipe 完全不用改。

**Q：怎麼跟沒裝 MediaForge 的人分享 workflow？**
A：對方會看到 "Missing nodes" 警告。叫他開 ComfyUI Manager → "Install Missing Custom Nodes" 即可。ComfyUI 標準流程。

**Q：為什麼還沒有 Audio domain 節點？**
A：Phase 6 — AI shakedown 之後做。會涵蓋去噪、normalize、混音、ducking。`MediaForge/Audio` 分類名稱已保留。

## 測試

Repo 內含 **58 個測試**，覆蓋 IR 編譯、real-ffmpeg roundtrip、10 輪 codex review 留下的回歸測試：

```bash
cd ComfyUI/custom_nodes/comfyui_MediaForge
pip install pytest
python -m pytest tests/                      # 跑全部
python -m pytest tests/test_compose_ir.py    # 只跑 IR spike
python -m pytest tests/ -k "video_io"        # 只跑 rawvideo roundtrip
```

real-ffmpeg 系列需要 PATH 上有 `ffmpeg`。

## Roadmap

- **Phase 2** ✅ Foundation bridges — LoadVideoFrames / SaveVideoFrames
- **Phase 3** ✅ Gap-priority — DetectSilence / TrimByRanges / ConcatVideos
- **Phase 4** ✅ Compose pipeline — single-encode multi-overlay
- **Phase 5** 🚧 AI — WhisperTranscribe / TranslateSubtitle（schema 仍 experimental）
- **Phase 6** ⏳ Audio domain — 去噪、normalize、混音、ducking
- **Phase 7** ⏳ Net domain — yt-dlp ingest、HTTP fetch（lazy import）

## License

MIT — © YingLiang Lu (leon80148)。

## 致謝

- **FFmpeg** — 真正幹活的引擎；這個 plugin 90% 是在格式化 argument
- **ComfyUI** — runtime 跟節點圖引擎
- **VideoHelperSuite** — 先行作品，定義了 IMAGE-batch 契約，MediaForge 與其互通
- **faster-whisper / CTranslate2** — 本地 Whisper backend
