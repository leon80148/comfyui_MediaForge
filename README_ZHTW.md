# MediaForge

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg) ![License MIT](https://img.shields.io/badge/license-MIT-green.svg) ![ComfyUI](https://img.shields.io/badge/ComfyUI-custom__nodes-orange.svg)

> **給進階使用者的 ComfyUI FFmpeg 工具集。** Tensor↔影音橋接，帶廣播級編碼控制。深度 > 廣度。

FFmpeg 驅動的 custom_nodes plugin：字幕燒入、影片循環、媒體 probe、tensor-native 影格 I/O、靜音偵測、多片段拼接、單次編碼多層 overlay 的 Compose pipeline，以及 provider-agnostic 的 AI 字幕／翻譯。**所有節點都是 FFmpeg 的薄包裝，不混 GPU / AI 模型推論**。AI 節點靠 `MF_AIConfig` 連線傳遞 provider 設定，一處切換即可整批換 backend。

📖 **[English README →](README.md)**

## 亮點

- 🎞️ **22 個節點** 分散在 5 個分類 — Subtitle / Video / Analysis / Compose（含 audio chain）/ AI（standalone Audio / Net / Image 規劃中）
- 🔗 **Dual-input bridge** — file-consumer node 同時接受 `video_path` 字串 *或* in-memory 的 `IMAGE + AUDIO + tensor_fps` 三件套，VHS / AnimateDiff / 任意 IMAGE-pipeline plugin 都能直接 wire 進 MediaForge、不必 SaveVideoFrames 來回 round-trip
- 🚀 **智慧 GPU codec 預設** — 偵測到 NVENC 自動用 `h264_nvenc`，沒 GPU 的機器自動 fallback `libx264`。不必手動切、不會在沒卡的環境壞掉
- 📡 **API-ready 輸出** — 每個產出檔案的節點都同時 emit ComfyUI `ui.images` metadata，`/history/<prompt_id>` 直接看到輸出檔名，`/view?filename=X&subfolder=Y&type=output` 可下載（見 [Using via API](#using-via-api)）
- 🎙️ **內建音訊混音** — BurnSubtitle 的 `keep_source_audio`（預設開）用 `amix` 把外部 audio pin 跟 source 自帶音軌混在一起；支援 cinematic fps（23.976 / 29.97 / 59.94 等 FLOAT 值）
- 🧪 **廣播級編碼控制** — H.264 / HEVC / AV1 / ProRes，支援 CRF / bitrate / target-size 三種編碼模式；NVENC variants（`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`）由 `ffmpeg -encoders` probe 自動加入 dropdown
- 🎚️ **單次編碼、多層 overlay 的 Compose pipeline** — `filter_complex` graph 編譯器，N 層 overlay 仍只走一次 re-encode
- 🤖 **Provider-agnostic AI** — `MF_AIConfig` 讓 Whisper / Translate 在 OpenAI / Groq / Ollama / 本地 backend 之間一處切換
- 🪶 **零硬性 Python 相依** — 只要 PATH 有 `ffmpeg` + `ffprobe`。`requests` / `faster-whisper` 用到才 lazy import
- 🔁 **Tensor-native** — `IMAGE [B,H,W,C] float32` 跟 ComfyUI canonical 的 `AUDIO` dict；rawvideo roundtrip PSNR > 38 dB

## 目錄

1. [Quick Start](#quick-start)
2. [Using via API（API 工作流）](#using-via-api)
3. [智慧 GPU codec 預設](#智慧-gpu-codec-預設)
4. [為什麼選這個 plugin（vs VideoHelperSuite）](#為什麼選這個-pluginvs-videohelpersuite)
5. [節點清單（22）](#節點清單22)
6. [AI Provider Recipes](#ai-provider-recipes)
7. [Hidden Contracts（內部型別契約）](#hidden-contracts內部型別契約)
8. [Architecture](#architecture)
9. [系統需求](#系統需求)
10. [安裝](#安裝)
11. [疑難排解](#疑難排解)
12. [常見問題](#常見問題)
13. [測試](#測試)
14. [Roadmap](#roadmap)
15. [License & 致謝](#license)

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

### 2. Compose 聯合:浮水印 + 開場文字 + BGM + 字幕,**只走一次** re-encode(5 個節點)

```
[ComposeWatermark]→[ComposeOverlayText]→[ComposeBurnSubtitle]→ MF_COMPOSE_OPS ─┐
                                                                                ▼
[ComposeAudioMix(bgm.mp3)] ──────────────── MF_COMPOSE_AUDIO_OPS ──► [ComposeVideo] → output.mp4
                                                                                ↑
                                                                                video_path
```

| 節點 | 關鍵設定 |
|---|---|
| `MF_ComposeWatermark` | `image_path = "logo.png"`, `placement = "bottom_right"`, `relative_scale = 0.12`, `opacity = 0.6` |
| `MF_ComposeOverlayText` | `text = "Episode 01"`, `fontsize = 64`, `start_sec = 0`, `end_sec = 5` |
| `MF_ComposeBurnSubtitle` | `srt_path = "subs.srt"`, `font = "msjh.ttc"`, `font_size = 24` |
| `MF_ComposeAudioMix` | `audio_path = "bgm.mp3"`, `keep_source = True`, `bgm_volume = 0.3` |
| `MF_ComposeVideo` | `video_path = "clip.mp4"`, `target_*=0`(沿用 source), `codec = "h264_nvenc"`, `crf = 18` |

四個效果累積到 Compose IR、最後編譯成**一份** `filter_complex_script` — 輸入只 decode 一次、輸出只 encode 一次。疊 10 層 overlay + 4 個 audio op 也只付一次 re-encode 成本。

> **從 v1 遷移?** `MF_ComposeStart` + `MF_ComposeFinalize` 已合併進 `MF_ComposeVideo`。見 [遷移指南](#從-compose-v1-遷移)。

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

## Using via API

所有產出檔案的節點(BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeVideo / ConvertChinese) 都同時 emit ComfyUI `ui.images` metadata 跟 STRING path 兩種輸出。API 客戶端不必自己 parse 路徑就能拿到產出檔。

### 1. 送 workflow 上去

```bash
curl -X POST http://localhost:8188/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt": <workflow_json>}'
# → {"prompt_id": "abc-123", "number": 1, ...}
```

### 2. 拿 history 查產出

```bash
curl http://localhost:8188/history/abc-123
```

每個產檔節點在 outputs 內會同時暴露：

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

（`images` 是 ComfyUI 通用 UI key — 影片 / 音訊 / 任意檔都走這個 key。下游 wire 拿到的 STRING `final_video_path` 仍然有效，metadata 是「**加上去**」而非取代。）

### 3. 下載產出檔

```bash
curl "http://localhost:8188/view?filename=subtitled_00001.mp4&subfolder=MediaForge&type=output" \
  -o final.mp4
```

`type` 永遠是 `output`。每次跑 workflow 自動接 counter（`_00001.mp4` → `_00002.mp4` → ...）、不會 silently 覆蓋前次成品。

### 字幕 / 文字輸出

`MF_ConvertChinese` 寫 `.srt` 或 `.txt`（heuristic：轉換後字串含 `-->` 視為 SRT）。`MF_WhisperTranscribe` / `MF_TranslateSubtitle` 回傳 SRT **字串內容**而非檔案 — 直接 wire 給下游 `MF_BurnSubtitle`，或先過 `MF_ConvertChinese`（填 `filename_prefix`）才會落地成檔。

## 智慧 GPU codec 預設

所有 encode 能力節點(BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeVideo) 的 `codec` dropdown 預設值在啟動時動態挑:偵測到 ffmpeg 支援 NVENC 就用 **`h264 NVIDIA GPU (h264_nvenc)`**,沒有就 fallback **`h264 (libx264)`** — CPU-only 機器不會壞、不必每次手動切。

Probe 在 ComfyUI 啟動時跑一次（走 `utils/encoder.py:pick_default_codec()`），檢查 `ffmpeg -encoders` 有沒有 `h264_nvenc`。NVENC variants（`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`）只在可用時加進 dropdown；`av1_nvenc` 需要 Ada Lovelace（RTX 4000+）。

要 per-node 覆寫直接在 dropdown 選就好 — 既有 workflow 已存了 codec 值（如 `"h264 (libx264)"`）載入時不受影響，新 default 只影響**新拖出來的節點**。

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

## 節點清單（22）

> **Dual-input 註記**：下列標 **(dual-input)** 的 node 同時接受兩種 input：(a) 既有的 `video_path` STRING 欄位，(b) 新加的 `frames` + `tensor_fps` + `audio` 三件套 optional 輸入。連 tensor 時 MediaForge 內部會寫一個 temp .mp4 給 FFmpeg 吃。Path 模式仍是預設的 fast path — MediaForge 之間串接時不會被迫多走一次無謂的 decode/encode。
>
> 前端 extension `web/dual_input_lock.js` 把 widget 可見性跟 wiring 狀態連動：接 `frames` 時 path widget 自動收起（tensor 模式）、`tensor_fps` 只在接了 frames 時才顯示。Path-mode-only 的 widget（如 BurnSubtitle 的 `keep_source_audio`）在 tensor 模式自動隱藏。

### `MediaForge/Subtitle`

#### 🀄 Convert Chinese (`MF_ConvertChinese`)

OpenCC 簡繁中文轉換、對任意中文文字或 SRT 都通用。四個 profile：`s2twp`（簡→繁台灣詞庫，預設）/ `s2t`（簡→繁通用）/ `tw2sp`（繁台灣→簡）/ `t2s`（繁通用→簡）。三段式輸入：直接貼 `text` widget、wire 上游 STRING（如從 `MF_TranslateSubtitle` / `MF_WhisperTranscribe`）、或填 `input_path` 讀檔。`filename_prefix` 非空時自動 counter 寫到 `output/<prefix>_NNNNN.srt`（或 `.txt`，副檔名 auto-detect 看 `-->`）— 跟其他 file-producer 同 pattern。Lazy-import `opencc-python-reimplemented` — 安裝：`pip install opencc-python-reimplemented`。

#### 🔥 Burn Subtitle (`MF_BurnSubtitle`) **(dual-input)**

SRT → 硬燒字幕，完整 ASS 風格控制。顏色輸入 `#RRGGBB`，內部轉成 ASS BGR-with-alpha。

**輸出**：`filename_prefix` STRING（預設 `MediaForge/subtitled`）— 對齊 ComfyUI 核心 `SaveImage` 慣例，每次跑 workflow 自動接 counter：`output/<prefix>_00001.mp4` → `_00002.mp4` → ... 不會 silently 覆蓋先前產出。可含子目錄；`.mp4` 副檔名自動補上。

可調樣式：
- **字型**：`font` dropdown 讀 `<plugin>/font/*.ttf|.otf|.ttc`。把 TTF 丟到 `font/` 後從 dropdown 選即可 — MediaForge lazy-import `fontTools` 自動讀 TTF 內部的 Family Name 餵給 libass。沒裝 `fontTools` 時退用檔名 stem（建議 `pip install fontTools`）。
- **粗細 / 風格**：`bold` + `italic` boolean、`letter_spacing` FLOAT（ASS Spacing，像素為單位）。
- **外框 / 陰影 / 底色塊**：`outline_color_hex` + `outline_width` + `shadow_depth` + `border_style`（1=outline 描邊、3=box 半透明底色配 `back_color_hex`）。
- **位置**：`alignment` dropdown（9 個命名位置：`bottom_center (2)`、`top_right (9)`…）+ `margin_v` + `margin_l` + `margin_r`。字幕實際可用寬 = 播放區寬 − `margin_l` − `margin_r`（margin 不對稱時可同時控制「字幕往哪邊推」+「字幕多寬」）。

進階 optional 輸入：`video_path`（檔案路徑、沒接 tensor 時走它）、`tensor_fps`（只在連 `frames` 時用）、`keep_source_audio`（BOOLEAN，預設 `True` — 接了外部 `audio` pin 且 source 影片自帶音軌時 `amix` 混兩條；設 `False` 退回舊行為，外部音蓋過 source）、`target_fps`（輸出畫格率覆寫；`0.0` = 沿用 source fps — FLOAT 是為了支援廣電 / 手機素材常見的 cinematic 23.976 / 29.97 / 59.94）。

### `MediaForge/Video`

#### 📂 Select Video (`MF_SelectVideo`)

`ComfyUI/input/` 的影片檔 dropdown picker。會遞迴掃子目錄、列 `.mp4 / .mov / .mkv / .webm / .avi / .m4v / .mpg / .mpeg / .ts`。輸出 `STRING video_path` — 直接 wire 給任何 file-consumer node。`IS_CHANGED` 用 file mtime 做 cache key，同檔名換內容也會自動 invalidate 下游 cache。

#### 🔁 Loop Video (`MF_LoopVideo`) **(dual-input)**

把影片循環到目標時長。三種模式對應不同接縫處理需求，可加速、可反向。

**Loop modes**：
- **`strict`** — 硬接重複到精確時長（無接縫平滑），最快、最可預測。
- **`ping_pong`** — A→A 反向→A→A 反向（順暢來回，無可見接縫）。
- **`crossfade`** — 重複片段之間走 `xfade` chain；上限 50 圈；`crossfade_sec >= 有效片段長度` 時自動退階回 `strict`(合理退階、不報錯)。

**核心設定**:
- `target_duration_sec`（FLOAT，預設 30.0）— 輸出時長（秒）。
- `crossfade_sec`（FLOAT，預設 1.0）— 重複片段間的重疊長度；只在 `crossfade` mode 用。
- `speed`（FLOAT 0.25–4.0，預設 1.0）— 播放速度（走 `setpts` + `atempo` chain；超出範圍會自動 chain）。
- `reverse`（BOOLEAN）— 先把 source 反向再 loop。
- `keep_audio`（BOOLEAN，預設 True）+ `audio_volume`（FLOAT 0.0–1.0，預設 1.0）— 衰減音量。`0.0` 靜音、`0.5` 半音量、`1.0` 原音。

**編碼**：`codec` / `crf`（預設 18）/ `preset`（預設 `medium`）— `codec` 在偵測到 NVENC 時預設 GPU 加速。見 [智慧 GPU codec 預設](#智慧-gpu-codec-預設)。

**輸出**：`filename_prefix`（預設 `MediaForge/looped`）→ `output/MediaForge/looped_<NNNNN>.mp4`（auto-counter）。

**硬限制**：FFmpeg `loop` filter 的 frame 緩衝上限是 `MAX_LOOP_FRAMES = 32767`（INT16），超長素材高 fps 會撞牆 — 改用 `crossfade` mode（xfade chain 無單一 buffer 上限）或降 fps。

#### 📥 Load Video Frames (`MF_LoadVideoFrames`)

FFmpeg decode 任意容器／codec → `IMAGE` batch `[B,H,W,C] float32 [0,1]` + `AUDIO` dict（`{'waveform': Tensor[B,C,T], 'sample_rate': int}`）+ fps + metadata JSON。**旋轉感知**（直拍手機影片會正確顯示），記憶體有上界。可選 `target_fps` 重採樣、`max_frames` 截斷。

#### 📤 Save Video Frames (`MF_SaveVideoFrames`)

`IMAGE` batch（+ 可選 `AUDIO` dict）→ 編碼後影片檔。canonical 的 tensor→file producer；跟 `MF_LoadVideoFrames` 形成對稱 roundtrip。

**編碼模式**（單一 dropdown、互斥）：
- **`crf`（預設）** — 等質編碼（數字越低品質越高、檔案越大）。`crf` 0=無損、18=視覺無損、23=libx264 標準、28=堪用、51=最差。
- **`bitrate`** — 指定 `bitrate_kbps`（如 4000 = 4 Mbps）。
- **`target_size`** — 給 `target_size_mb` 上限、自動算 bitrate（從 duration 估算 two-pass-style）。

**Codecs**（容器副檔名自動修正）：
- H.264 / HEVC / AV1 / ProRes — 全部 CPU + NVENC variants（`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`）自動偵測。
- ProRes → `.mov`（其他 codec → `.mp4`）。`prores_ks` 用 `yuv422p10le` pix_fmt 保留 10-bit 精度。

**Tensor → fps**：`fps` 控制 raw video pipe 的來源 frame rate。從 `MF_LoadVideoFrames` 接過來時用其輸出的 `meta_fps`。

**輸出**：`filename_prefix` → `output/<prefix>_<NNNNN>.<ext>`（ext 自動選）。

#### ✂️ Trim by Ranges (`MF_TrimByRanges`) **(dual-input)**

按時間區間裁切影片。主要使用情境是接 `MF_DetectSilence` 自動裁靜默，但也支援手填 JSON 區間做手動編輯。

**Ranges 輸入**（兩種、互斥）：
- `ranges`（pin）— 從上游（通常是 `MF_DetectSilence`）接的 `SILENCE_RANGES` 列表 `[[start_sec, end_sec], ...]`。
- `ranges_json`（STRING widget，JSON literal）— 手填覆寫，例如 `[[1.5, 3.0], [10.0, 12.5]]`。

**Modes**：
- **`keep`** — 保留指定區間、刪掉其他。空 ranges → raise（沒東西可保留、拒絕輸出空檔）。
- **`remove`** — 刪掉指定區間、保留其他。空 ranges → identity（no-op，回原片）。

**接縫處理**：
- `crossfade_sec`（FLOAT，預設 0.0）— 相鄰保留段之間走 `xfade` chain 做淡入淡出。0 = 硬切。

**編碼**：跟其他 encode 節點同一組 `codec` / `crf` / `preset`；GPU NVENC 預設。

**輸出**：`filename_prefix`（預設 `MediaForge/trimmed`）→ `output/<prefix>_<NNNNN>.mp4`。

**音訊處理**：音影 interleaved concat（`[v0][a0][v1][a1]...concat=n=N:v=1:a=1`）讓音訊跨切點保持同步。沒音軌的 source 自動處理。

#### 🔗 Concat Videos (`MF_ConcatVideos`) **(dual-input, prepend 語意)**

多檔路徑級拼接。兩種策略對應不同速度／相容性 trade-off。

**Modes**：
- **`copy`** — FFmpeg concat demuxer、stream copy 不 re-encode。極快但要求輸入的 codec / 解析度 / fps / pix_fmt 完全一致。最適合同台相機輸出或預先 normalize 過的素材。
- **`transcode`** — `filter_complex` graph 加可選過場。一定可用、一定 re-encode。當 source 在 codec / 尺寸 / fps 不一致時必須走這個。

**過場**（`transcode` mode、`transition_sec > 0` 才用）：
- `fade`、`wipeleft`、`wiperight`、`slideleft`、`slideright`、`circleopen`、`circleclose`、`dissolve` — FFmpeg `xfade` 內建。

**輸入**：
- `video_paths`（STRING widget、多行）— 每行一個絕對路徑。至少 2 段。
- `frames`（IMAGE pin、optional）— 接線時 tensor 寫成 path[0]（prepend）、`video_paths` 列表 shift 到 path[1..N]。至少要有 1 條 path 才能湊到 2 段。

**Transcode 設定**：`fps` / `width` / `height` / `crf` / `codec` / `preset`。GPU NVENC 預設。

**輸出**：`filename_prefix`（預設 `MediaForge/concat`）→ `output/<prefix>_<NNNNN>.mp4`。

**行為註記**：沒音軌的輸入自動補 `anullsrc` 靜音（只 transcode mode）；demuxer mode 在音訊串流不一致時會拒絕。

### `MediaForge/Analysis`

#### 🔍 Probe Media (`MF_ProbeMedia`) **(dual-input)**

`ffprobe` 包裝，回任意 media 檔的結構化 metadata。純讀、不會跑 FFmpeg encode。

**輸出**（6 個 port）：
- `duration_sec`（FLOAT）— **影片串流的時長**。跟容器時長可能不一樣（MKV 之類 mux 容易有差異）；MediaForge 用影片時長當權威 timeline。
- `width` / `height`（INT）— **display dimensions**（rotation-aware）。直拍手機影片帶 rotation metadata 會在這裡 swap，下游 Compose / Save 拿到的方向正確。
- `fps`（FLOAT）— 從 `r_frame_rate` parse（例如 `30000/1001` → 29.97）。
- `video_codec`（STRING）— 例如 `"h264"`、`"hevc"`、`"av1"`、無影片時是 `""`。
- `audio_codec`（STRING）— 例如 `"aac"`、`"opus"`、無音訊時是 `""`。

跟 `MF_LoadVideoFrames`(先 probe 拿尺寸、再 load) 跟 Compose pipeline(餵尺寸給 `MF_ComposeVideo`、或留 `target_*=0` 沿用 source) 天然配對。

#### 🤫 Detect Silence (`MF_DetectSilence`)

FFmpeg `silencedetect` 包裝 → `SILENCE_RANGES` list（`[[start_sec, end_sec], ...]`）。`MF_TrimByRanges` 的標準上游、用在演講縮時／podcast 預剪／直播精華 workflow。

**可調**：
- `noise_db`（FLOAT，預設 -30.0）— dB 在此值以下且持續 ≥ `min_duration_sec` 就算靜音。較不負（-20）= 較積極；較負（-40）= 較嚴格。
- `min_duration_sec`（FLOAT，預設 1.5）— 最短靜音長度才會被記錄、低於此值視為自然停頓忽略。

**輸出**：`ranges`（SILENCE_RANGES）。空 list = 沒偵到靜音；下游 `MF_TrimByRanges` 空 list + `mode="remove"` 視為 identity（保留整片）。

### `MediaForge/Compose` — 單次編碼 pipeline(視訊 overlay + 音訊 chain)

Compose pipeline 讓 overlay / 字幕 / 音訊操作**單次 ffmpeg encode** 完成。兩條並行 chain(視訊 overlay + 音訊 op) 接進 `MF_ComposeVideo`、一次跑出成品。

```
[OverlayText] → [Watermark] → [BurnSubtitle] ──► MF_COMPOSE_OPS ─┐
                                                                  ▼
[Volume] → [AudioMix(+bgm)] → [Fade] → [Normalize] ── AUDIO_OPS ──► [ComposeVideo] → output.mp4
                                                                          ↑
                                                                          video_path
```

**最簡工作流** 2 個節點:拖一個 overlay 節點、wire 進 `ComposeVideo`。沒 audio chain 也可、純 transcode 也可。

#### 🎬 Compose Video (`MF_ComposeVideo`) **(dual-input)**

Compose 工作流的單一終點 — 取代舊的 `ComposeStart` + `ComposeFinalize` 兩節點模式。

**設定**:
- `video_path`(STRING) + dual-input `frames` / `tensor_fps` / `audio` — source media。
- `target_fps`(FLOAT,**預設 0.0**) + `target_width`(INT,預設 0) + `target_height`(INT,預設 0) — `0` = **沿用 source**(透過 ffprobe 偵測、含 rotation-aware dims)。多數 workflow 留 0 即可。
- `codec` / `crf` / `preset` — `codec` smart default 偵測到 NVENC 用 `h264_nvenc`、否則 `libx264`。NVENC variants(`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`) 自動加入 dropdown。CRF 範圍 0–51(預設 18 = 視覺無損)。
- `keep_audio`(BOOLEAN、預設 True) — 沒接 audio chain 時是否保留 source 自帶音軌。

**Optional chain 輸入**:
- `overlays`(`MF_COMPOSE_OPS`) — 從任一組合的 `ComposeOverlayText` / `ComposeOverlayImage` / `ComposeWatermark` / `ComposeBurnSubtitle` 串接過來。列表順序 = z-order(後面的疊在前面之上)。
- `audio_ops`(`MF_COMPOSE_AUDIO_OPS`) — 從 `ComposeVolume` / `ComposeAudioMix` / `ComposeAudioFade` / `ComposeNormalize` 串接。順序 = filter chain 順序。

**輸出**:`filename_prefix`(預設 `MediaForge/composed`) → `output/<prefix>_<NNNNN>.mp4`(或 ProRes 走 `.mov`)。回傳 path + 編譯後的 `filter_complex_script`(debug 用)。emit `ui.images` metadata 給 API `/history` 暴露。

**行為**:編譯後 graph 超過 6000 字元自動切到 `-filter_complex_script <tempfile>`(避免 Windows command-line 長度限制)。

#### ✏️ Compose Overlay Text (`MF_ComposeOverlayText`)

Append 一個 `drawtext` op spec 進 overlay chain。

**Widget**:

- `text`(multiline STRING) — 編譯時透過 `textfile=` 傳給 ffmpeg、安全處理單引號 / % / 換行。
- `x_expr` / `y_expr` — FFmpeg drawtext **位置表達式**(string、不是數字 — 詳見下方表達式參考表)。
- `fontsize` / `fontcolor` / `borderw` / `bordercolor` — 標準樣式。
- `effect` — 動畫 preset(`none` / `slide_in_left|right|top|bottom` / `marquee_horizontal`)。非 `none` 時把 `x_expr` / `y_expr` 當成**最終停靠位置**、節點層 wrap 動畫表達式上去。預設 `none` = 無動畫(對舊 workflow 完全 backward compat)。
- `effect_duration`(FLOAT、預設 1.5) — `slide_in_*` 表「滑入秒數」;`marquee_horizontal` 表「跑一輪需要的秒數」。`effect=none` 時忽略。
- `fontfile` — 留空讓 ComposeVideo 退階到 bundled font(Windows 原生 ffmpeg 沒 fontconfig)。
- `start_sec` / `end_sec` — 時間**可見性**區間。兩者 0 = 全長顯示。(注:`effect_duration` 從 `start_sec` 算 — 文字出現後才開始跑動畫。)

##### `x_expr` / `y_expr` 表達式語言

FFmpeg `drawtext` 接受的是算式 string 而非單純數字,**每幀**在 encode 時求值 — 所以可以做動態定位。可用變數:

| 變數 | 意義 |
|---|---|
| `w`, `h` | 影片畫面寬 / 高(px) |
| `text_w`, `text_h` | 文字 render 出來的 bounding box 寬 / 高(px) — 會自動隨 `fontsize` 改變 |
| `t` | 當前幀時間戳(秒、浮點) |
| `n` | 當前幀號(int) |
| `line_h`(別名 `lh`) | 單行文字高度 |

支援算符:`+ - * /` 加內建函式(`if(cond,a,b)`、`lt(a,b)`、`mod(x,y)`、`sin(x)`、`between(x,lo,hi)` 等)。完整列表見 [FFmpeg drawtext 文檔](https://ffmpeg.org/ffmpeg-filters.html#drawtext)。

**預設 `(w-text_w)/2` / `h-text_h-40`** = 水平置中、距畫面底邊 40 px(典型的 lower-third 字幕位置)。`text_w` 會自動跟著字級走、即使改 `fontsize` 也能保持置中。

**常用範例**:

| 想做 | `x_expr` | `y_expr` |
|---|---|---|
| 螢幕正中 | `(w-text_w)/2` | `(h-text_h)/2` |
| 左上角、padding 30 px | `30` | `30` |
| 右下角、padding 30 px | `w-text_w-30` | `h-text_h-30` |
| 水平置中、垂直上三分之一 | `(w-text_w)/2` | `h/3` |
| 跑馬燈(右→左、~100 px/s) | `w-mod(t*100,w+text_w)` | `h-text_h-20` |
| 上下擺動(正弦、±10 px) | `(w-text_w)/2` | `h-text_h-40+10*sin(2*t)` |
| 2 秒從左飛入、停在中央 | `if(lt(t,2),-text_w+(w-text_w)/2*t/2,(w-text_w)/2)` | `(h-text_h)/2` |

最後兩個範例說明了為什麼 `x_expr` / `y_expr` 設計成 string 而非 INT widget — 表達式語言能寫出時間相依動畫。但常見的(`slide_in_*` / `marquee_horizontal`)直接用下方的 `effect` dropdown 就好、不必手寫。

##### `effect` preset 對照

當 `effect != none`、`x_expr` / `y_expr` 被視為**最終停靠位置(anchor)**、preset 在外面包一層動畫表達式。下表 effect 行為皆假設 anchor = 預設值 `(w-text_w)/2` / `h-text_h-40`、`effect_duration=1.5`:

| Preset | 行為 | `effect_duration` 語意 |
|---|---|---|
| `none` | 原樣使用 `x_expr` / `y_expr` | —(忽略) |
| `slide_in_left` | 文字從畫面外左方滑入、在 `start_sec + effect_duration` 抵達 anchor | 滑入秒數 |
| `slide_in_right` | 從畫面外右方滑入 | 滑入秒數 |
| `slide_in_top` | 從畫面外上方滑入 | 滑入秒數 |
| `slide_in_bottom` | 從畫面外下方滑入 | 滑入秒數 |
| `marquee_horizontal` | 持續右→左跑馬燈(忽略 `x_expr`、用 `y_expr` 決定垂直位置) | 每跑完一輪所需秒數 |

動畫時間軸從 `start_sec` 算起:`slide_in_left` 配 `start_sec=3` + `effect_duration=2` 表示第 3 秒文字出現開始滑、第 5 秒抵達 anchor。`start_sec` / `end_sec` 控可見性、不影響動畫表達式。

需要 preset 沒涵蓋的動畫(垂直擺動、easing 曲線、淡入)的話 — 留 `effect=none`、自己手寫 `x_expr` / `y_expr`。

#### 🖼️ Compose Overlay Image (`MF_ComposeOverlayImage`)

通用圖片 overlay。

- `image_path` — PNG / JPG 等。
- `x_expr` / `y_expr` — 絕對或表達式位置。
- `scale_w` — 寬度像素(0 = 原圖、高度按 aspect ratio 自動算)。
- `start_sec` / `end_sec` — 時間區間。

#### 💧 Compose Watermark (`MF_ComposeWatermark`)

浮水印 preset — 對最常見場景的便利 UI。

- `image_path` — 建議用帶 alpha 的 PNG。
- `placement` — `top_left` / `top_right` / `bottom_left` / `bottom_right` / `center` / `tile`(用真實 aspect 自動算 row × col)。
- `relative_scale`(0.05–0.5) — 浮水印寬度 / frame width 比例。compile 時用 `ComposeVideo.target_width` 解析成絕對像素。
- `opacity`(0–1) — 走 `colorchannelmixer alpha`、保留 PNG 原 alpha。
- `margin_top` / `right` / `bottom` / `left` — 四邊獨立 margin。
- `visible_start/end_sec` — 兩者 0 = 全長顯示。

#### 🔥 Compose Burn Subtitle (`MF_ComposeBurnSubtitle`)

v2 的旗艦新功能 — 字幕燒錄**進到 Compose pipeline 內**、 `字幕 + 浮水印 + 音訊 mix` 可一次 encode 完成。

設定 widget 跟 `MF_BurnSubtitle` 一致(font dropdown、完整 ASS 樣式、alignment、margins、顏色)。把 `.ttf` / `.otf` / `.ttc` 丟進 plugin 的 `font/` 目錄、dropdown 自動掃描。`fontTools` 自動偵測 TTF 內部 Family Name 給 libass(lazy-imported,建議 `pip install fontTools`)。

獨立的 `MF_BurnSubtitle`(在 Subtitle 分類) 仍保留,給「只燒字幕不疊其他 overlay」的場景。

#### 🔊 Compose Volume (`MF_ComposeVolume`)

Append `volume=N` 音訊 filter op。

- `scale`(FLOAT 0.0–2.0、預設 1.0) — `0.0` 靜音、`0.5` 半音量、`1.0` 原音、`2.0` 2× boost(注意 clipping)。

#### 🎵 Compose Audio Mix (`MF_ComposeAudioMix`) **(dual-input audio)**

把外部 BGM 跟 source audio 混音、或完全用外部音源取代 source。

- `audio_path`(STRING) — BGM 檔案路徑、或 wire `audio` pin (AUDIO dict)。AUDIO dict 會 materialize 成 temp WAV、encode 完自動清理。
- `keep_source`(BOOLEAN、預設 True) — `True` 走 `amix` 混 source+BGM;`False` 捨棄 source、純粹用 BGM。
- `bgm_volume`(FLOAT 0.0–2.0、預設 0.3) — BGM 在 mix 前的音量衰減。預設 0.3 讓 voice 蓋過 BGM、podcast/vlog 慣例。
- `duration` — `first` (輸出長度 = source audio) / `longest` / `shortest`。

#### 🌅 Compose Audio Fade (`MF_ComposeAudioFade`)

Append `afade` op (淡入 / 淡出)。

- `direction` — `in`(靜→全) 或 `out`(全→靜)。
- `start_sec` / `duration_sec` — fade 視窗。`out` 用 `start_sec = video_duration - duration_sec`。
- `curve` — 10 種 FFmpeg curve:`tri`(線性,預設) / `qsin`(quarter sine,聽起來最自然) / `esin` / `hsin` / `log` / `par` / `qua` / `cub` / `squ` / `cbr`。

#### 📏 Compose Normalize (`MF_ComposeNormalize`)

EBU R128 / streaming 級響度標準化(走 `loudnorm` 單 pass)。

- `target_i`(LUFS、預設 -16) — Apple Podcasts / Spotify spoken-word 目標。YouTube / TikTok 用 -14、廣電 EBU R128 用 -23。
- `target_tp`(dBTP、預設 -1.0) — true-peak 上限。-1 dBTP 避免在 consumer 端 clip。
- `target_lra`(LU、預設 11.0) — loudness range、值越大保留越多動態。
- `linear`(BOOLEAN、預設 True) — `True` 避免 dynamic range compression。設 False 會強制壓平到 target 範圍、犧牲動態。

> **單 pass** 對 streaming 用途夠用。嚴格 EBU R128 broadcast 認證需要 two-pass(measure → 再套),MediaForge 目前不提供。

### 從 Compose v1 遷移

含 `MF_ComposeStart` / `MF_ComposeFinalize` 的 workflow JSON 載入 ComfyUI 會看到 "Missing nodes" 警告。手動 migrate 步驟:

1. 拖一個新的 `MF_ComposeVideo`。把舊 `ComposeStart` 的 `video_path` / `target_*` + 舊 `ComposeFinalize` 的 `codec` / `crf` / `preset` / `keep_audio` 填過來。
2. 刪掉舊的 `MF_ComposeStart` 跟 `MF_ComposeFinalize` 兩個節點。
3. 把 overlay chain 的最後輸出(原本是 `MF_COMPOSE` IR) 接到 `MF_ComposeVideo` 的新 `overlays` pin(類型現在是 `MF_COMPOSE_OPS`、chain 結構相同)。
4. 如果原本另外用 `MF_BurnSubtitle` 二次 encode 字幕、改用 `MF_ComposeBurnSubtitle` 串進 chain、可以併進單次 encode 省一輪。

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
│   ├── compose_video.py        # MF_ComposeVideo  — Compose v2 終點(取代 Start+Finalize)
│   ├── compose_overlay_text.py # MF_ComposeOverlayText
│   ├── compose_overlay_image.py# MF_ComposeOverlayImage
│   ├── compose_watermark.py    # MF_ComposeWatermark
│   ├── compose_burn_subtitle.py# MF_ComposeBurnSubtitle  — 字幕進 Compose chain(單次 encode)
│   ├── compose_volume.py       # MF_ComposeVolume  — 音訊 chain
│   ├── compose_audio_mix.py    # MF_ComposeAudioMix  — BGM 混音(dual-input audio)
│   ├── compose_audio_fade.py   # MF_ComposeAudioFade
│   ├── compose_normalize.py    # MF_ComposeNormalize  — loudnorm
│   ├── concat_videos.py        # MF_ConcatVideos
│   ├── convert_chinese.py      # MF_ConvertChinese  — OpenCC 簡繁轉換
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
│   ├── ass_style.py         # ASS 字幕 style helpers(BurnSubtitle + ComposeBurnSubtitle 共用)
│   ├── audio_mix.py         # amix / afade / volume / loudnorm filter builders
│   ├── color.py             # hex_to_ass_color:#RRGGBB → ASS BGR+alpha
│   ├── compose_ir.py        # ComposeIR + AudioOp + compile_ir + compile_audio_chain
│   ├── compose_ops.py       # MF_COMPOSE_OPS / AUDIO_OPS dispatch + watermark resolver
│   ├── encoder.py           # codec catalog + NVENC probe + pick_default_codec + build_encoder_args
│   ├── ffmpeg.py            # ensure_ffmpeg / run_ffmpeg / probe / escape_filter_path
│   ├── output_path.py       # resolve_output_path + output_path_to_ui_entry (API metadata helper)
│   └── video_io.py          # rawvideo pipe ↔ IMAGE/AUDIO + encode_tensor_to_tempfile
├── font/                    # 丟 .ttf / .otf 進來給 MF_BurnSubtitle 的 font_file dropdown
├── web/
│   └── dual_input_lock.js   # 前端 extension：依 dual-input 模式隱藏不適用 widget
│                            #   - lock_widget / hidden_when_connected: tensor 模式隱藏
│                            #   - linked_widgets: path 模式隱藏
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
A:Phase 4.5 已有「Compose chain 內」的音訊 op(Volume / AudioMix / Fade / Normalize、跟視訊 overlay 共享一次 encode)。獨立的 Phase 6 audio domain(檔案級 denoise、normalize-file、cut/trim、ducking) 在 AI shakedown 之後做、`MediaForge/Audio` 分類名稱已保留。

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
- **Phase 4** ✅ Compose pipeline v1 — single-encode multi-overlay (Start + Finalize)
- **Phase 4.5** ✅ Compose pipeline v2 — 合一 ComposeVideo + 字幕進 chain + audio chain(Volume / AudioMix / Fade / Normalize)
- **Phase 5** 🚧 AI — WhisperTranscribe / TranslateSubtitle(schema 仍 experimental)
- **Phase 6** ⏳ 獨立 Audio domain — 檔案級 denoise、normalize-file、audio cut/trim、ducking(Phase 4.5 的 Compose audio chain 是子集)
- **Phase 7** ⏳ Net domain — yt-dlp ingest、HTTP fetch(lazy import)

## License

MIT — © YingLiang Lu (leon80148)。

## 致謝

- **FFmpeg** — 真正幹活的引擎；這個 plugin 90% 是在格式化 argument
- **ComfyUI** — runtime 跟節點圖引擎
- **VideoHelperSuite** — 先行作品，定義了 IMAGE-batch 契約，MediaForge 與其互通
- **faster-whisper / CTranslate2** — 本地 Whisper backend
