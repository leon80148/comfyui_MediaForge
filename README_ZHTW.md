# MediaForge

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg) ![License MIT](https://img.shields.io/badge/license-MIT-green.svg) ![ComfyUI](https://img.shields.io/badge/ComfyUI-custom__nodes-orange.svg)

> **給進階使用者的 ComfyUI FFmpeg 工具集。** Tensor↔影音橋接，帶廣播級編碼控制。深度 > 廣度。

FFmpeg 驅動的 custom_nodes plugin：字幕燒入、影片循環、媒體 probe、tensor-native 影格 I/O、靜音偵測、多片段拼接、單次編碼多層 overlay 的 Compose pipeline，以及 provider-agnostic 的 AI 字幕／翻譯。**所有節點都是 FFmpeg 的薄包裝，不混 GPU / AI 模型推論**。AI 節點靠 `MF_AIConfig` 連線傳遞 provider 設定，一處切換即可整批換 backend。

📖 **[English README →](README.md)**

## 亮點

- 🎞️ **24 個節點** 分散在 6 個分類 — Subtitle / Video / Analysis / Audio（Phase 6 開張）/ Compose（含 audio chain）/ AI（Net / Image 規劃中）
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
5. [節點清單（24）](#節點清單24)
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
| `MF_AIConfig` (ASR) | `provider = "openai_compatible"`, `base_url = "https://api.groq.com/openai/v1"`, `model = "whisper-large-v3"` |
| `MF_WhisperTranscribe` | `audio_path = "interview.mp4"` |
| `MF_AIConfig` (LLM) | `provider = "openai_compatible"`, `base_url = "https://api.openai.com/v1"`, `model = "gpt-4o-mini"` |
| `MF_TranslateSubtitle` | `target_lang = "繁體中文"` |
| `MF_BurnSubtitle` | 把翻譯後 SRT wire 進 `srt_text`，設定 ASS 字型、顏色、外框 |

具體 `base_url` / `model` 組合可直接複貼 — 見下方 [AI Provider Recipes](#ai-provider-recipes)。

## Using via API

所有產出檔案的節點(BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeVideo / ExtractAudio / ConvertChinese) 都同時 emit ComfyUI `ui.images` metadata 跟 STRING path 兩種輸出。API 客戶端不必自己 parse 路徑就能拿到產出檔。

注意：若 `filename_prefix` 解析到 `output/` 之外（舊相容用法，例如 `filename_prefix="../input/cleaned"`），節點仍會正常寫檔、正常回傳路徑，但不會出現在 `/history` 的 `ui.images` 清單中 —— ComfyUI 內建的 `/view` endpoint 只服務 `output/` 底下的檔案，沒有對應的合法 `/view` URL 可以暴露。

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

`MF_ConvertChinese` 寫 `.srt` 或 `.txt`（heuristic：轉換後字串含 `-->` 視為 SRT）。`MF_WhisperTranscribe` / `MF_TranslateSubtitle` 回傳 SRT **字串內容**而非檔案 — 直接 wire 給 `MF_BurnSubtitle` 的 `srt_text` 輸入（節點內部落地暫存檔），要把 SRT 存成真檔案的話走 `MF_ConvertChinese`（填 `filename_prefix`）。

## 智慧 GPU codec 預設

所有 encode 能力節點(BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeVideo) 的 `codec` dropdown 預設值在啟動時動態挑:偵測到 ffmpeg 支援 NVENC 就用 **`h264 NVIDIA GPU (h264_nvenc)`**,沒有就 fallback **`h264 (libx264)`** — CPU-only 機器不會壞、不必每次手動切。

Probe 在 ComfyUI 啟動時跑一次（走 `utils/encoder.py:pick_default_codec()`），檢查 `ffmpeg -encoders` 有沒有 `h264_nvenc`。NVENC variants（`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`）只在可用時加進 dropdown；`av1_nvenc` 需要 Ada Lovelace（RTX 4000+）。

**CRF-equivalent 畫質（`cq`）**：當 `crf` 對到 NVENC encoder 時，`utils/encoder.py:build_encoder_args()` 會吐 `-rc vbr -cq <n> -b:v 0`。這個 `-b:v 0` 很關鍵 — 沒加的話 NVENC 仍會疊加預設 ~2 Mbps 的 bitrate target 在 `-cq` 之上，不管 `crf` 設多低畫質都被蓋住。

**同數值 ≠ 同畫質（跨 encoder family）。** MediaForge 把你填的 `crf` 值原樣傳給各家 encoder（不自動換算 — 偷換會讓「同 crf 換 codec 重跑」的輸出不可預期），而且**不存在**通用的等畫質換算表：對映關係隨 encoder 世代、preset、內容型態變動。每個 encoder 固定不變的是：

| Encoder | 實際送出的 rate-control 參數 | 合法範圍 | Encoder 自己的預設 |
|---|---|---|---|
| `libx264` | `-crf` | 0–51 | 23 |
| `libx265` | `-crf` | 0–51 | 28 |
| `libsvtav1` | `-crf` | 原生 0–63（MediaForge 統一 `crf` widget 上限 51 — 碰不到的 52–63 段是最低畫質區,實務幾乎用不到） | 35 |
| `h264_nvenc` / `hevc_nvenc` / `av1_nvenc` | `-rc vbr -cq <n> -b:v 0` | 1–51 為明確 CQ；`0` = automatic（encoder 自決，**不是**最高畫質） | （沒設 `cq` 時走 bitrate 導向） |

在各 family 的明確數值範圍內，越低畫質越高／檔案越大（NVENC 的 `cq=0` 是 auto sentinel、不是「比 1 更好」）。注意各 encoder *自身預設值* 的落點 — x265 與 SVT-AV1 的刻度在同等意圖下數值比 x264 高 — 且 NVENC `-cq` 與 `libx264 -crf` 並非一單位對一單位。換 codec family 時，舊值只能當起點：先用一小段代表性素材試編、比對大小與畫質後再調整。

要 per-node 覆寫直接在 dropdown 選就好 — 既有 workflow 已存了 codec 值（如 `"h264 (libx264)"`）載入時不受影響，新 default 只影響**新拖出來的節點**。

## 為什麼選這個 plugin（vs VideoHelperSuite）

| 能力 | VHS | MediaForge | 註 |
|---|---|---|---|
| 影片載入 → IMAGE batch | ✅ opencv | ✅ FFmpeg | MediaForge 可處理 AV1 / HEVC 10-bit / ProRes / VP9 / 任意 colorspace |
| IMAGE batch → 影片儲存 | ✅ 只 H.264 | ✅ H.264 / HEVC / AV1 / ProRes | 加上 CRF / bitrate / target-size 模式 |
| GIF 匯出 | ❌ | ✅ | `MF_SaveVideoFrames` `codec=gif (palette)` — 雙 pass palette + Bayer dither |
| 音訊 in/out（canonical dict） | ⚠️ 有限 | ✅ 雙向 | `{'waveform': Tensor[B,C,T], 'sample_rate': int}` |
| 按 ranges 裁切 | ✅ image-batch | ✅ 影片 + image-batch | MediaForge 直接吃 `MF_DetectSilence` 輸出 |
| 無損裁切（不 re-encode） | ❌ | ✅ | `MF_TrimByRanges` `precision=lossless (stream copy)` |
| **路徑級影片拼接（跨 codec、含音軌）** | **❌** | **✅** | MediaForge 獨有 — VHS Combine 只能拼 IMAGE batch |
| 靜音偵測 | ❌ | ✅ | |
| 場景切換偵測 | ❌ | ✅ | `MF_DetectScenes` — 直接接 `MF_TrimByRanges` |
| 字幕燒入 | ❌ | ✅ | |
| 多層 overlay Compose pipeline（單次 re-encode） | ❌ | ✅ | filter_complex graph 編譯器 |
| 浮水印預設（透明度／邊距／時間窗／擺位） | ❌ | ✅ | |
| AI 字幕（轉錄 + 翻譯） | ❌ | ✅ | provider-agnostic |
| ffprobe metadata | ⚠️ 部分 | ✅ | |

**結論**：兩個都裝。VHS 用於快速 IMAGE batch workflow；MediaForge 用於廣播級編碼、檔案級操作、Compose pipeline、AI 字幕。

## 節點清單（24）

每個節點區段都遵循同一個模板：**用途 → 適用情境 → 必填欄位 → 選用輸入 → 輸出 → 範例**。Widget 表掃過去就能找到要調的旋鈕，範例則給典型接線方式。

> **Dual-input 註記**：下列標 **(dual-input)** 的 node 同時接受兩種 input：(a) 既有的 `video_path` STRING 欄位，(b) `frames` + `tensor_fps` + `audio` 三件套 optional 輸入。連 tensor 時 MediaForge 內部會寫一個 temp `.mp4` 給 FFmpeg 吃。
>
> 前端 extension `web/dual_input_lock.js` 把 widget 可見性跟 wiring 狀態連動：接 `frames` 時 path widget 自動收起（tensor 模式）、`tensor_fps` 只在 tensor 模式才顯示。Path-mode-only 的 widget（如 `keep_source_audio`）在 tensor 模式自動隱藏。
>
> 有些 dual-input node 是靠 **AUDIO** pin 觸發、不是 `frames` — `MF_ComposeAudioMix` / `MF_DetectSilence` / `MF_ExtractAudio` 在 `audio` pin 接上時會隱藏各自的 `audio_path` / `audio_source` STRING widget。機制相同（`web/dual_input_lock.js` 的 `DUAL_INPUT_NODES`），只是觸發的 socket 不同。
>
> **輸出檔名 pattern**：所有產出檔的 node 都用 `filename_prefix` + auto-counter（跟 ComfyUI 核心 `SaveImage` 同 pattern）。每次跑 workflow 寫 `output/<prefix>_00001.mp4` → `_00002.mp4` → ...，重跑不會 silently 覆蓋舊輸出。`filename_prefix` 可含子目錄（例 `MediaForge/subtitled`）。

### `MediaForge/Subtitle`

#### 🀄 Convert Chinese (`MF_ConvertChinese`)

OpenCC 簡繁中文轉換，對純文字或 SRT 檔都通用。字元級對應，SRT 的時間戳 / 序號不會被動到。

**適用情境**：把簡體中文字幕轉成台灣繁體（`s2twp` 在簡→繁基礎上加做 词→詞 詞庫轉換）、處理 crowd-sourced SRT、normalize 編碼混雜的字幕資料集。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `profile` | dropdown | `s2twp (簡→繁台灣詞庫)` | `s2twp` / `s2t`（簡→繁通用）/ `tw2sp`（台灣→簡）/ `t2s`（繁→簡通用） |
| `text` | STRING (multiline) | `""` | 直接貼或 wire 上游 STRING（例 `MF_TranslateSubtitle.translated_srt`） |
| `input_path` *(選用)* | STRING | `""` | `text` 為空才讀。裝了 `charset-normalizer` 會自動偵測 UTF-8 / GBK / BIG5 / UTF-16 |
| `filename_prefix` *(選用)* | STRING | `""` | 非空 → 寫 `output/<prefix>_NNNNN.srt`（不含 `-->` 則寫 `.txt`）。空 → 只 in-memory |

**輸出**：`(converted_text: STRING, saved_path: STRING)`。`saved_path` 沒填 `filename_prefix` 時是空字串。

**範例 chain**：`MF_TranslateSubtitle → MF_ConvertChinese (profile=s2twp) → MF_BurnSubtitle` — 先翻到簡體、用台灣詞庫 normalize、再硬燒。

**相依套件**：lazy-import `opencc-python-reimplemented`（`pip install opencc-python-reimplemented`）。處理非 UTF-8 SRT 建議再裝 `charset-normalizer`。

---

#### 🔥 Burn Subtitle (`MF_BurnSubtitle`) **(dual-input)**

把 SRT 字幕硬燒進影片，完整 ASS 樣式控制。顏色輸入 `#RRGGBB`，內部轉成 ASS BGR-with-alpha。

**適用情境**：要出帶永久字幕的影片（YouTube 上片、社群短片、簡報側錄）。如果還要疊浮水印或加 BGM，改用 `MF_ComposeBurnSubtitle`（整條 chain 一次 encode）。

**必填 widget**（依 node body 由上而下排列）：

| Group | Widget | 預設 | 說明 |
|---|---|---|---|
| 來源 | `video_path` | `input/sample.mp4` | Tensor 模式時隱藏 |
| 來源 | `srt_path` | `input/sample.srt` | UTF-8 SRT |
| 輸出 | `filename_prefix` | `MediaForge/subtitled` | Auto-counter → `output/<prefix>_NNNNN.mp4` |
| 編碼 | `codec` | smart（有 NVENC 用、沒有用 libx264） | 跟 SaveVideoFrames / ComposeVideo 共用 catalog |
| 編碼 | `crf` | `18` (0–51) | 越低畫質越高／檔案越大。原樣傳給各 family（`-crf` 或 NVENC `-cq`）— 刻度意義隨 codec 不同，見[智慧 GPU codec 預設](#智慧-gpu-codec-預設) |
| 編碼 | `preset` | `medium` | `ultrafast` … `veryslow` |
| 字型 | `font` | `msjh.ttc` 有就用、否則第一個 | 讀 `<plugin>/font/*.ttf|.otf|.ttc`；`fontTools` 自動讀 Family Name |
| 字型 | `font_size` | `24` (8–150) | px |
| 字型 | `font_color_hex` | `#FFFFFF` | Hex RGB |
| 字型 | `bold` / `italic` | `True` / `False` | ASS Bold / Italic flag |
| 字型 | `letter_spacing` | `0.0` (0–20) | ASS Spacing（像素） |
| 外框 | `outline_color_hex` | `#000000` | |
| 外框 | `outline_width` | `2` (0–10) | px |
| 外框 | `shadow_depth` | `1` (0–10) | px |
| 外框 | `border_style` | `1` | `1` = 描邊+陰影、`3` = 配 `back_color_hex` 的不透明底色塊 |
| 外框 | `back_color_hex` | `#000000` | 只在 `border_style=3` 看得到 |
| 位置 | `alignment` | `bottom_center (2)` | 9 個命名位置（數字鍵盤 1–9 對映） |
| 位置 | `margin_v` | `20` (0–500) | 距邊緣的垂直邊距（px） |
| 位置 | `margin_l` / `margin_r` | `50` / `50` (0–1000) | 字幕可用寬 = 播放區寬 − `margin_l` − `margin_r` |

**選用輸入**：

| 輸入 | 型別 | 預設 | 何時用 |
|---|---|---|---|
| `srt_text` | STRING（input-only） | — | 來自 `MF_WhisperTranscribe` / `MF_TranslateSubtitle` 的 SRT **內容**字串 — 內部落地 plugin-local 暫存檔；隱藏 `srt_path` |
| `frames` | IMAGE | — | Wire 來自 VHS / AnimateDiff / LoadVideoFrames；隱藏 `video_path` |
| `tensor_fps` | FLOAT | `30.0` | 從 `frames` 寫 temp `.mp4` 的 fps |
| `audio` | AUDIO | — | 外部音訊 pin，預設跟 source 音訊混音 |
| `keep_source_audio` | BOOLEAN | `True` | Path 模式 + 接 `audio` + source 有音訊 → `amix` 兩條；`False` = 外部蓋過 source |
| `target_fps` | FLOAT | `0.0` | `0` = 沿用 source；FLOAT 支援 23.976 / 29.97 / 59.94 等 cinematic fps |

**輸出**：`final_video_path` STRING。

**範例**：`MF_SelectVideo → MF_BurnSubtitle (font=msjh.ttc, font_size=28, alignment=bottom_center (2), outline_width=2)` 是典型 1080p YouTube 影片的配置。

---

### `MediaForge/Video`

#### 📂 Select Video (`MF_SelectVideo`)

`ComfyUI/input/` 影片檔的 dropdown picker。遞迴掃子目錄、列 `.mp4 / .mov / .mkv / .webm / .avi / .m4v / .mpg / .mpeg / .ts`。

**適用情境**：要選輸入影片但不想自己打路徑。輸出的 `video_path` 可以接任何 file-consumer node。

| Widget | 型別 | 說明 |
|---|---|---|
| `video` | dropdown | `input/` 下所有符合副檔名的檔（相對路徑、`/` normalize）。空目錄會出現提示 |

**輸出**：`video_path` STRING — runtime 透過 `folder_paths.get_input_directory()` 解析為絕對路徑。

**說明**：`IS_CHANGED` 用 file mtime 做 cache key，同檔名換內容會自動 invalidate 下游 cache。新增檔到 `input/` 之後要重新整理瀏覽器才會重掃。

---

#### 🔁 Loop Video (`MF_LoopVideo`) **(dual-input)**

把影片循環到目標時長，三種接縫處理策略，可調速、可反向。

**適用情境**：短片補長到指定時長（intro loop、ambient B-roll、social media 15s / 30s / 60s 多版本）。

**Loop modes**：
- `strict` — 硬接重複到精確時長。最快、最可預測、每個接縫看得見。
- `ping_pong` — A → A-reversed → A → A-reversed（無縫來回、有效時長翻倍）。
- `crossfade` — 重複片段間走 `xfade` chain（上限 50 圈）。`crossfade_sec >= 片段長度` 時自動退階回 `strict`。

**必填 widget**：

| Widget | 預設 | 範圍 | 說明 |
|---|---|---|---|
| `video_path` | `input/sample.mp4` | — | Tensor 模式隱藏 |
| `filename_prefix` | `MediaForge/looped` | — | → `output/<prefix>_NNNNN.mp4` |
| `target_duration_sec` | `30.0` | 0.1–36000 | 輸出時長（秒） |
| `loop_mode` | `strict` | — | `strict` / `ping_pong` / `crossfade` |
| `crossfade_sec` | `1.0` | 0.1–10 | 只在 `crossfade` mode 用 |
| `speed` | `1.0` | 0.25–4.0 | `setpts` + 自動 chain 的 `atempo` |
| `reverse` | `False` | — | 先反向再 loop |
| `keep_audio` | `True` | — | `False` 等於靜音 |
| `audio_volume` | `1.0` | 0.0–1.0 | 衰減保留的音量；`0.0` 靜音 |
| `codec` / `crf` / `preset` | smart / `18` / `medium` | — | 跟其他 producer 同一組 |

**選用輸入**：`frames` / `tensor_fps` / `audio`（dual-input 三件套）。

**輸出**：`final_video_path` STRING。

**限制**：FFmpeg `loop` filter 的 frame 緩衝上限是 `MAX_LOOP_FRAMES = 32767`（INT16）。超長素材高 fps 會撞牆 — 改用 `crossfade` mode（xfade chain 無單一 buffer 上限）或降 fps。

**範例**：5 秒短片循環成 60 秒無縫 ambient loop → `loop_mode=crossfade, target_duration_sec=60, crossfade_sec=0.5`。

---

#### 📥 Load Video Frames (`MF_LoadVideoFrames`)

FFmpeg decode 任意容器／codec → `IMAGE` batch + `AUDIO` dict + metadata。處理 VHS 的 opencv decode 跑不動的格式（AV1 / HEVC 10-bit / ProRes / VP9）。

**適用情境**：要把影片拉進 tensor-based workflow（逐幀變換、AI 推論、image-batch 操作）並同時保留音訊。

| Widget | 預設 | 說明 |
|---|---|---|
| `video_path` | `input/sample.mp4` | |
| `target_fps` | `0.0` (= 沿用原 fps) | `>0` 跑 `fps` filter 重採樣 |
| `max_frames` | `0` (= 不限) | preview / 記憶體上限 |
| `load_audio` | `True` | `False` 跳過 audio decode |
| `audio_sr` | `0` (= 沿用原 sr) | 覆寫 sample rate |

**輸出**（7 個 port）：

| 輸出 | 型別 | Shape / 意義 |
|---|---|---|
| `frames` | IMAGE | `[B, H, W, C]` float32 [0, 1] |
| `audio` | AUDIO | `{'waveform': Tensor[B, C, T], 'sample_rate': int}`；source 沒音軌時是 `None` |
| `fps` | FLOAT | 實際 fps（過 `target_fps` 重採樣後） |
| `width` / `height` | INT | Display 尺寸（rotation-aware） |
| `frame_count` | INT | 解出的 frame 數 |
| `metadata_json` | STRING | 完整 probe metadata JSON |

**說明**：旋轉感知 — 直拍手機影片會正確顯示。記憶體被 `max_frames` 上界限制。source 沒音軌時輸出 `None`（**不**合成假靜音 — 假靜音會誤導 `MF_SaveVideoFrames` 的 `-shortest` mux）。

---

#### 📤 Save Video Frames (`MF_SaveVideoFrames`)

Tensor → 編碼影片檔。Canonical 的 tensor→file producer；跟 `MF_LoadVideoFrames` 形成對稱 roundtrip（PSNR > 38 dB）。

**適用情境**：tensor pipeline 結果寫回硬碟、要 broadcast-grade codec 控制。

| Widget | 預設 | 說明 |
|---|---|---|
| `frames` | (必填 IMAGE) | `[B, H, W, C]` float32 [0, 1] |
| `filename_prefix` | `MediaForge/video` | 副檔名跟 codec 走（.mp4 / .mov / .gif 自動選） |
| `fps` | `30.0` | rawvideo pipe 的來源 fps — roundtrip 從 LoadVideoFrames 接過來時用其 `fps` |
| `codec` | smart 預設 | H.264 / HEVC / AV1 / ProRes / `gif (palette)` + NVENC variants |
| `encode_mode` | `crf` | `crf` / `bitrate` / `target_size` |
| `crf` | `18` (0–51) | `crf` mode 用。越低畫質越高／檔案越大；刻度意義隨 codec family 不同，見[智慧 GPU codec 預設](#智慧-gpu-codec-預設) |
| `bitrate_kbps` | `4000` | `bitrate` mode 用（4000 = 4 Mbps） |
| `target_size_mb` | `8.0` | `target_size` mode 用 — 從 duration 反算 bitrate |
| `preset` | `medium` | `ultrafast` … `veryslow` |
| `pix_fmt_override` | `""` | 空 = 用 codec 預設（x264/x265 走 yuv420p、ProRes 走 yuv422p10le） |

**Widget 可見性**（前端強制，`web/dual_input_lock.js`）：`encode_mode` 是三選一互斥 radio — 選其一會隱藏另外兩個 rate-control widget（例：`encode_mode=crf` 隱藏 `bitrate_kbps` + `target_size_mb`）。`codec=gif (palette)` 會隱藏整組 rate-control widget（`encode_mode` / `crf` / `bitrate_kbps` / `target_size_mb` / `preset` / `pix_fmt_override`）— GIF 沒有 CRF/bitrate/preset/pix_fmt 這些概念。`codec=prores (prores_ks)` 隱藏同一組、但 `pix_fmt_override` 除外（ProRes 仍吃 pixel format override）。

**選用**：`audio` AUDIO dict — 接了就 mux 進輸出。`codec=gif (palette)` 時會被忽略（印警告）— GIF 沒有音軌。

**輸出**：`final_video_path` STRING。

**說明**：ProRes 輸出 `.mov`（`prores_ks` 用 `yuv422p10le` 保留 10-bit 精度）。`gif (palette)` 輸出 `.gif`，走雙 pass `palettegen`/`paletteuse` filter（diff-stats palette + Bayer dither），畫質比 FFmpeg 預設 GIF encoder 好很多。其他 codec 一律 `.mp4`。NVENC 內部用 `-cq` 而非 `-crf`，但 UI 統一（見[智慧 GPU codec 預設](#智慧-gpu-codec-預設)）。

---

#### ✂️ Trim by Ranges (`MF_TrimByRanges`) **(dual-input)**

按時間區間裁切影片。主要使用情境是接 `MF_DetectSilence` 自動裁靜默，但也接受手填 JSON 做手動編輯。

**適用情境**：移除演講／podcast／直播錄影的死寂時段、從 raw footage 只留指定精華段。

**Precision 模式**：
- `precise (re-encode)` — `trim` + `setpts` re-encode（原本的行為，逐幀精確）。預設值；這個 widget 加入前存的 workflow JSON 載入後行為不變。
- `lossless (stream copy)` — keyframe-seek 分段 stream copy（`-c copy`）+ concat demuxer 合併。無需重編碼，比 `precise` 快得多——但不是真的瞬間完成：執行前會先掃描來源的 keyframe index（`ffprobe -skip_frame nokey`，只解 keyframe），超長素材這一步仍需要時間。每個 keep 段的起點一律**向後**對齊到來源下一顆 keyframe（絕不往前對齊——往前對齊會把已經被判定要移除的內容重新包回輸出，見 Codex R6-1）。若 keep 段內完全沒有 keyframe（區間比來源 GOP 還窄、剛好卡在兩顆 keyframe 中間），lossless 模式無法表示該段，節點會 **raise**——請改用 `precision=precise (re-encode)`，或放寬該段範圍。`crossfade_sec` 在這個模式無法生效（stream copy 無法混合像素）— widget 會被隱藏，殘留自 `precise` 模式的非 0 值會被**忽略**（印警告），不會 raise。每段抽取與最終合併都會 map 主 video、全部 audio、全部字幕軌，多*音軌*來源（例如英文＋評論雙音軌）不會漏軌——data/timecode 軌與封面圖（attached picture）則會被略過（印警告點名），因為 concat demuxer 給不了這類 data stream 該有的 codec 參數。來源若有一條以上主*視訊*流（例如雙 angle 的 mkv）只會保留第一條——keyframe 對齊只掃這條視訊流，若把其他視訊流也一起 map 進去，切點會落在非 keyframe、變成無法解碼；其餘會被略過並印警告點名數量。

**Ranges 輸入**（兩種、互斥 — pin 優先）：
- `ranges`（pin、選用）— 從 `MF_DetectSilence`（或 `MF_DetectScenes`）接過來的 `SILENCE_RANGES`。
- `ranges_json`（STRING widget、multiline）— 手填 JSON，例如 `[[1.5, 3.0], [10.0, 12.5]]`。

| Widget | 預設 | 說明 |
|---|---|---|
| `video_path` | `input/sample.mp4` | |
| `filename_prefix` | `MediaForge/trimmed` | |
| `mode` | `remove` | `remove` = 刪除指定區間、留其他；`keep` = 留指定區間、刪其他 |
| `ranges_json` | `"[[0.0, 1.0], [5.0, 7.5]]"` | 沒接 `ranges` pin 時用這個 |
| `crossfade_sec` | `0.0` (0–2) | 保留段之間走 `xfade`；`0` = 硬切。`precision=lossless` 時隱藏且被忽略（stream copy 無法混合像素） |
| `codec` / `crf` / `preset` | smart / `18` / `medium` | `precision=lossless` 時忽略（輸出容器改沿用來源） |
| `precision` | `precise (re-encode)` | `lossless (stream copy)` 會隱藏 `codec` / `crf` / `preset` / `crossfade_sec`（前端強制）— 這幾個在該模式下都不會傳給 ffmpeg。刻意宣告在 `optional` 的**最後一個位置**（在 `tensor_fps` 之後），不是放 `required`：ComfyUI 存檔的 widget 值是按「整個 required+optional widget 列表」的位置對齊，`tensor_fps` 已經佔了一個位置——若改放 `required` 尾端會把它擠後一位、錯位舊 workflow JSON |

**模式語意**：
- `keep` + 空 ranges → **raise**（拒絕輸出空檔）。
- `remove` + 空 ranges → **identity**（原片不動）。

**音訊同步**：音影 interleaved concat（`[v0][a0][v1][a1]…concat=n=N:v=1:a=1`），跨切點音訊不會跑掉。沒音軌的 source 輸出也無音軌（`-an`）— 不會補靜音 pad（那是 `MF_ConcatVideos` 的行為，不是這個節點）。（`precision=lossless` 改成逐段 stream copy 再 concat demuxer 合併 — 不經過 filter graph。）

**輸出容器**（僅 `precision=lossless`）：沿用來源的副檔名——path 模式沿用 `video_path` 本身的副檔名（`.mp4` / `.mov` / `.mkv` / `.webm` / `.avi` / `.m4v`；不認得的副檔名退回 `.mp4`），tensor 模式固定 `.mp4`（frames 一律先寫成 `.mp4` 暫存檔，跟下游容器無關）。例外：`video_path` 是 `.webm` 且同時外接了 `audio` AUDIO dict 時，dual-input 的預合成步驟會把音軌轉成 AAC（WebM 不支援 AAC）——輸出改退回 `.mkv` 容器，並印出警告。`precision=precise` 一律照 `codec` 選擇走（ProRes 走 `.mov`，其他 `.mp4`）。

**範例 chain**：`MF_LoadVideoFrames → MF_DetectSilence (noise_db=-30) → MF_TrimByRanges (mode=remove, crossfade_sec=0.1)` — podcast 預剪、切點微淡入淡出。長素材要零畫質損失的硬切，改接 `MF_TrimByRanges (mode=remove, precision=lossless (stream copy))`。

---

#### 🔗 Concat Videos (`MF_ConcatVideos`) **(dual-input, prepend 語意)**

多檔路徑級拼接。兩種策略對應不同速度／相容性 trade-off。

**適用情境**：同台相機素材拼接（用 `copy` 秒接 stream-copy）、跨 codec / 跨解析度素材拼接（用 `transcode` 加可選過場）。

**Modes**：
- `copy` — FFmpeg concat demuxer、stream copy 不 re-encode。極快，只保留主 video、全部 audio、全部字幕軌（`-map 0:V -map 0:a? -map 0:s?`）——data/timecode 軌（例如 GoPro/DJI 的 `tmcd`/`gpmd`）與封面圖（例如 `yt-dlp --embed-thumbnail` 內嵌的 attached picture）會被略過，並印警告點名檔案與 stream 種類，因為 concat demuxer 給不了這類 data stream 該有的 codec 參數。**要求**所有輸入通過 preflight probe 比對，比對範圍收斂成「實際會被 map 的 stream」：main video（`codec_type=video`、排除封面圖）逐 stream 比對 `codec_name`/`width`/`height`/`pix_fmt`/`profile`/`sample_aspect_ratio`、audio 逐 stream 比對 `codec_name`/`sample_rate`/`channels`/`channel_layout`、subtitle 逐 stream 比對 `codec_type`/`codec_name`——皆依 stream 數量與逐 stream 欄位比對。不一致會在跑 ffmpeg 前直接 raise，而不是 silently 產出壞檔（concat demuxer + `-c copy` 常常 exit code 0 但輸出從第二段開始 glitch、或字幕軌缺漏）。被略過的 stream 種類（data/attachment/封面圖）**不參與**比對，因為它們本來就不影響輸出。刻意**不比對** `time_base` / `level` / `r_frame_rate`——concat demuxer 本來就會 rescale timestamps、播放器對 level 差異普遍容忍，比了反而會誤殺原本可以安全 concat 的輸入組合。
- `transcode` — `filter_complex` graph 加可選 `xfade`。一定可用、一定 re-encode。

**必填 widget**：

| Widget | 預設 | 說明 |
|---|---|---|
| `video_paths` | multiline — `input/clip1.mp4\ninput/clip2.mp4` | 一行一個路徑、至少 2 條 |
| `filename_prefix` | `MediaForge/concat` | |
| `mode` | `transcode` | `copy`（快） / `transcode`（穩） |
| `transition_sec` | `0.0` (0–5) | xfade 秒數、只在 transcode mode 生效；`0` = 硬切 |
| `transition_type` | `fade` | `fade` / `wipeleft` / `wiperight` / `slideleft` / `slideright` / `circleopen` / `circleclose` / `dissolve` |
| `fps` / `width` / `height` | `30.0` / `1920` / `1080` | Transcode mode 的目標 dims |
| `crf` / `codec` / `preset` | `18` / smart / `medium` | 只 transcode mode 用 |

**選用輸入**：`frames` / `tensor_fps` / `audio` — 接線時 tensor 寫成 path[0]（prepend）、`video_paths` shift 到 path[1..N]。仍需要 `video_paths` 至少有 1 條才能湊到 2 段。

**輸出**：`final_video_path` STRING。

**說明**：`transcode` mode 沒音軌的輸入自動補 `anullsrc` 靜音；`copy` mode 只要 preflight（見上）比對出任何差異就 hard fail — 跨 source 拼接前先用 `MF_SaveVideoFrames` normalize。`copy` mode 輸出的副檔名沿用**第一個輸入**的容器（不是看 `codec` widget，`copy` mode 完全忽略它）——例如兩個 ProRes `.mov` 輸入會產出 `.mov`，不是 `.mp4`（`.mp4` muxer 沒有 ProRes tag，硬塞會 mux 失敗）。

---

### `MediaForge/Analysis`

#### 🔍 Probe Media (`MF_ProbeMedia`) **(dual-input)**

`ffprobe` 包裝、回結構化 metadata。純讀、不會跑 FFmpeg encode。

**適用情境**：餵尺寸給 Compose / Save chain（也可以留 `target_*=0` 讓 Compose 自己 probe）；檢查陌生檔再決定怎麼處理。

| Widget | 預設 | 說明 |
|---|---|---|
| `media_path` | `input/sample.mp4` | Tensor 模式隱藏 |
| `frames` / `tensor_fps` / `audio` *(選用)* | — | Dual-input 三件套（probe 內部產生的 temp `.mp4`） |

**輸出**（6 個 port）：

| 輸出 | 型別 | 說明 |
|---|---|---|
| `duration_sec` | FLOAT | 容器 `format.duration` — 當權威 timeline 用 |
| `width` / `height` | INT | **Display** 尺寸（rotation-aware）；直拍手機影片在此 swap |
| `fps` | FLOAT | 從 `r_frame_rate` parse（`30000/1001` → 29.97） |
| `video_codec` | STRING | `"h264"` / `"hevc"` / `"av1"`、無影片時 `""` |
| `audio_codec` | STRING | `"aac"` / `"opus"`、無音訊時 `""` |

**範例**：probe 一個不認識的素材、把 `width` / `height` 餵給 `MF_ComposeVideo`（或就 `target_*=0` 跳過 probe — Compose 內部本來就會做同樣的事）。

---

#### 🤫 Detect Silence (`MF_DetectSilence`)

FFmpeg `silencedetect` 包裝 → `SILENCE_RANGES` list。`MF_TrimByRanges` 的標準上游。

**適用情境**：演講 / podcast / 直播錄影預剪、找出語音段給下游 ASR。

| Widget | 預設 | 說明 |
|---|---|---|
| `audio_source` | `input/sample.mp4` | 影片或音訊檔；接了 `audio` pin 就忽略 |
| `noise_db` | `-30.0` (-90 到 0) | dB 門檻；較不負（-20）= 較積極、較負（-40）= 較嚴格 |
| `min_duration_sec` | `0.5` (0.05–60) | 最短靜音長度；短於此值視為自然停頓忽略 |

**選用**：`audio` AUDIO dict — 接了就蓋過 `audio_source` path。

**輸出**（3 個 port）：

| 輸出 | 型別 | 說明 |
|---|---|---|
| `ranges` | SILENCE_RANGES | `[[start_sec, end_sec], ...]` — wire 給 `MF_TrimByRanges.ranges` |
| `ranges_json` | STRING | 同資料的 JSON 字串（debug / preview 用） |
| `count` | INT | 偵測到的靜音段數 |

**調參**：典型 podcast — `noise_db=-30, min_duration_sec=0.5`。演講錄影帶風扇 / hum noise — 試 `noise_db=-25, min_duration_sec=1.0`。音樂帶安靜段落 — 嚴格點：`noise_db=-50, min_duration_sec=2.0`。

---

#### 🎞️ Detect Scenes (`MF_DetectScenes`)

FFmpeg 場景切換偵測（`select='gt(scene,threshold)'` + `showinfo`）→ 場景邊界 ranges。輸出跟 `MF_DetectSilence` 同一個 `SILENCE_RANGES` 連線型別，不需要任何轉接就能直接接 `MF_TrimByRanges`。

**適用情境**：把 raw footage 切成一鏡一鏡做精華集、把場景邊界餵給剪輯 pipeline、在沒有結構的素材裡找切點。

| Widget | 預設 | 說明 |
|---|---|---|
| `video_path` | `input/sample.mp4` | |
| `threshold` | `0.4` (0.05–1.0) | FFmpeg `scene` filter 分數門檻。越低越敏感（更小的畫面變化也判定為切點） |
| `min_scene_sec` | `1.0` (0–60) | 短於此秒數的場景併入前一段，避免雜訊切點把輸出切得太碎 |

**輸出**（3 個 port）：

| 輸出 | 型別 | 說明 |
|---|---|---|
| `scene_ranges` | SILENCE_RANGES | `[[start_sec, end_sec], ...]`，涵蓋整段影片（無間隙）— wire 給 `MF_TrimByRanges.ranges` |
| `ranges_json` | STRING | 同資料的 JSON 字串（debug / preview 用） |
| `count` | INT | `min_scene_sec` 合併後的場景數 |

**說明**：`SILENCE_RANGES` 這個型別名是歷史包袱 — 本質上是跟 `MF_DetectSilence` 共用的通用 `list[[start, end]]` 契約，不是靜音專屬。跟只回傳偵測到的靜音段的 `MF_DetectSilence` 不同，`MF_DetectScenes` 一定回傳涵蓋全片的 ranges。

**範例 chain**：`MF_DetectScenes (threshold=0.4) → MF_TrimByRanges (接 ranges pin, mode=keep)` — 只留想要的場景（或用 `mode=remove` 刪掉指定場景）。

---

### `MediaForge/Audio`

Phase 6 的第一個 standalone Audio 節點。跟 Compose 的音訊 *chain*（在視訊編碼「過程中」做混音/淡入淡出/normalize）不同，也跟 Analysis（只檢視、不寫檔）不同 — `MF_ExtractAudio` 把音軌落地成獨立檔案，來源可以是影片/音訊路徑，也可以是 in-memory 的 `AUDIO` dict（目前唯一能把 `AUDIO` dict 寫成檔案的節點）。

#### 🎧 Extract Audio (`MF_ExtractAudio`) **(dual-input audio)**

把影片裡的音軌抽出來（或重存一份音訊檔）成獨立音訊檔。預設 stream copy（快、零畫質/音質損失）；改 `format` 可轉碼。

**適用情境**：從影片來源產出獨立音訊檔做剪輯 / 上傳；把 in-memory 的 `AUDIO` dict（例如來自 `MF_LoadVideoFrames` 或 Compose 音訊 chain）落地成檔案。

| Widget | 預設 | 說明 |
|---|---|---|
| `audio_source` | `input/sample.mp4` | 含音軌的影片或音訊檔；`audio` pin 接上時隱藏 |
| `format` | `copy` | `copy`（stream copy，不 re-encode）/ `mp3` / `aac (m4a)` / `wav (pcm_s16le)` / `flac` |
| `filename_prefix` | `MediaForge/audio` | → `output/<prefix>_NNNNN.<ext>` |

**選用**：`audio`（AUDIO dict）— 接了就忽略 `audio_source`，改把 dict 先落地成 temp WAV。

**`format=copy` 副檔名對映**（來源 codec → 輸出副檔名，不 re-encode）：`aac → .m4a`、`mp3 → .mp3`、`opus → .ogg`、`vorbis → .ogg`、`flac → .flac`、任何 `pcm_*` → `.wav`。來源 codec 不在此清單內 → **raise**，建議改走轉碼 `format`。

**輸出**：`audio_file_path` STRING。

**說明**：接 `audio` dict 時 `format=copy`沒有字面意義（dict 沒有來源 codec）— 視為 `wav` 並印出提示。來源沒有音軌 → **raise**。

**範例**：`MF_SelectVideo → MF_ExtractAudio (format=mp3)` 從影片抽出 MP3 給 podcast feed 用；或 `MF_LoadVideoFrames → MF_ExtractAudio (接 audio pin)` 把解碼出的 `AUDIO` dict 落地成 WAV 檔。

---

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

> **Chain 語意**：每個 overlay / audio op node 都是 *append* 到上游 chain。把前一個 op 的輸出接到這個 node 的 `overlays`（或 `audio_ops`）selectable input；沒接 = 從新 chain 起點。Overlay 列表順序 = z-order；audio 是 filter chain 順序。

#### 🎬 Compose Video (`MF_ComposeVideo`) **(dual-input)**

Compose 工作流的單一終點 — 取代舊的 `ComposeStart` + `ComposeFinalize` 兩節點模式。

**適用情境**：要套用 **2 個以上效果**（overlay + 字幕、浮水印 + BGM、文字 + 音訊淡出…）。單一效果一發的話、用獨立節點（`MF_BurnSubtitle`、`MF_LoopVideo`）通常更簡單。

**必填 widget**：

| Widget | 預設 | 說明 |
|---|---|---|
| `video_path` | `input/sample.mp4` | Tensor 模式隱藏 |
| `filename_prefix` | `MediaForge/composed` | → `output/<prefix>_NNNNN.mp4`（ProRes 走 `.mov`） |
| `target_fps` | `0.0` (0–240) | `0` = 沿用 source（probe 取得） |
| `target_width` | `0` (0–7680) | `0` = 沿用 display width（rotation-aware） |
| `target_height` | `0` (0–4320) | `0` = 沿用 display height |
| `codec` / `crf` / `preset` | smart / `18` / `medium` | NVENC variants 自動偵測 |
| `keep_audio` | `True` | 沒接 `audio_ops` 時是否保留 source 自帶音軌 |

**選用輸入**：

| 輸入 | 型別 | 說明 |
|---|---|---|
| `frames` / `tensor_fps` / `audio` | dual-input | 同其他 dual-input node 的三件套 |
| `overlays` | `MF_COMPOSE_OPS` | 接 `ComposeOverlayText` / `OverlayImage` / `Watermark` / `BurnSubtitle` chain head |
| `audio_ops` | `MF_COMPOSE_AUDIO_OPS` | 接 `ComposeVolume` / `AudioMix` / `AudioFade` / `Normalize` chain head |

**輸出**：`(final_video_path: STRING, filter_complex_script: STRING)` — 第二個是編譯後的 filter graph（debug 用）。emit `ui.images` metadata 給 API `/history`。

**行為**：編譯後 graph 超過 6000 字元自動切到 `-filter_complex_script <tempfile>`（避免 Windows command-line 長度限制）。

---

#### ✏️ Compose Overlay Text (`MF_ComposeOverlayText`)

Append 一個 `drawtext` op spec 進 overlay chain。

**適用情境**：標題、lower-third、章節標籤、文字動畫進場。要由 SRT 驅動的字幕請用 `MF_ComposeBurnSubtitle`。

| Widget | 預設 | 說明 |
|---|---|---|
| `text` | `Hello MediaForge` (multiline) | 透過 `textfile=` 傳遞、安全處理單引號 / `%` / 換行 |
| `x_expr` | `(w-text_w)/2` | FFmpeg drawtext **位置表達式**（string、非數字） |
| `y_expr` | `h-text_h-40` | 距底邊 40 px — 典型 lower-third 位置 |
| `font` | 有 `msjh.ttc` 則優先,否則按字母序第一個 | 下拉選 plugin `font/` 的 `.ttf` / `.otf` / `.ttc`。`font/` 為空 → ComposeVideo 自動退階用系統字型（依 OS 取 Arial / Helvetica / DejaVu） |
| `fontsize` | `36` (8–300) | px |
| `fontcolor` | `white` | FFmpeg color 名稱 *或* hex（`#RRGGBB`） |
| `borderw` | `2` (0–20) | 外框粗細（px） |
| `bordercolor` | `black` | 外框顏色 |
| `effect` | `none` | `none` / `slide_in_left|right|top|bottom` / `marquee_horizontal` |
| `effect_duration` | `1.5` (0.1–60) | `slide_in_*`:滑入秒數;`marquee_horizontal`:跑一輪秒數 |
| `start_sec` / `end_sec` | `0.0` / `0.0` | 可見性區間。兩者 `0` = 全長顯示 |

**選用**：`overlays` — 上游 chain。沒接 = 從新 chain 起點。

**輸出**：`overlays`（`MF_COMPOSE_OPS`）。

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

---

#### 🖼️ Compose Overlay Image (`MF_ComposeOverlayImage`)

Append 通用 `overlay` op（絕對位置 + 絕對縮放）。要 preset 化浮水印改用 `MF_ComposeWatermark`。

**適用情境**：在指定位置貼一張圖（logo bug、貼紙、lower-third 姓名牌）。

| Widget | 預設 | 說明 |
|---|---|---|
| `image_path` | `input/overlay.png` | PNG / JPG 等 |
| `x_expr` / `y_expr` | `10` / `10` | 位置表達式 — 支援 `W` / `H` / `w` / `h` / `t` 等 |
| `scale_w` | `0` (0–7680) | 寬度（px）；`0` = 原圖大小（高度按 aspect ratio 自動算） |
| `start_sec` / `end_sec` | `0.0` / `0.0` | 可見性區間；兩者 `0` = 全長顯示 |

**選用**：`overlays` — 上游 chain。

**輸出**：`overlays`（`MF_COMPOSE_OPS`）。

---

#### 💧 Compose Watermark (`MF_ComposeWatermark`)

帶 placement / scale / opacity 的浮水印 preset — 是 overlay 對最常見場景的便利包。

**適用情境**：影片打 logo。`relative_scale` 讓浮水印在不同 source 解析度都保持比例。

| Widget | 預設 | 說明 |
|---|---|---|
| `image_path` | `input/watermark.png` | 建議帶 alpha 的 PNG |
| `placement` | `bottom_right` | `top_left` / `top_right` / `bottom_left` / `bottom_right` / `center` / `tile`（用真實 aspect 自動算 row × col） |
| `relative_scale` | `0.15` (0.05–0.5) | 浮水印寬度 / frame width 比例。compile 時對 `ComposeVideo.target_width` 解析 |
| `opacity` | `0.7` (0–1) | 走 `colorchannelmixer alpha`，保留 PNG 原 alpha |
| `margin_top` / `right` / `bottom` / `left` | `20` 各邊 (0–1000) | 四邊獨立 margin（px） |
| `visible_start_sec` / `visible_end_sec` | `0.0` / `0.0` | 可見性區間；兩者 `0` = 全長顯示 |

**選用**：`overlays` — 上游 chain。

**輸出**：`overlays`（`MF_COMPOSE_OPS`）。

**範例**：右下角 12% 寬、60% 不透明的 logo bug → `placement=bottom_right, relative_scale=0.12, opacity=0.6, margin_bottom=20, margin_right=20`。

---

#### 🔥 Compose Burn Subtitle (`MF_ComposeBurnSubtitle`)

把 SRT 字幕燒**進到 Compose pipeline 內** — 字幕 + 浮水印 + 音訊 mix 一次 encode 完成。Widget 跟 `MF_BurnSubtitle` 完全一致（font dropdown、完整 ASS 樣式、alignment、margins、顏色），但少了 encoder 控制（那些在 `MF_ComposeVideo`）。

**適用情境**：跟 `MF_BurnSubtitle` 一樣的場景、加上「同時還在套其他 Compose op」— 整條 chain 一次 encode。

| Widget | 預設 | 說明 |
|---|---|---|
| `srt_path` | `input/sample.srt` | UTF-8 SRT |
| `font` … `back_color_hex` | (見 `MF_BurnSubtitle` 表) | 完全相同的 widget — `font_size` / `bold` / `italic` / `outline_*` / `border_style` / `back_color_hex` |
| `alignment` / `margin_v` / `margin_l` / `margin_r` | `bottom_center (2)` / `20` / `50` / `50` | 同 `MF_BurnSubtitle` |

**選用**：`overlays` — 上游 chain。

**輸出**：`overlays`（`MF_COMPOSE_OPS`）。

**說明**：獨立的 `MF_BurnSubtitle`（在 Subtitle 分類）仍保留，給「只燒字幕不疊其他 overlay」的場景。

---

#### Audio Ops Chain — 為什麼分成四個小節點而不是合成一個？

每個音訊操作各自一個 append-only 節點、保留 chain 的**可組合性** — 跟視訊 `MF_COMPOSE_OPS` chain（`OverlayText` / `Watermark` / `BurnSubtitle`）一樣的設計：一個 op = 一個節點。

- **順序有差。** `Volume → AudioMix → Fade` 跟 `AudioMix → Volume → Fade` 出來的成品不同。Chain 順序 = wiring 順序 = ffmpeg filter 順序。
- **同一個 op 可以多次。** `Fade` 用兩次做 intro 淡入 *加* outro 淡出、或 `Volume` 分兩段階梯衰減。合成單一節點的話、要表達這些就得在 schema 上開重複的 widget 組。
- **每個節點 schema 清爽。** `Volume` 只露一個 `scale` slider、`AudioMix` 才有 BGM path + `keep_source` + dual-input `AUDIO` pin。混在一起的話、只想「把音量壓低」的人也得盯著用不到的 BGM widget。
- **易擴充。** 之後要加 EQ / reverb / pitch-shift / ducking = 再開一個節點、現有四個 schema 完全不動。

Trade-off：用三個音訊 op 的 workflow 要拉三個音訊節點 series 接起來。最簡單「只調音量」的 case 仍然只一個節點 — 成本只在多 op 疊加時才出現，而且那時候 chain 順序變成 feature、不是 friction。

---

#### 🔊 Compose Volume (`MF_ComposeVolume`)

Append `volume=N` 音訊 filter op。

**適用情境**：混 BGM 前先把 source 音壓低、或低音量錄音要 boost。

| Widget | 預設 | 說明 |
|---|---|---|
| `scale` | `1.0` (0.0–2.0) | `0.0` 靜音、`0.5` 半音、`1.0` 原音、`2.0` 2× boost（注意 clipping） |

**選用**：`audio_ops` — 上游 chain。

**輸出**：`audio_ops`（`MF_COMPOSE_AUDIO_OPS`）。

---

#### 🎵 Compose Audio Mix (`MF_ComposeAudioMix`) **(dual-input audio)**

把外部 BGM 跟 source 音訊混音、或完全用外部音源取代 source。

**適用情境**：podcast / vlog 加 BGM；旁白底下鋪 ambient sound。

| Widget | 預設 | 說明 |
|---|---|---|
| `audio_path` | `input/bgm.mp3` | BGM 檔案路徑；接了 `audio` AUDIO pin 就隱藏 |
| `keep_source` | `True` | `True` = `amix` 混 source + BGM；`False` = 捨棄 source、純粹用 BGM |
| `bgm_volume` | `0.3` (0.0–2.0) | BGM 在 mix *前*的音量衰減。`0.3` 讓 voice 蓋過 — podcast/vlog 慣例 |
| `duration` | `first` | `first`（= source 長度）/ `longest` / `shortest` |

**選用輸入**：

| 輸入 | 說明 |
|---|---|
| `audio` (AUDIO) | 接其他 node 的輸出 — materialize 成 plugin 內 `.mf_tmp/` 目錄下的 WAV。encode 完**不會**立刻清掉（這個 path 是本節點 cached 輸出值的一部分，下游 `MF_ComposeVideo` cache-hit 重跑時可能還會用到）；超過約 24h 才會被自動掃除。接了就覆蓋 `audio_path` |
| `audio_ops` | 上游 chain |

**輸出**：`audio_ops`（`MF_COMPOSE_AUDIO_OPS`）。

---

#### 🌅 Compose Audio Fade (`MF_ComposeAudioFade`)

Append `afade` op（淡入 / 淡出）。

**適用情境**：音訊不要硬接 — intro 淡入、outro 淡出。

| Widget | 預設 | 說明 |
|---|---|---|
| `direction` | `in` | `in` = 靜→全；`out` = 全→靜 |
| `start_sec` | `0.0` | fade 開始時間。`out` 通常設 `video_duration - duration_sec` |
| `duration_sec` | `2.0` (0.1–60) | fade 長度（秒） |
| `curve` | `tri` | `tri`（線性）/ `qsin`（quarter sine、最自然）/ `esin` / `hsin` / `log` / `par` / `qua` / `cub` / `squ` / `cbr` |

**選用**：`audio_ops` — 上游 chain。

**輸出**：`audio_ops`（`MF_COMPOSE_AUDIO_OPS`）。

---

#### 📏 Compose Normalize (`MF_ComposeNormalize`)

EBU R128 / streaming 級響度標準化（走 `loudnorm` 單 pass）。

**適用情境**：上傳前要符合 streaming 平台的目標 LUFS（Spotify / YouTube / Apple Podcasts）。

| Widget | 預設 | 說明 |
|---|---|---|
| `target_i` | `-16.0` LUFS (-70 到 -5) | 目標 integrated loudness。**-14** YouTube / TikTok / Spotify 音樂 · **-16** Apple Podcasts / Spotify spoken-word · **-23** 廣電 EBU R128 |
| `target_tp` | `-1.0` dBTP (-9 到 0) | True-peak 上限。`-1` dBTP 避免 consumer 端 clip |
| `target_lra` | `11.0` LU (1–50) | Loudness range；值越大保留越多動態 |
| `linear` | `True` | `True` 避免 dynamic range compression。`False` 強制壓平到 target 範圍（嚴格 LUFS、犧牲動態） |

**選用**：`audio_ops` — 上游 chain。

**輸出**：`audio_ops`（`MF_COMPOSE_AUDIO_OPS`）。

> **單 pass** 對 streaming 用途夠用。嚴格 EBU R128 broadcast 認證需要 two-pass（measure → 再套），MediaForge 目前不提供。

---

### 從 Compose v1 遷移

含 `MF_ComposeStart` / `MF_ComposeFinalize` 的 workflow JSON 載入 ComfyUI 會看到 "Missing nodes" 警告。手動 migrate 步驟:

1. 拖一個新的 `MF_ComposeVideo`。把舊 `ComposeStart` 的 `video_path` / `target_*` + 舊 `ComposeFinalize` 的 `codec` / `crf` / `preset` / `keep_audio` 填過來。
2. 刪掉舊的 `MF_ComposeStart` 跟 `MF_ComposeFinalize` 兩個節點。
3. 把 overlay chain 的最後輸出(原本是 `MF_COMPOSE` IR) 接到 `MF_ComposeVideo` 的新 `overlays` pin(類型現在是 `MF_COMPOSE_OPS`、chain 結構相同)。
4. 如果原本另外用 `MF_BurnSubtitle` 二次 encode 字幕、改用 `MF_ComposeBurnSubtitle` 串進 chain、可以併進單次 encode 省一輪。

### `MediaForge/AI` — provider-agnostic

Schema 標記為 **experimental** — `AI_CONFIG` API 在 Phase 5 內可能改，直到 Whisper / Translate 在所有 provider recipe 都 e2e 驗證完才會凍結。

#### ⚙️ AI Config (`MF_AIConfig`)

集中管理 provider 設定。輸出 `AI_CONFIG` dict、所有 AI node 吃這個 — 一處切換 provider / model / endpoint，整條 chain 跟著換。

**適用情境**：所有 AI workflow。每個 backend 配一個 `MF_AIConfig`（例如一個給 Groq ASR、一個給 OpenAI translate）、扇出接給各 consumer。

| Widget | 預設 | 說明 |
|---|---|---|
| `provider` | `openai_compatible` | `openai_compatible`（任意 `/v1/...` HTTP endpoint）/ `faster_whisper_local`（in-process） |
| `base_url` | `https://api.openai.com/v1` | 尾端 `/` 會自動 strip |
| `api_key` | `""` | **建議填 `env:OPENAI_API_KEY`** — `env:` 前綴會在執行時解析同名環境變數，secret 不會進 workflow JSON（明文 key 會原樣序列化進每一份匯出／分享的 workflow — 畫面遮罩擋不了這條路；變數不存在會給明確錯誤）。防外送保護：`env:` 解析出的 key 只允許送往信任 host（內建：`api.openai.com` / `api.groq.com` / localhost 家族）— 可用環境變數 `MF_AI_KEY_ALLOWED_HOSTS`（全域，逗號分隔 hostname）或 `MF_AI_KEY_ALLOWED_HOSTS_<變數名>`（綁單一變數）擴充，讓惡意分享的 workflow 無法用 `env:任意秘密` 配攻擊者 endpoint 外送。`env:` 解析的 key 對非 loopback host 一律要求 **HTTPS**（明文 HTTP 會暴露 Bearer header）— 只有 `localhost` / `127.0.0.1` / `::1` 可走 `http://`；內網 IP 視同遠端，即使加進 allowlist 也需要 HTTPS（或改用明文 key，兩道防護都不適用）。log 只露前 4 字 + `***`；節點畫面上明文 key 以 `•` 遮罩顯示（`env:` 引用直接顯示），點擊可編輯／顯示真值（見 `web/ai_config_mask.js`） |
| `model` | `gpt-4o-mini` | 自由字串。Whisper 認出來不像 STT id 時會自動換預設（例如 `gpt-4o-mini` 被同時餵給 translate；Whisper 退回 `whisper-1`） |
| `device` | `auto` | `cpu` / `cuda` / `auto` — 只 `faster_whisper_local` 用 |

**輸出**：`ai_config`（AI_CONFIG dict）。

**詳見 [AI Provider Recipes](#ai-provider-recipes)** — 有 OpenAI / Groq / Ollama / faster-whisper 可直接複貼的 `provider` / `base_url` / `model` 組合。

---

#### 🗣️ Whisper Transcribe (`MF_WhisperTranscribe`)

音訊檔或 in-memory `AUDIO` dict → SRT 文字（字串輸出、不是檔案）。Backend 由 `ai_config.provider` 決定。

**適用情境**：從原始錄音生字幕（訪談、演講、podcast）。SRT 可以直接 wire 給 `MF_TranslateSubtitle` 跟 `MF_BurnSubtitle` 做端到端自動字幕。

| Widget | 預設 | 說明 |
|---|---|---|
| `ai_config` | (必填 AI_CONFIG) | 接 `MF_AIConfig`。`provider` 決定 backend |
| `audio_path` | `input/sample.mp4` | 任意有音軌的 media；FFmpeg 內部抽 mono 16 kHz WAV |
| `language` | `zh` | ISO 639-1 hint（`en` / `ja` / `zh` / `ko` / ...）— 空 = 自動偵測 |

**選用**：`audio`（AUDIO dict）— 接了就蓋過 `audio_path`。Client 端先下採樣到 16 kHz mono 讓各 backend 結果一致。

**輸出**：`srt_text` STRING — 標準 SRT（多段），直接 wire 給 `MF_TranslateSubtitle.srt_text` 或 `MF_BurnSubtitle.srt_text`（要另外存檔的話走 `MF_ConvertChinese` 給 `filename_prefix`）。

**Backend 語意**（看 `ai_config.provider`）：
- `openai_compatible` — POST 到 `<base_url>/audio/transcriptions`。支援 OpenAI、Groq、任意 OpenAI-API 相容 server。需要 `pip install requests`。
- `faster_whisper_local` — lazy-import `faster-whisper`。吃 `ai_config.device`（`cpu` / `cuda` / `auto`）跟 `ai_config.model`（`tiny` / `base` / `small` / `medium` / `large-v3`）。第一次跑會下載到 HF cache。需要 `pip install faster-whisper`。

**Model 自動替換**：`ai_config.model` 看起來不像 STT model id（例如同一個 `AI_CONFIG` 還要餵 Translate、所以是 `gpt-4o-mini`），Whisper 會自動換成 provider 預設 — OpenAI-compatible 用 `whisper-1`、faster-whisper-local 用 `base`。要明確指定 STT 模型（`whisper-large-v3` / `distil-large-v3` 等）直接填到 `ai_config.model`。

**早期 raise**：source 沒音軌 → 友善錯誤。抽出 WAV < 256 bytes（靜音 / 損毀）→ 在送 backend 前先 raise。

---

#### 🌐 Translate Subtitle (`MF_TranslateSubtitle`)

SRT 文字 → 翻譯後 SRT（時間戳保留）。走 `/v1/chat/completions`、用編號 batch prompt 保證對齊。

**適用情境**：把 AI 生成的字幕在地化到其他語言。要簡↔繁 normalize 再 pair `MF_ConvertChinese`。

| Widget | 預設 | 說明 |
|---|---|---|
| `ai_config` | (必填 AI_CONFIG) | 必須 `provider=openai_compatible`（沒有本地 LLM 模式 — 本機 LLM 把 OpenAI-compatible server URL 填到 base_url 即可） |
| `srt_text` | `""` (multiline) | Wire 來自 `MF_WhisperTranscribe.srt_text` 或手填 |
| `target_lang` | `繁體中文` | 自由字串 — `English` / `日本語` / `한국어` / `Español` / ... |
| `system_prompt` | (預設 prompt、支援 `{target_lang}` 占位字串) | 改 prompt 控制語氣（technical / 口語 / formal） |
| `batch_size` | `20` (1–200) | 每次 LLM call 行數。較小 = 對齊較穩但慢；較大 = 快但小模型可能漂掉 |

**輸出**：`translated_srt` STRING。

**行為**：每批用 `[1] ...` / `[2] ...` 編號送 prompt、response 用同 pattern parse。LLM 漏行 / 合行造成行數不對時 node **raise**（不會吐錯位的字幕）— 降 `batch_size` 或換更強的 model 重試。

**推薦模型**：`gpt-4o-mini`（快又便宜、batch 30 內 OK）；`gpt-4o` / `llama-3.3-70b-versatile`（長段或專業詞彙更穩、可吃 batch 50+）。

## AI Provider Recipes

`MF_AIConfig` 輸出的 dict 由所有 AI 節點消費。同一個 `provider` / `base_url` / `api_key` / `model` 介面 — 只是值換。可直接複貼的組合：

### OpenAI（官方）

```
provider   = openai_compatible
base_url   = https://api.openai.com/v1
api_key    = sk-...
model      = whisper-1         # 給 MF_WhisperTranscribe
           = gpt-4o-mini       # 給 MF_TranslateSubtitle
```

要錢；最穩定。`whisper-1` 是多語 GA endpoint。

### Groq（市場最快的 hosted Whisper）

```
provider   = openai_compatible # OpenAI 相容 API 介面
base_url   = https://api.groq.com/openai/v1
api_key    = gsk_...
model      = whisper-large-v3  # ASR — 同模型家族下比 OpenAI whisper-1 快 ~5–10 倍
           = llama-3.3-70b-versatile  # 翻譯
```

有免費額度（受限速）；要在 1 分鐘內字幕 1 小時 podcast 時就靠它。

### faster-whisper（本地，不需要 API key）

```
provider   = faster_whisper_local      # 設在 MF_WhisperTranscribe
device     = cuda                      # 或 "cpu", "auto"
model      = large-v3                  # 首次使用會下載到 HF cache
```

要先 `pip install faster-whisper`。隱私 / 離線 workflow 首選。CPU 可跑但慢（現代筆電大約 real-time × 0.3）；非小檔建議 CUDA。

### Ollama / LM Studio（本地 OpenAI 相容）

```
provider   = openai_compatible
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
- **MF_COMPOSE_OPS**：`list[dict]` — 純 op-spec dict（`{"type": "drawtext" | "overlay" | "watermark" | "subtitle", "params": {...}, "image_path"?: ...}`），由 `MF_ComposeOverlayText` / `MF_ComposeOverlayImage` / `MF_ComposeWatermark` / `MF_ComposeBurnSubtitle` append。Compose v2 的視訊 chain 連線型別。
- **MF_COMPOSE_AUDIO_OPS**：`list[dict]` — 音訊 chain 版本（`{"type": "volume" | "amix" | "afade" | "loudnorm", "params": {...}}`），由 `MF_ComposeVolume` / `MF_ComposeAudioMix` / `MF_ComposeAudioFade` / `MF_ComposeNormalize` append。`MF_ComposeVideo` 在 compile 時透過 `utils/compose_ops.py` dispatch 把兩條 chain 解析進內部的 `ComposeIR` dataclass（`utils/compose_ir.py`）— IR 本身已經不是跨節點的連線型別（那是 Compose v1 的 `MF_COMPOSE`，隨 `ComposeStart`/`ComposeFinalize` 一起除役）。
- **AI_CONFIG**: `dict`，keys 為 `provider / base_url / api_key / model / device`（`api_key` 是解析後的值 — `env:` 間接引用由 `MF_AIConfig` 在 dict 離開節點前展開）。Experimental。

## Architecture

```
comfyui_MediaForge/
├── __init__.py              # pkgutil 自動發現 nodes/ — 丟檔進去就出現
├── pyproject.toml
├── requirements.txt         # ffmpeg binary fallback + 輕量純 Python 相依；大型選用相依仍走 lazy import
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
│   ├── detect_scenes.py        # MF_DetectScenes  — MediaForge/Analysis
│   ├── detect_silence.py       # MF_DetectSilence
│   ├── extract_audio.py        # MF_ExtractAudio  — MediaForge/Audio（Phase 6 第一個節點）
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
└── tests/                   # 開發者本地 pytest suite — gitignored，不隨公開 repo 發佈（見「測試」章節）
    ├── test_compose_ir.py        # IR spike case（Phase 4 prerequisite）
    ├── test_compose_e2e.py       # real-ffmpeg e2e
    ├── test_video_io_roundtrip.py# PSNR > 38 dB rawvideo roundtrip
    └── test_codex_r*_fixes.py    # 各輪 codex review 留下的回歸測試
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

重啟 ComfyUI — 節點會出現在 `MediaForge/Subtitle | Video | Analysis | Audio | Compose | AI`。

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
| 檔名含 `[ ] ' , ;` 等 filter 特殊字元，字幕/drawtext/overlay 節點出錯 | 跟 `:` 同一個根因 — 這些字元在 filtergraph 語法裡也有特殊意義 | 同樣自動處理：`escape_filter_path()` 現在做完整的兩層 FFmpeg escape（`: ' \ [ ] , ;`），不只 colon。只影響 filter graph 內的路徑（`subtitles=`、`textfile=` 等）— 純 `-i` 參數本來就不需要 escape |
| `MF_ConcatVideos` `mode=copy` raise 出一份逐檔 codec/解析度/pix_fmt/音訊差異列表 | Preflight probe 發現輸入彼此不相容、無法 stream copy | 正常行為 — concat demuxer + `-c copy` 對不相容輸入以前會 exit 0 但輸出從第二段開始壞掉。改用 `mode=transcode`（一定可用、一定 re-encode） |
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

**Q：standalone Audio domain 目前有什麼？**
A：Phase 4.5 已有「Compose chain 內」的音訊 op(Volume / AudioMix / Fade / Normalize、跟視訊 overlay 共享一次 encode)。Phase 6 在這之上加獨立的檔案級 Audio 節點：`MF_ExtractAudio`（把音軌抽出 / 落地成檔案）先上，denoise、normalize-file、cut/trim、ducking 仍在規劃中。

## 測試

`tests/` 是開發者本地的 pytest suite（IR 編譯、real-ffmpeg roundtrip、各輪 review 累積下來的回歸測試），已從公開 repo 與套件中排除 — `.gitignore` 排除，一般 `git clone` 或 ComfyUI Manager 安裝都不會帶到這個目錄（這是預期行為，不是安裝壞掉）。若你的 checkout 剛好還留著這個目錄（例如 contributor checkout），可以跑：

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
- **Phase 6** 🚧 獨立 Audio domain — `MF_ExtractAudio` 先上；檔案級 denoise、normalize-file、audio cut/trim、ducking 仍規劃中(Phase 4.5 的 Compose audio chain 是子集)
- **Phase 7** ⏳ Net domain — yt-dlp ingest、HTTP fetch(lazy import)

## License

MIT — © YingLiang Lu (leon80148)。

## 致謝

- **FFmpeg** — 真正幹活的引擎；這個 plugin 90% 是在格式化 argument
- **ComfyUI** — runtime 跟節點圖引擎
- **VideoHelperSuite** — 先行作品，定義了 IMAGE-batch 契約，MediaForge 與其互通
- **faster-whisper / CTranslate2** — 本地 Whisper backend
