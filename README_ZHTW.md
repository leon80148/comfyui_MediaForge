# MediaForge

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg) ![License MIT](https://img.shields.io/badge/license-MIT-green.svg) ![ComfyUI](https://img.shields.io/badge/ComfyUI-custom__nodes-orange.svg)

> **給進階使用者的 ComfyUI FFmpeg 工具集。** Tensor↔影音橋接，帶廣播級編碼控制。深度 > 廣度。

FFmpeg 驅動的 custom_nodes plugin：字幕燒入、影片循環、媒體 probe、tensor-native 影格 I/O、靜音偵測、多片段拼接、單次編碼多層 overlay 的 Compose pipeline、provider-agnostic 的 AI 字幕／翻譯，以及把長片拆成重疊分段生成的時間軸。**影音節點都是 FFmpeg 的薄包裝**；Timeline 節點不載入、也不 patch 模型——只改寫你傳入的 `LATENT`、用你接上的 `VAE` 解碼；MiniMax H3 的 conditioning 與取樣只透過執行 ComfyUI core 節點類別完成，最後交給 FFmpeg 編碼。AI 節點靠 `MF_AIConfig` 連線傳遞 provider 設定，一處切換即可整批換 backend。

📖 **[English README →](README.md)**

## 亮點

- 🎞️ **30 個節點** 分散在 6 個分類 — Subtitle / Video / Analysis / Audio（Phase 6 開張）/ Compose（含 audio chain）/ AI（Net / Image 規劃中）。另有 3 顆已註冊的內部節點，由 Sample Segments 執行時自己建立
- 🔗 **Dual-input bridge** — file-consumer node 同時接受 `video_path` 字串 *或* in-memory 的 `IMAGE + AUDIO + tensor_fps` 三件套，VHS / AnimateDiff / 任意 IMAGE-pipeline plugin 都能直接 wire 進 MediaForge、不必 SaveVideoFrames 來回 round-trip
- 🚀 **智慧 GPU codec 預設** — 偵測到 NVENC 自動用 `h264_nvenc`，沒 GPU 的機器自動 fallback `libx264`。不必手動切、不會在沒卡的環境壞掉
- 📡 **API-ready 輸出** — 每個產出檔案的節點都同時 emit ComfyUI `ui.images` metadata，`/history/<prompt_id>` 直接看到輸出檔名，`/view?filename=X&subfolder=Y&type=output` 可下載（見 [Using via API](#using-via-api)）
- 🎙️ **內建音訊混音** — BurnSubtitle 的 `keep_source_audio`（預設開）用 `amix` 把外部 audio pin 跟 source 自帶音軌混在一起；支援 cinematic fps（23.976 / 29.97 / 59.94 等 FLOAT 值）
- 🧪 **廣播級編碼控制** — H.264 / HEVC / AV1 / ProRes，支援 CRF / bitrate / target-size 三種編碼模式；NVENC variants（`h264_nvenc` / `hevc_nvenc` / `av1_nvenc`）由 `ffmpeg -encoders` probe 自動加入 dropdown
- 🎚️ **單次編碼、多層 overlay 的 Compose pipeline** — `filter_complex` graph 編譯器，N 層 overlay 仍只走一次 re-encode
- 🤖 **Provider-agnostic AI** — `MF_AIConfig` 讓 Whisper / Translate 在 OpenAI / Groq / OpenRouter / Gemini / Ollama / 本地 backend 之間一處切換
- 🎬 **Video Batch** — 十分鐘的影片也能分批通過任何 IMAGE 節點（放大、遮罩、H3）：兩個節點、一個佇列項目，畫面留在磁碟而不是 RAM，最後產出一支完整的影片。見 [Video Batch](#video-batch批次處理長影片)
- 🧵 **時間軸與長片** — 在附素材庫（參考圖片 / 影片 / 音訊、關鍵幀、錨定片段、鎖定原音）的視覺化編輯器規劃互相重疊的分段；一顆取樣節點逐段生成，把每段 latent 尾端續接到下一段、逐段解碼並暫存到磁碟，最後由一顆節點只編碼一次。只支援 MiniMax H3 AV。同一個編輯器也能標出要從現有影片剪出的區間（見[時間軸與長片](#時間軸與長片)）
- 🪶 **輕量相依** — `requirements.txt` 只有四個小型純 Python 套件，加上找不到系統 `ffmpeg` / `ffprobe` 時才用的 `imageio-ffmpeg` / `static-ffmpeg`。全部用到才 import；`faster-whisper` 維持選裝
- 🔁 **Tensor-native** — `IMAGE [B,H,W,C] float32` 跟 ComfyUI canonical 的 `AUDIO` dict；rawvideo roundtrip PSNR > 38 dB

## 目錄

1. [Quick Start](#quick-start)
2. [Video Batch（批次處理長影片）](#video-batch批次處理長影片)
3. [時間軸與長片（Timeline & Long Video）](#時間軸與長片)
4. [Using via API（API 工作流）](#using-via-api)
5. [智慧 GPU codec 預設](#智慧-gpu-codec-預設)
6. [為什麼選這個 plugin（vs VideoHelperSuite）](#為什麼選這個-pluginvs-videohelpersuite)
7. [節點清單（30）](#節點清單30)
8. [AI Provider Recipes](#ai-provider-recipes)
9. [Hidden Contracts（內部型別契約）](#hidden-contracts內部型別契約)
10. [Architecture](#architecture)
11. [系統需求](#系統需求)
12. [安裝](#安裝)
13. [疑難排解](#疑難排解)
14. [常見問題](#常見問題)
15. [測試](#測試)
16. [Roadmap](#roadmap)
17. [License & 致謝](#license)

## Quick Start

四個逐漸複雜的最小 workflow。每個都以節點鏈描述 — 在 ComfyUI 裡照圖接線即可。

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

### 3. AI 自動字幕：轉錄 → 翻譯 → 燒入（5 個節點）

```
[AIConfig (ASR)]──▶[WhisperTranscribe]──srt──▶[TranslateSubtitle]──srt──▶[BurnSubtitle]──▶ output.mp4
[AIConfig (LLM)]─────────────────────────────────▲
```

| 節點 | 關鍵設定 |
|---|---|
| `MF_AIConfig` (ASR) | `provider = "openai_compatible"`, `base_url = "https://api.groq.com/openai/v1"`, `model = "whisper-large-v3"` |
| `MF_WhisperTranscribe` | `audio_path = "interview.mp4"` |
| `MF_AIConfig` (LLM) | `provider = "openai_compatible"`, `base_url = "https://api.openai.com/v1"`, `model = "gpt-6-luna"` |
| `MF_TranslateSubtitle` | `target_lang = "繁體中文"` |
| `MF_BurnSubtitle` | 把翻譯後 SRT wire 進 `srt_text`，設定 ASS 字型、顏色、外框 |

具體 `base_url` / `model` 組合可直接複貼 — 見下方 [AI Provider Recipes](#ai-provider-recipes)。

### 4. H3 長片（3 個 MediaForge 節點）

把一支長 AV 片段拆成互相重疊的 107 幀分段生成，最後只編碼一次。開啟 `example_workflows/timeline_h3_basic.json`（ComfyUI 的範本瀏覽器會列出）並選自己的模型檔，或手動接線：

1. `⏱️ Edit Timeline` — `fps = 24`、`mode = generate`；三段 `[0,107)`、`[85,192)`、`[153,260)`，每段一個 prompt。
2. `🎛️ Sample Segments` — `segment_plan` ← Edit Timeline；`model`、`clip`、`vae`、`audio_vae`、`sampler`、`sigmas` 接你自己的 core 載入器 / 取樣器節點；`mask = hard`、`carry_audio = True`。
3. `🧵 Assemble Segments` — `state` ← Sample Segments → 一個 `.mp4`（260 幀、32 kHz 音訊）。

編輯器、素材與限制見[時間軸與長片](#時間軸與長片)。要用參考圖片、錨定片段與鎖定原音：`example_workflows/timeline_h3_director.json`。不生成、只剪現有影片：`example_workflows/timeline_cut.json`。

## Video Batch（批次處理長影片）

> 讓長影片通過任何 IMAGE 節點，而不必把整支影片放進 RAM。畫面一批一批送進你的節點，每批處理完就寫到磁碟，最後產出一支完整的影片。不用接迴圈，也不用計算總幀數。

```
🎞️ Start Video Batch ── frames ──► 你的節點 ── frames ──► 🎬 End Video Batch
```

**快速試用**：放這兩個節點，中間接你的節點（例如放大模型），在 Start 選影片，按 Run。End 的 `frames_per_batch` 是每批送進你的節點的幀數，它決定記憶體峰值；RAM 或 VRAM 不夠時調低它。

**按下 Run 之後**：全程只佔一個佇列項目、一筆歷史。

1. 先檢查整個工作：來源、接線、音訊。有問題就在你的節點拿到任何畫面之前停止。
2. End 要一批，Start 只用 FFmpeg 解那一段，你的節點處理它。每批重跑的都是同一批節點。在 ComfyUI 一般的快取模式下，節點是同一個 instance，所以它自己留著的模型只載入一次；`--cache-none` 會重建。
3. End 把畫面以無損 FFV1 寫到 `.mf_tmp/seg_*`，從記憶體放掉，再要下一批。第一批之後還會試編碼幾幀，所以奇數尺寸或編碼器不可用會立刻報錯，不必等整片跑完。
4. 來源結束後，End 把分段編成一支影片、驗證、顯示在節點上，並刪掉分段。

End 節點會顯示「第 k / 約 N 批」、「正在組裝成片…」、「完成」。進度條要等成品驗證通過才會滿。狀態下面會列出每支來源的 fps、長度、成片是否用它的音軌，以及 End 轉換了什麼，例如「bg.mp4：24 → 25 fps」。classic 前端的 End 預設大約只顯示兩行，把節點拉高就能看到全部；server log 也有完整摘要。

**Video Batch 還是 Timeline？**

| | Video Batch | [Timeline](#時間軸與長片) |
|---|---|---|
| 用途 | 處理現有影片：放大、遮罩、重繪 | 依計畫分段生成新影片，段與段接續 |
| 長度 | 來源的幀數 | 在編輯器裡規劃的長度 |
| 接線 | 2 個節點加上你自己的 | 6–8 個節點，H3 可用 Sample Segments |

**分批**：`frames` 永遠剛好是 `frames_per_batch` 幀。最後一批不足時重複最後一幀，End 再裁回去，所以需要固定長度的模型也能用。你的節點必須回傳和收到一樣多的幀，改變幀數的節點（例如插幀）請放到 End 之後，接 `final_video_path`。批與批之間不會傳遞 latent 或時序資訊，時序模型可能在接縫留下痕跡。`overlap_frames` 會淡接接縫，但只是混合結果，不能給模型更多上下文。結果不保證等同一次處理整支影片。

**MiniMax H3**：`frames_per_batch` 必須符合 17n+5（例如 124），`fps = 24`。其他值會在模型執行前被拒絕。在 150 批（12.9 分鐘）的實測中，每批越跑越慢，從約 141 秒變成 183 秒，整段比照前幾批的速度多花約 16% 的時間，原因還沒找到。

**音訊**：由 End 的 `audio` 與 `keep_source_audio` 決定成片音軌：

| End 的 `audio` | 來源 | 成片音軌 |
|---|---|---|
| 未接，`keep_source_audio` 關閉 | 任何 | 無 |
| 未接 | 所有來源都沒有音軌 | 無 |
| 未接 | 一個來源有音軌 | 該來源的音軌，以第一個影片幀為起點 |
| 未接 | 兩個來源都有音軌 | 模型執行前報錯：接 `audio`，或關閉 `keep_source_audio` |
| 接批次外的節點（Load Audio） | 任何 | 該音訊，只讀一次，從影片開頭對齊；不足補靜音，過長裁切 |
| 接批次內的節點（Start 的 `audio`、VAE Decode Audio） | 任何 | 每批的音訊，逐批取用 |

Start 的 `audio` 輸出只有接線才解碼，`keep_source_audio` 關閉時也一樣；來源沒有音軌時接它會報錯。重疊區的音訊用線性淡接，同一段原音在接縫不會變大聲。

**兩支以上的影片**：每支影片用一個 Start，都接進你的節點，最後接到同一個 End。最短的那支決定長度。批次設定都在 End，不會互相衝突。fps 不同而 End 的 `fps` 是 0 時，End 用成片保留音軌的那支的 fps，沒有保留音軌就用最高的，其他支自動轉換，並在節點上顯示。有一支低於 5 fps 時會停下，因為那幾乎都是上游設錯，例如 Loop Video 的 `tensor_fps`；確定要這樣，就在 End 填 `fps`。

**磁碟與記憶體**：分段在 1344×768 約每幀 0.5 MB，十分鐘 1080p 30 fps 粗估約 20 GB，一直佔用到最後編碼完成。End 每批更新估計，暫存或輸出磁碟看起來不夠就報錯。Start 解碼前依一批的大小檢查 RAM，End 在第一批後再依實際輸出尺寸檢查一次。這些都是估計，不是保證；你的節點自己用多少記憶體由節點決定。

**Stop**：按下後不會再開始新的一批。正在跑的節點會在下次檢查 Stop 時停下，FFmpeg 也會一起結束；來源卡住時可能延遲。不提供部分成片。分段會在下一個 Video Batch 工作開始時刪除。當機殘留的分段，由啟動時的清理在寫入它的程序已結束且超過 24 小時後刪除。

**和 VHS Meta Batch Manager 比較**：實測環境是 ComfyUI 0.42.5、VHS commit `4d907be`，影片 11.6 分鐘 720p，每批 64 幀，兩邊都用 x264 CRF 19、preset medium。Video Batch 的分段放在 Linux 磁碟上。

| | VHS Meta Batch Manager | Start / End Video Batch |
|---|---|---|
| 節點 | Batch Manager，接到 Load Video 和 Video Combine | Start 和 End，取代 Load 和 Combine |
| 佇列與歷史 | 每批一筆（這裡是 261 筆） | 一筆 |
| 音軌 | 另一個檔案 `-audio.mp4` | 在同一個輸出檔裡 |
| 按 Stop 之後 | FFmpeg 繼續開著，檔案打不開，要等下次 Run 才會收尾。收尾後有已寫入的幀，沒有音訊 | 不再開始新的一批，FFmpeg 也會結束，沒有部分成片 |
| 批次間重疊 | 無 | `overlap_frames` |
| 額外磁碟 | 不需要 | 約 4 GB 分段，到最後編碼完成為止 |
| 時間：有音軌的影片，不接模型 | 1191 秒 | 281 秒 |
| 時間：沒有音訊，不接模型（VHS 把檔案的音軌移除；Video Batch 關閉 `keep_source_audio`） | 236 秒 | 286 秒（較早的版本） |
| 時間：FlashVSR，50 批，每批 16 幀，480×270 | 940 秒 | 1075 秒 |
| RAM 峰值：ComfyUI 與 FFmpeg | 2.43 + 0.56 GiB | 2.35 + 0.60 GiB |
| RAM 峰值：FlashVSR 的 ComfyUI | 11.54 GiB | 11.30 GiB |

這幾次實測的 RAM 峰值很接近。沒有音訊時 VHS 比較快，因為它邊跑邊編碼，Video Batch 最後要再編碼一次。在這個 ComfyUI 版本上，影片有音軌時，VHS 每一批都會解碼整條音軌（這裡每批約 3.6 秒，片子越長越久），即使音訊輸出沒有接線：core 的記憶體掃描會走訪每個節點的輸出，走訪時就觸發了 VHS 的延遲解碼。這是兩個專案碰在一起才出現的情況，不是 Meta Batch 的設計造成的，任何一邊更新都可能消失。接真模型時，幾乎所有時間都花在模型上。FlashVSR 自己每批的時間在不同天之間差了約 17%，上面兩次 FlashVSR 不是同一天量的，看不出誰快：和 VHS 同一天量的較早版本 Video Batch 是 900 秒。在 FlashVSR 這幾次裡，Video Batch 自己的工作（讀窗口和提交）每批約 0.4 秒。標「較早的版本」的那列是最後幾輪審查修正前量的。

暫存磁碟不夠，或想在 Stop 後留下已寫入的幀，選 VHS。想要佇列只有一筆、一個檔案就有聲音、Stop 後沒有 FFmpeg 留著跑，或需要批次間重疊，選 Video Batch。

**尚未支援**：重啟後續跑、Stop 後的部分成片、一個工作流多個 End、批次內改變幀數的節點、MPEG-TS 與 MPEG 檔、HDR 與廣色域影片、批次內放 core Start / End Loop。

**範例工作流**（在 `example_workflows/`；模型欄位是 `SELECT_*` 佔位，請選自己的檔案）：

| 檔案 | 內容 |
|---|---|
| `video_batch_upscale.json` | Start 與 End 之間接放大模型，保留原音 |
| `video_batch_h3.json` | MiniMax H3 逐批重繪：124 幀、24 fps |

## 時間軸與長片

> 將長片拆成互相重疊的短段。Latent 是模型使用的壓縮表示；每一段從上一段的 latent 尾端開始，用遮罩指定這部分可以改變多少，其餘由 sampler 生成。每段完成後解碼、去掉重複的重疊並暫存到磁碟，最後一次編碼成影片。這能減少同時保留的解碼畫面，但不保證接縫無痕或總記憶體固定。

生成只有一種接法，剪片也只有一種：

| 要做的事 | 節點 | 需要 |
|---|---|---|
| 生成長片 | `⏱️ Edit Timeline`（`mode = generate`，可帶素材也可不帶）→ `🎛️ Sample Segments` → `🧵 Assemble Segments` | MiniMax H3 模型（唯一支援的模型家族）、24 fps |
| 剪現有影片 | `⏱️ Edit Timeline`（`mode = cut`）→ `✂️ Trim by Ranges`（`mode = keep`） | 一支放在 `input/` 的影片；不需要模型 |

**生成。** `🎛️ Sample Segments` 接你的 model、CLIP、VAE、audio VAE、sampler 與 sigmas，執行時逐段展開成一組節點：`🧩 Encode H3 Segment` → `🧬 Extend Latent` → core `RandomNoise` + `BasicGuider`（接了 `negative` 則用 `CFGGuider`）+ `SamplerCustomAdvanced` → `➕ Append Segment`。它只負責把 ComfyUI core 節點與 MediaForge 節點接起來 —— 沒有自己的取樣程式碼，也不載入或 patch 模型。其中三顆 MediaForge 節點是[內部節點](#內部節點)：你不需要自己放，節點選單裡也不會出現。展開內部出錯時，錯誤顯示在 Sample Segments 節點上，訊息開頭的標籤是內層節點名稱（例如 `[Encode H3 Segment]`）。模型載入、text encoder、sampler 與 scheduler 仍用你自己的 core 節點。

**剪片。** `cut` 模式的 Edit Timeline 需要來源 `video`，由你標出要保留的區間。把它的 `ranges` 與 `video_path` 輸出接到 `✂️ Trim by Ranges`、`mode` 選 `keep`：區間會從同一個檔案剪出來、依序接起來。區間是用秒傳過去的，成品沿用來源影片自己的幀率 —— 以三個 48 幀（24 fps 時間軸）的區間實測：24 fps 來源得到的正好是這些區間的 144 個來源幀；30 fps 來源得到 180 幀，同樣是 6.000 秒。Trim by Ranges 不是 output node，後面要再接一顆 output node（範例用 core `PreviewAny` 顯示成品路徑），否則 ComfyUI 沒有東西可以執行。cut 模式不接受素材，也沒有 prompt。

### 範例 workflow

ComfyUI 會把 plugin 的 `example_workflows/` 資料夾列在範本瀏覽器。模型欄位是 `SELECT_*` 佔位值 —— 請選自己的檔案。codec 為 `h264 (libx264)`。

| 檔案 | 內容 |
|---|---|
| `timeline_h3_basic.json` | 純文字：3 段 MiniMax H3、每段 107 幀、重疊 22 / 39 → 260 幀。選好模型檔就能執行 |
| `timeline_h3_director.json` | 2 段帶素材的 MiniMax H3：一張圖片參考、一支影片參考 + 錨定片段、一段鎖定原音；Assemble `lock_audio = original`。三個素材檔是 `input/mediaforge_timeline/` 下的佔位檔名 —— 請換成自己的 |
| `timeline_cut.json` | 不需要模型：Edit Timeline（`cut`）→ Trim by Ranges（`ranges` + `video_path`、`mode = keep`）→ 預覽。請先選自己的影片 |

### 時間軸編輯器

Edit Timeline 以視覺化編輯器取代原始 `timeline_data` 文字框。看得到的三個原生 widget 顯示白話標籤（`來源影片（video）`、`每秒幀數（fps）`、`用途（mode）`，以及 `生成新影片（generate）` 這類選項），只改顯示：存檔與 API 用的名稱和值不變。第四個 `grid` 只剩一個值，維持隱藏。

版面由上而下：預覽與素材庫、工具列、軌道（左欄標示分段 / 參考 / 指定畫面 / 保留原音，空軌道有用途提示）、狀態列、inspector。時間一律秒數在前，例如 `4.46 秒（107 幀）`。`？` 開啟名詞說明與快捷鍵。

作用對象：點選或 Tab 到一段就是編輯對象；播放頭不在該段時會跳過去（`generate` 跳到扣除開頭重疊後的位置，`cut` 跳到來源區間起點）。之後點或拖曳尺規、方向鍵、播放都只移動播放頭，不會換編輯對象。按鈕會寫明作用在哪裡，例如 `＋ 用在第 2 段`、`＋ 在 3.54 秒指定畫面`。

分段（`generate`，生成新影片）：

- 拖曳分段邊界。右邊界改段長、左邊界改與前段的重疊，後續各段跟著平移（ripple），邊界會吸附到合法值（段長 5 + 17n 幀；重疊 0 或 5 + 17j 幀）。
- inspector 第一層：這段的 prompt、參考素材標籤、長度 `[−] 4.46 秒（107 幀） [＋]`（只走合法值）、`從這段開始新鏡頭` / `接續前一段`。
- `進階設定`：與前段重疊、精確起點 / 終點（幀）、成品銜接方式（直接切換，或交叉淡化 + 淡化長度 + 聲音淡化方式）、seed。
- 自動分段：段數、每段長度、與前段重疊。下拉列出可用的值並顯示秒數與幀數，也可自訂。套用時依序保留各段的 prompt、seed 與仍放得下的交叉淡化，不會搬動素材；預覽會先說明哪幾段會被移除、哪些淡化會重設、參考歸屬如何改變，以及套用後會出現的問題。一個 undo 步驟。

剪輯（`cut`，剪現有影片）：

- 播放或拖曳尺規找位置，`設為起點 I` / `設為終點 O` 直接提交，各一個 undo 步驟。起點超過目前終點時整段移過去並保留長度；碰到下一段或來源結尾時縮短，並說明新長度。
- `在目前位置新增一段` 依影片順序插入；位置已在某段內或已是來源結尾時會拒絕並說明。`刪除這段`（或 `Delete`）可刪任一段，最後一段不能刪。區間一律依影片順序、互不重疊。
- 拖曳邊界以整數幀在相鄰兩段之間移動；可匯入 🎞️ Detect Scenes 的 `ranges_json`。
- `cut` 沒有 prompt 欄位：只剪片時沒有東西會讀 prompt。
- 資料裡還有素材時無法執行，狀態列提供 `移除素材`（檔案仍在 `input/`，prompt 不動）與 `切回生成新影片`。

來源影片：預覽選定的來源；瀏覽器無法播放的 codec 會改用小尺寸、無聲的 H.264 proxy。`上傳來源影片` 把來源存到 `input/mediaforge_timeline/`。沒有可播放內容時播放鍵停用並說明。播放的是來源影片或選中的素材，不是生成結果。

素材（`generate` 模式）：

- **素材庫** —— 按「加入素材」（可多選），或從桌面把圖片 / 影片 / 音訊檔拖進編輯器。檔案每次兩個上傳到 `input/mediaforge_timeline/`；一批上傳完成後合併成一個 undo 步驟；失敗的檔案留成卡片，可「重試 / 刪除」。卡片顯示縮圖或圖示、名稱、讀取後的長度與狀態（上傳中 / 讀取中 / 已讀取 / 讀取失敗）。選取卡片會在預覽區預覽該素材。名稱就是 prompt 別名，由檔名產生；在 inspector 改名時 prompt 會一起改寫。刪除素材會移除它的參考與指定，不會刪掉已上傳的檔案。
- **軌道** —— 把卡片拖到分段下方三條軌道之一：拖曳時可放的軌道亮起、不可放的變暗，落點是否合法仍在游標位置判斷並說明原因。也可以選取卡片後用 inspector 的按鈕放置，按鈕上的時間就是實際落點，會跟著播放頭更新。所有卡片都要等讀取完成；表外的組合會被拒絕。

  | 卡片 | 參考 | 指定畫面 | 保留原音 |
  |---|---|---|---|
  | 圖片 | 參考，範圍 = 落點所在段扣除開頭重疊後的部分 | 落點的關鍵畫面（`keyframe`） | — |
  | 影片 | `follow` 參考，從該段扣除開頭重疊後的位置開始；檔案有音軌時帶音軌 | 落點的引導片段（`guide`；H3：最多 22 幀，維持在單一段內、避開開頭重疊） | — |
  | 音訊 | 參考，範圍 = 該段扣除開頭重疊後的部分 | — | 從落點開始保留原音（`lock`），長度到素材結尾或時間軸結尾 |

- **長條** —— 拖曳移動、拖曳端點修剪，`S`（或「分割」）在播放頭分割，`Delete` / `Backspace` 刪除，`Alt + ←/→` 微調 1 幀（`Shift` ×10），`Escape` 取消拖曳。長條與分段可用 `Tab` 取得焦點。長條在 8 px 內依序吸附段界、播放頭、其他長條的端點；會讓時間軸不合法的編輯（例如「接續前一段」把引導片段推過段界）會被拒絕並說明原因、不寫入。參考會在最接近播放頭的段界分割（不會切在段內）；關鍵畫面與 H3 引導片段不能分割（兩個 17k + 5 長度加起來不會是另一個合法長度），所以不顯示分割按鈕。數值在 inspector 編輯：參考的範圍、`follow` / `fixed`、內容起點 / 長度、音軌與順序（排序上移 / 下移 —— 順序可能影響標籤編號）；指定的位置、內容起點 / 長度與音軌。保留原音的 inspector 另外說明實際範圍、是否被截短，以及成品換回原聲的條件。
- **標籤按鈕** —— `✓ 名稱`：已是本段參考，點一下插入 `<@名稱>`。`＋ 名稱`：點一下同時建立本段參考並插入標籤，一個 undo 步驟；prompt 已有該標籤時只補參考。無法使用時停用，並在提示文字說明原因（同素材多條參考、該類已達上限、素材未讀取完成）。只想加參考不插標籤時，把卡片拖到軌道。
- **Prompt** —— 共用 prompt 給沒寫 prompt 的段使用；段落有自己的 prompt 就取代它、不會合併。表單持續顯示目前是哪一種，並可 `清空，改用共用 prompt`。需要插入標籤而本段空白時，會先把共用文字複製到本段並說明；只補共用 prompt 已有標籤的參考時，繼續使用共用 prompt。影片參考的配對音軌寫 `<@名稱:audio>`。每段把別名解析成自己的 `<Picture N>` / `<Video N>` / `<Audio N>` 標籤。直接手寫的編號標籤仍可用，但參考順序改變時意義會跟著變；對應明確時可按「轉成 <@名稱>」改寫成別名，新增參考前也會先做這個轉換，轉不了就拒絕並說明。
- **這段送給模型的內容** —— 收合時一行摘要（例如 `1 張圖、1 支影片、保留原音 3.96 秒`）。展開後每段在所有素材讀取完成時顯示：標籤對照表、每支參考影片實際使用的秒數與幀數（以及未使用多少）、關鍵畫面與引導片段的時間、保留原音的範圍、解析後的 prompt，以及 H3 技術細節（token 範圍以 `[起點, 終點)` 表示、覆蓋圖、接續邊界偏差）。錯誤與「保留原音和接續前段的聲音重疊」的警告不會隨收合隱藏。
- **Seed** —— 每段可選的 seed，附「換一個」；空白 = 使用 Sample Segments 的 `noise_seed`。只有 Sample Segments 會套用。
- **鏡頭切換 / 接續** —— 「從這段開始新鏡頭」把該段與前段的重疊設為 0、銜接改為直接切換，生成時不接續前段；本段與後續各段及屬於它們的參考與指定一起平移，跨過舊段界的項目會被標示。「接續前一段」恢復預設重疊（22 幀；段太短時取放得下的最大值）。各是一個 undo 步驟。

狀態列：

- 第一行是持續狀態。`✓ 時間軸檢查通過` 只代表時間軸本身（結構、分段、素材位置、每段的 prompt 與上限），不檢查下游接線或模型。有問題時顯示白話的對象與原因，點一下定位，`詳情` 內有原始位置與代碼。
- 少數問題附修復按鈕：長度或重疊不符（例如手改的 JSON）→ `調整段長與重疊`（依序只修不合法的值，後面的段會跟著移，交叉淡化放不下時縮短，不是整體最佳化）；generate 的 fps 不是 24 → `改成 24 fps`；cut 缺來源 → `上傳來源影片`；cut 帶素材 → 見上。一個 undo 步驟，並說明總長變化。
- 第二行是這次操作的通知：哪幾段送給模型的內容變了；參考換了段或編號標籤改指別的素材時會明確點名，因為這些變更全部合法、檢查仍會通過。狀態正常且本節點輸出還沒接線時，提示下一步（generate → 🎛️ Sample Segments；cut → 把 ranges 與 video_path 接到 ✂️ Trim by Ranges）。

一般：

- `timeline_data` JSON 是唯一會被存檔與送出執行的狀態；編輯器的檢視狀態（縮放、選取、播放頭、區塊開合）只存在 editor 裡，不寫進 workflow，所以選取或移動播放頭不算一個 undo 步驟，也不會進入 prompt。重新載入同一個 graph（undo / redo，或 graph id 相同的 workflow，例如同一個檔案再開一次）時會沿用檢視狀態；其他 graph 從預設檢視開始。素材讀取結果只存在編輯器裡。
- 打字後可以直接點按鈕或換欄位：文字先存，點擊照常生效；鍵盤操作後焦點留在同一個控制項。字母快捷鍵不會攔截 `Ctrl` / `Cmd` 組合鍵。
- 編輯器載入失敗時會改顯示原始 JSON widget，仍可直接編輯。
- 介面文字為繁體中文；技術名稱與儲存值保留英文，顯示標籤可中英並列。

編輯器使用兩條本機 route：`POST /mediaforge/timeline/probe`（影片、圖片與音訊）與 `POST /mediaforge/timeline/proxy`（只限影片）。只接受同源請求、只讀 ComfyUI `input/` 內的檔案、回應不含絕對路徑；上限為單檔 8 GiB、30 分鐘、每邊 8192 px、120 fps。Proxy 快取在 ComfyUI temp 目錄下的 `mediaforge_timeline_proxy`（4 GiB / 128 筆 / 24 h，LRU）。不連網。跟 core 自己的 route 一樣**沒有認證**、信任 `Host` header（不宣稱能防 DNS rebinding）—— LAN 不可信時請在網路層限制存取。

### 保留原音

放在「保留原音」軌道上的音訊卡片（`role = lock`，執行訊息與技術段落稱「鎖定原音」）讓時間軸上該位置保留原始音訊，作用在兩個地方：

- **取樣時**（在 Sample Segments 內由 Encode H3 Segment 進行；需要 `audio_vae`）：lock 觸及的每一段，完全落在 lock 內的音訊 token（每秒 40 個）會用你的 audio VAE 從原聲編碼 —— 使用前最多 4 秒、後最多 0.4 秒的有限上下文，是整段編碼的近似 —— 寫進音訊 latent 並設 `noise_mask = 0`。lock 邊緣不足一個 token 的部分照常生成。解碼前 Append 會確認鎖定 token 仍是寫入的值（latent 遺失 `mf_lock` metadata 或任一值偏差超過 1e-3 → `E_LOCK_DRIFT`）。這讓模型取樣的 latent 裡保有原聲；不宣稱對嘴。
- **最終成品** —— Assemble `lock_audio = original`（預設）組出 48 kHz 雙聲道 float 母帶：生成音軌在每個 lock 的區間 `[at, at + length)` 換回原聲，AAC 編碼前逐 sample 精確。`generated` 保留生成音軌。接上 `audio` 時整條取代並隱藏 `lock_audio`。沒有鎖音時此設定沒有作用；report 的 `effective` 欄位寫明實際套用與原因。
- **與續接的關係** —— lock 與某段從前段續接的音訊 token 重疊時，該段略過音訊續接（`W_LOCK_CARRY`，Sample Segments 的 report 也會記錄）；video 續接不受影響。該接縫的音訊連續性因此不保證。

### 增量重算

Sample Segments 以常數把每段規格傳進該段的節點組，ComfyUI 的快取只會重跑輸入有變的內部節點。與前段重疊為 0 的段（第一段或鏡頭切換）重新開始一條取樣鏈：改前面的鏡頭不會重新取樣它。以下在真 core executor、`--cache-classic` 驗證（CPU，H3 模型節點用替身）：

| 變更 | 重跑 |
|---|---|
| 無 | 無 —— Sample Segments 本身命中快取 |
| 只改 Assemble 設定 | 只有 Assemble |
| 第 k 段 prompt | 第 k 段的 encode、guider、extend 與 sampler；之後各段到下一個鏡頭切換前的 extend + sampler；第 k 段起到最後的 Append |
| 第 k 段 seed | 第 k 段的 noise、extend 與 sampler，之後各段到下一個鏡頭切換前的 extend + sampler；第 k 段起到最後的 Append；encode 不重跑 |
| sampler / sigmas | 每段的 sampler 與 Append；鏡頭切換處以外的 extend；encode 不重跑 |
| 尾端加一段 | 新段的整組節點，加上原末段的 Append |
| 執行中途被中斷 | 已完成的內部節點直接重用 |

其他快取模式（`--cache-lru N`、`--cache-none` 與預設的 RAM-pressure 快取）驗證的是變更後結果完整正確，但可能重跑更多內部節點，被逐出的結果會重新計算。重用也需要快取住的 state 的分段暫存檔仍然存在。

### 相容性

驗證環境：ComfyUI core 0.42.5、frontend 1.55.18（Nodes 2.0 與 classic）、RTX 5090 —— 這是測試環境，不是最低版本需求。

| 路徑 | 狀態 | 實跑紀錄 |
|---|---|---|
| 生成 —— MiniMax H3 AV 經 Sample Segments、24 fps、batch 1 | **支援** | 832×480 與 320×192、turbo 4-step。3 段各 107 幀、重疊 22 與 39 → 260 幀、32 kHz 共 346,667 個音訊 sample（最後補 267 個）。core 收到的是時間軸解析後的 `<Picture N>` / `<Video N>` / `<Audio N>` prompt、圖片順序與裁切後的參考幀；關鍵幀 / 錨定片段的幀位置相符。鎖定的音訊 token 取樣後與編碼的原聲相差 2.4e-7 以內；`lock_audio = original` 的母帶在鎖定區間與原始 PCM 逐 sample 相同；解碼後的生成音訊與原聲的包絡相關係數 0.997–1.000。832×480 從 2 段增加到 6 段：觀察到的 RSS 峰值 +0.15 GiB（一段 RGB 是 0.48 GiB）。只改一段的 seed 只重新取樣該段與下一段；加一段只取樣新的那段 |
| 剪片 —— Edit Timeline（`cut`）→ Trim by Ranges | 可用 | 不需要模型。來源幀率與時間軸相同時，保留的正好是各區間的來源幀；幀率不同的來源沿用自己的幀率（見[時間軸與長片](#時間軸與長片)） |

2026-10-30 以真的 H3 模型量測（RTX 5090、ComfyUI 0.42.5、H3 fl2va fp8 + turbo 4-step LoRA）。另外兩項結果來自 0.12 以前的迴圈接線，它在相同 seed、sampler 與 sigmas 下取樣出的 latent 與 Sample Segments 逐位元相同（最大差 0）：續接的 latent token 經過 sampler 後完全保留（最大差 ≤ 5e-7）；832×480 的接縫縮圖在 0–255 尺度上相差 MAE ≈ 1。此外，Encode H3 Segment 有針對 core `MiniMaxH3ReferenceToVideo` / `MiniMaxH3AddGuide` 類別的契約測試；Sample Segments 的展開與快取有在真 core executor 上的測試（H3 模型節點用替身：每一種失效情況、LRU / RAM-pressure 模式的實際逐出、編碼 / 取樣 / 附加時中斷）。

拒絕執行（直接報錯停止）：batch > 1、layout 與計畫不符的 latent、會改動續接區或鎖定區的 sampler、Sample Segments 用在 `generate`、24 fps 以外的計畫。

「支援」不代表會檢查 conditioning 衝突、保證角色一致或接縫無痕。

不支援 LTX-2 與 Wan 2.2；它們的實驗性續接已在 0.13 移除。見[從 0.12 升級](#從-012-升級)。

### 記憶體與磁碟

- Append 逐段解碼、去掉重疊、做 crossfade，把無損 FFV1 視訊與 float32 WAV 分段檔（1344×768 約每幀 0.5 MB）暫存到 plugin 的 `.mf_tmp/seg_*` 目錄。解碼前預估 RAM（`E_RAM`）、落盤前檢查剩餘磁碟（`E_DISK`）—— 都是估算，不是保證。
- Assemble 做唯一一次有損編碼（codec / crf / preset 同其他 MediaForge 編碼節點；ProRes → `.mov`），寫入 BT.709 標記；AAC 編碼前把音軌精確截成影片長度對應的 sample 數，編碼後核對寫出的幀數、音軌與色彩標記。是 Timeline 節點中唯一的 output node。選用的 `audio` 會整條取代音軌（自動補靜音 / 截斷到影片長度）。
- 音訊時鐘：第一段決定取樣率與聲道（之後不一致 → `E_AUDIO_LAYOUT`）。H3 解碼出的音訊每段約短 8.3 ms，由下一段補上 —— 只有真正的缺口與片尾會補靜音（`W_AUDIO_PAD`）。
- 快取住的 state 被釋放時，其分段暫存檔會一併刪除；啟動時會清掉超過 24 小時、且所屬程序已不存在的 session 目錄。等待 crossfade 的尾段也存在磁碟，不佔 RAM。
- 生成畫面只在 Append 內解碼、立即落盤。參考素材只在 Encode H3 Segment 內短暫解碼，不會成為快取的輸出。
- Sample Segments 每段留在 core 快取的主要是 conditioning —— 估計純文字 prompt ≈ 5 MB、每張參考圖約 +10 MB（`ref_image_size = match`）、每秒參考影片約 +20 MB；9 張圖加 3 支 15 秒影片 ≈ 1 GB —— 再加幾個 latent（1344×768 × 107 幀約各 12 MB）。20 段純文字 ≈ 1.2 GB；素材多時可達數 GB。以上是估算，不是 GPU 實測。
- 要壓低：用 `--cache-lru N` 啟動 ComfyUI、降低 `ref_video_short_edge`、維持 `ref_image_size = match`。不要指望預設的 RAM-pressure 快取釋放 —— 它的掃描可能看不進 H3 的巢狀 latent，會低估用量。
- 總記憶體仍受段長、素材、crossfade、模型工作集與 core 快取影響。這能減少解碼畫面佔用的記憶體，但不保證總記憶體固定或接縫無痕。
- 音訊一律逐 sample 精確切出。兩種情況不做 seek、改從檔頭解碼到切片位置：時間戳比一個 sample 粗的容器（Matroska / WebM 以毫秒記錄），以及需要重取樣的音訊（H3 參考與鎖音轉 32 kHz、其他取樣率轉 48 kHz 母帶）——從 seek 點重新起算的重取樣相位與從檔頭起算不同。代價在 720p MKV 約每分鐘位置 0.5 秒，純音訊檔更少；取樣率相同的 MP4、MOV、WAV、MPEG-TS 照常快速 seek。

### 從 Timeline Director 遷移

不提供 workflow / preset 自動轉換 —— 依概念重建：

| Timeline Director 概念 | MediaForge 重建方式 |
|---|---|
| Material Planner | Edit Timeline 素材庫：參考、關鍵幀 / 錨定片段、鎖定原音、`<@名稱>` 別名 |
| Select / Prepare Loop Segment | 由 Sample Segments 內部處理（每段一組節點） |
| Plan Encoder | Sample Segments（每一段都會執行 Encode H3 Segment） |
| Initialize / Accumulate / Finish Loop | Sample Segments + Assemble Segments；不需要迴圈節點 |
| Finite / Low-Memory Sampler | Sample Segments；Assemble 已經寫檔，不需要再接 SaveVideo |
| Drift-Control / Soft AV | 無等價實作；`hard` / `taper` / `linear` 遮罩不宣稱相同效果 |
| SelfLift / Omni / presets | 不提供也不匯入；沿用獨立 workflow、手動重建 |
| 原音鎖定 | 鎖定原音軌道（Encode H3 Segment 的 latent 鎖定）+ Assemble `lock_audio = original`；不宣稱對嘴 |

### 從 0.12 升級

0.13 只留一種生成接法（Edit Timeline → Sample Segments → Assemble Segments，MiniMax H3）與一種剪片接法（Edit Timeline → Trim by Ranges）。手動接線的路徑已移除，用到它們的 workflow 需要重接：

| 0.12 | 0.13 |
|---|---|
| `🎯 Select Segment`、`📽️ Load Segment Frames`、`🗂️ Load Segment Media` | 已移除。開啟舊 workflow 時 ComfyUI 會把它們列為缺少的節點 |
| 迴圈或攤平的接線（core Start / End Loop、每段自己接 sampler） | 改用 `🎛️ Sample Segments`。相同 seed、sampler 與 sigmas 下，它取樣出的 latent 與迴圈相同 |
| LTX-2 / Wan 2.2（`grid = causal_8n+1` / `causal_4n+1`、`experimental_causal`） | 不支援。要用這些模型請留在 0.12.x |
| Edit Timeline 的 `grid` | 只剩一個值 `h3_5+17n`。widget 已隱藏，舊 workflow 載入時會被設回這個值；API prompt 送其他值會被 ComfyUI 拒絕（`Value not in list`） |
| 不經模型的組裝（Load Segment Frames → Append 的 `frames` / `audio`） | `cut` 模式 → `✂️ Trim by Ranges`（`timeline_cut.json`）。它沿用來源影片的幀率，不會重取樣到時間軸幀率 |
| `cut` 模式的 prompt | 不再顯示，也不再讀取 |
| 節點選單裡的 `🧬 Extend Latent`、`🧩 Encode H3 Segment`、`➕ Append Segment` | 仍有註冊（Sample Segments 會建立它們），但已隱藏，見[內部節點](#內部節點)。它們的 `state`（Extend）、`frames` / `audio`（Append）與 `experimental_causal` 輸入已移除 |
| 範例 `timeline_h3_av_loop`、`timeline_ltx2_video_carry`、`timeline_wan22_experimental`、`timeline_frames_assembly` | `timeline_h3_basic`、`timeline_h3_director`、`timeline_cut` |

Edit Timeline 的 widget 順序與前五個輸出不變，所以照 `timeline_h3_director.json` 接的 0.12 workflow 可以直接載入；`video_path` 是新增的第六個輸出。

## Using via API

所有產出檔案的節點(BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeVideo / ExtractAudio / ConvertChinese / AssembleSegments) 都同時 emit ComfyUI `ui.images` metadata 跟 STRING path 兩種輸出。API 客戶端不必自己 parse 路徑就能拿到產出檔。

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

`type` 永遠是 `output`。每次跑 workflow 自動接 counter（`_00001.mp4` → `_00002.mp4` → ...）、不會 silently 覆蓋前次成品。💾 Save Video 則是每次執行寫一個新的 `<前綴>-<識別碼>/video.mp4` 資料夾。

### 字幕 / 文字輸出

`MF_ConvertChinese` 寫 `.srt` 或 `.txt`（heuristic：轉換後字串含 `-->` 視為 SRT）。`MF_WhisperTranscribe` / `MF_TranslateSubtitle` 回傳 SRT **字串內容**而非檔案 — 直接 wire 給 `MF_BurnSubtitle` 的 `srt_text` 輸入（節點內部落地暫存檔），要把 SRT 存成真檔案的話走 `MF_ConvertChinese`（填 `filename_prefix`）。

### 時間軸 prompt

`MF_EditTimeline` 的 `timeline_data` 是 JSON **字串** input；`video` / `fps` / `grid` / `mode` 是各自獨立的 input（`fps` 是 combo 字串，例如 `"24"` 或 `"30000/1001"`；`grid` 一律是 `"h3_5+17n"`；`generate` 不選來源時 `video` 填 `"(none)"`）。`plan_json` 是衍生輸出 —— 永遠不是 input，也不能貼回 `timeline_data`。

| 欄位 | 規則 |
|---|---|
| `version` | 整數 `2`（`1` 仍接受並自動升級；v1 沒有 `prompt`、素材與 seed） |
| `source.origin_frame` | 來源時間原點，預設 `0` |
| `prompt` | 全域 prompt，預設空；段落自己的 `prompt` 空白時沿用 |
| `segments` | 1–256 段 |
| `segments[].start` / `end` | 整數幀，`end` 不包含；`generate`：段長 5 + 17n、重疊 0 或 5 + 17j；`cut`：落在來源範圍內、不重疊 |
| `segments[].prompt` | 字串，預設空 |
| `segments[].seed` | 選填，`null` 或 0 … 2⁵³ − 1；只有 Sample Segments 會套用 |
| `segments[].seam_in.kind` | `cut` / `crossfade`；`crossfade` 需合法的 `frames` |
| `segments[].seam_in.audio_curve` | `auto` / `linear` / `equal_power` |
| `materials` | 最多 64 個 `{id, kind, ref, name}`：`kind` = `image` / `video` / `audio`；`ref` = `input/` 內的檔案；`name` = 別名，1–32 個 ASCII 英數 / `_` / `-` / 中日韓文字 / 假名 / 韓文，NFC、不重複 |
| `refs` | 最多 256 個；陣列順序 = 標籤順序。圖片 `{id, material, from, to}`；影片再加 `mode`（`follow` 預設，或 `fixed`）、`in`、`audio`，`fixed` 另加 `length`；音訊再加 `in`、`length` |
| `anchors` | 最多 512 個。圖片：`{id, material, role: "keyframe", at}`；影片：`{…, role: "guide", at, in, length, audio}`；音訊：`{…, role: "lock", at, in, length}` |

`id` 是 1–32 個 `A–Z a–z 0–9 _ -` 字元，在各自的陣列內不重複。所有位置都是時間軸幀；`in` / `length` 以時間軸幀、從素材起點計算。參考屬於所有與它的範圍重疊的段（以各段輸出區間判定）；錨定片段必須完整落在單一段的輸出區間內（H3：長度 5 + 17k）；`cut` 模式不接受素材。`<@名稱>` / `<@名稱:audio>` 別名與 `<Picture N>` / `<Video N>` / `<Audio N>` 標籤只用於 `generate` 模式。

未知欄位、重複鍵、拿布林值當整數、非有限數值一律拒絕（`E_TIMELINE_JSON`）。

最小的 v2 時間軸 —— 一張參考圖用於兩段、第 12–72 幀鎖定原音、第 2 段固定 seed 並沿用全域 prompt（在 `timeline_data` 以 JSON 字串送出）：

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

搭配 `fps = "24"`、`grid = "h3_5+17n"`、`mode = "generate"`，兩段的 prompt 都解析成 `<Picture 1> …`。Sample Segments 的 API 格式中 `noise_seed` 是單純整數（沒有 `control_after_generate`）。

完整的剪片 prompt（不需要模型）：

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

`clip.mp4` 是 `input/` 下的檔案。Edit Timeline 的 `ranges` 是 output 1、`video_path` 是 output 5。Trim by Ranges 不是 output node，所以 prompt 後面要再接一顆 —— 這裡用 core `PreviewAny`。

要改成生成：把 `"mode"` 設為 `"generate"`，並把節點 2–3 換成 `MF_SampleSegments`（`"segment_plan": ["1", 0]`、連到你的 model / clip / vae / sampler / sigmas 節點、`noise_seed`、`width`、`height`、`mask`、`carry_audio`）與 `MF_AssembleSegments`（`"state": [<sample_segments_id>, 0]`、`filename_prefix`、`codec`、`crf`、`preset`）。Assemble 與其他寫檔節點一樣會送出 `ui.images`，所以 `/history` 會列出它的輸出。

## 智慧 GPU codec 預設

所有 encode 能力節點(BurnSubtitle / LoopVideo / TrimByRanges / ConcatVideos / SaveVideoFrames / ComposeVideo / AssembleSegments) 的 `codec` dropdown 預設值在啟動時動態挑:偵測到 ffmpeg 支援 NVENC 就用 **`h264 NVIDIA GPU (h264_nvenc)`**,沒有就 fallback **`h264 (libx264)`** — CPU-only 機器不會壞、不必每次手動切。

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
| 長影片分批通過任何 IMAGE 節點 | ✅ Meta Batch Manager | ✅ Video Batch | [比較與實測數字](#video-batch批次處理長影片) |
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

## 節點清單（36）

每個節點區段都遵循同一個模板：**用途 → 適用情境 → 必填欄位 → 選用輸入 → 輸出 → 範例**。Widget 表掃過去就能找到要調的旋鈕，範例則給典型接線方式。

> **Dual-input 註記**：下列標 **(dual-input)** 的 node 同時接受兩種 input：(a) 既有的 `video_path` STRING 欄位，(b) `frames` + `tensor_fps` + `audio` 三件套 optional 輸入。連 tensor 時 MediaForge 內部會寫一個 temp `.mp4` 給 FFmpeg 吃。
>
> 前端 extension `web/dual_input_lock.js` 把 widget 可見性跟 wiring 狀態連動：接 `frames` 時 path widget 自動收起（tensor 模式）、`tensor_fps` 只在 tensor 模式才顯示。Path-mode-only 的 widget（如 `keep_source_audio`）在 tensor 模式自動隱藏。
>
> 有些 dual-input node 是靠 **AUDIO** pin 觸發、不是 `frames` — `MF_ComposeAudioMix` / `MF_DetectSilence` / `MF_ExtractAudio` 在 `audio` pin 接上時會隱藏各自的 `audio_path` / `audio_source` STRING widget。機制相同（`web/dual_input_lock.js` 的 `DUAL_INPUT_NODES`），只是觸發的 socket 不同。
>
> **輸出檔名 pattern**：所有產出檔的 node 都用 `filename_prefix` + auto-counter（跟 ComfyUI 核心 `SaveImage` 同 pattern）。每次跑 workflow 寫 `output/<prefix>_00001.mp4` → `_00002.mp4` → ...，重跑不會 silently 覆蓋舊輸出。`filename_prefix` 可含子目錄（例 `MediaForge/subtitled`）。例外：💾 Save Video 每次執行一個資料夾，`output/<前綴>-<識別碼>/video.mp4`。

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

#### 💾 Save Video · Serial H.264 (`MF_SaveVideo`)

ComfyUI `VIDEO` → H.264 MP4，有音訊時編成 AAC。讀檔時的解碼器、RGB→YUV 轉換（swscale）、libx264 與 AAC 編碼器各只用一條執行緒。ComfyUI V3 輸出節點：完成後顯示影片預覽，原輸入物件照樣往下傳。

**適用情境**：存任何 `VIDEO`（core Load Video、Create Video 或其他輸出 `VIDEO` 的節點），要的是單執行緒、可重現的匯出而不是編碼速度——例如機器正在跑取樣，或存檔出問題時要先排除多執行緒。

| Widget | 預設 | 說明 |
|---|---|---|
| `video` | (必填 VIDEO) | core 的 `VideoFromFile`（Load Video）沿用 core 的時間截取／裁切／旋轉／音軌選擇規則，解碼器設成單執行緒。已解碼的影片（`VideoFromComponents`，例如 Create Video）直接取用。其他 `VIDEO` 型別由來源自己提供元件，報告標示其解碼執行緒未知。 |
| `filename_prefix` | `MediaForge/serial_video` | 相對於 `output/`，可含子資料夾。絕對路徑、`..`、經由符號連結或 junction 指到 `output/` 外的路徑，在寫入任何東西前就拒絕。每次執行寫 `output/<前綴>-<32 位十六進位識別碼>/video.mp4`。 |

**輸出**：`video`（輸入物件本身）、`video_path`（完成的 MP4 絕對路徑）、`report_json`。節點以完成的 MP4 當預覽。

**編碼**：libx264 CRF 23、preset `medium`（x264 預設值）；8-bit 來源用 `yuv420p`，10-bit 用 `yuv420p10le`（10-bit 路徑保留 16-bit RGB 直到轉 YUV）。色彩沿用 ComfyUI 的標記：`sRGB` → BT.709／sRGB 轉換曲線，`HDR` → BT.2020／HLG，`HDR PQ` → BT.2020／PQ；來源沒標的當 `sRGB` 存。fps 跟 core 一樣取到小數第三位（`24000/1001` → `23.976`），不補幀也不丟幀。音訊保留取樣率與聲道配置（1 = mono、2 = stereo、6 = 5.1），超過影片的部分依 `ceil(取樣率 × 幀數 / fps)` 裁掉。MP4 使用 `faststart`。

**直接報錯、不寫檔**：alpha（H.264 沒有透明度；core 會給 GIF 一個 alpha 通道，所以 Load Video 讀進來的 GIF 要先用 Get Video Components → Create Video 不接 alpha 重建）、不是 8 或 10-bit 的來源、奇數寬高、畫面或音訊含 NaN／±Infinity、其他聲道數或 AAC 不支援的取樣率，以及 PyAV 17 上的 HDR（它的色彩轉換沒有 BT.2020 矩陣，需要 `av` 18 以上）。

**寫檔**：先寫 `video.partial.mp4`，編碼器 flush、容器關閉、`fsync` 之後，在同一資料夾改名成 `video.mp4`。取消（解碼前、每一幀、改名前都會檢查）或失敗時一定不會留下 `video.mp4`，也不回傳路徑或預覽；已經寫入資料的暫存檔保留供診斷。同一前綴同時跑多次，各自有自己的資料夾。

**中繼資料**：遵守 `--disable-metadata`。啟用時，`extra_pnginfo` 的每個頂層欄位（例如 `workflow`）和執行中的 `prompt` 各寫成一個嚴格 JSON 的 MP4 標籤。`NaN`／`±Infinity`——例如 🎬 End Video Batch 這類節點留在執行中 prompt 的 `is_changed: [NaN]` 快取標記——寫成 `null`；prompt 和 workflow 本身不會被改，Unicode 原樣寫入。

**`report_json`**：`status`、`path`、`subfolder`、`width`、`height`、`frames`、`fps`／`fps_fraction`、`source_fps`／`source_fps_fraction`、`bit_depth`、`pix_fmt`、`color_space`、`video`（編碼設定，以及從編碼器和 x264 自己的設定字串讀回的執行緒數）、`audio`（沒有音訊時為 `null`）、`color_conversion`、`source`（來源型別與解碼方式）、`metadata`，以及 `checks`——只列實際跑過的檢查。

**說明**：讀檔時整段影片會解碼進記憶體，跟 core 的 Get Video Components 一樣。core 把解碼後的 YUV 轉成 RGB 時用 PyAV 的自動執行緒數，這一步不在本節點控制內，報告會照實標示。PyAV 用一個小的 libavfilter 圖把音訊切成 AAC 的幀，它的執行緒池無法設定，但一直閒置。

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

#### ⏱️ Edit Timeline (`MF_EditTimeline`)

在同一條時間軸上把長片規劃成分段，輸出其他 Timeline 節點共用的計畫。可在內嵌的[時間軸編輯器](#時間軸編輯器)編輯，也可直接送 `timeline_data` JSON（[時間軸 prompt](#時間軸-prompt)）；每次執行都會重新嚴格驗證 JSON。

**適用情境**：所有 Timeline workflow 的第一個節點 —— `generate` 規劃要由模型生成的分段；`cut` 選取來源區間、不續接 latent。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `video` | dropdown | `(none)` | 顯示為「來源影片（video）」。`input/` 下的來源影片。`generate` 可不選、`cut` 必選；`(none)` 依模式顯示為「不使用來源影片」或「尚未選擇（必選）」 |
| `fps` | dropdown | `24` | 顯示為「每秒幀數（fps）」。時間軸幀率（有理數）：`16` / `24` / `25` / `30` / `24000/1001` / `30000/1001` / `48` / `50` / `60000/1001` / `60`。H3 必須 `24` |
| `grid` | dropdown | `h3_5+17n` | 顯示為「段長規則（grid）」，選項附模型名稱（例如 `MiniMax H3 段長規則（h3_5+17n）`）。決定合法段長與重疊（見下表）；不選擇也不載入模型。`mode = cut` 時隱藏（cut 不使用） |
| `mode` | dropdown | `generate` | 顯示為「用途（mode）」。`generate`（生成新影片）規劃生成段；`cut`（剪現有影片）選取來源區間，不續接 latent |
| `timeline_data` | STRING (multiline) | `{"version": 1, "source": {"origin_frame": 0}, "segments": [{"start": 0, "end": 107, "prompt": ""}]}` | 以 JSON 字串表示的計畫（含素材的 v2 格式見[時間軸 prompt](#時間軸-prompt)；編輯器存檔為 v2）。視覺化編輯器正常運作時隱藏；編輯器載入失敗時顯示且可編輯 |

| `grid` | 合法段長（幀） | 合法重疊（幀） | 續接 |
|---|---|---|---|
| `h3_5+17n` | 5 + 17n | 0 或 5 + 17j | video（開 `carry_audio` 時含 audio）；fps 必須 `24` |
| `causal_4n+1` | 1 + 4n | 0 或 1 + 4j | video；需開 `experimental_causal` |
| `causal_8n+1` | 1 + 8n | 0 或 1 + 8j | video；需開 `experimental_causal` |
| `free` | 任意 ≥ 1 | 任意 | 無 —— 重疊只做裁切 |

`generate`：首段從 0 開始；start 與 end 都嚴格遞增、段與段之間不留白，所以每個重疊都比相鄰兩段短；`crossfade` 接縫的幀數須在 1 … min(重疊, 前段輸出長度)。`cut`：必須選 `video`；各段依序排列、互不重疊（可相接）、不超出來源範圍，接縫只能 `cut`。

**輸出**：

| 輸出 | 型別 | 說明 |
|---|---|---|
| `segment_plan` | MF_SEGMENT_PLAN | 接 Select Segment |
| `ranges` | SILENCE_RANGES | `cut`：選取的來源區間（秒，與 `MF_DetectScenes` 同慣例，可接 `MF_TrimByRanges`）；`generate`：空 list |
| `plan_json` | STRING | 驗證後計畫的 JSON，供檢視 —— 永遠不是 input |
| `segment_count` | INT | 接 Start Loop 的 `max_iteration` |
| `total_frames` | INT | 輸出總幀數 |

**說明**：`IS_CHANGED` 以選定 `video` 與所有素材檔的指紋判斷，換檔會重新規劃（`timeline_data` 或 `video` 是連線輸入時，Edit Timeline 每次都重跑）。每次執行都會讀取素材（`E_MATERIAL`）、以素材檢查參考與錨定（`E_REF`、`E_ANCHOR`、`E_ANCHOR_OVERLAP`），H3 grid 再套用每段上限與標籤（`E_REF_LIMIT`、`E_REF_SHORT`）並解析 prompt（`E_PROMPT_REF`）。讀不到來源起始時間時印出 `W_SOURCE_TIME_UNKNOWN`。

**範例**：H3 —— `fps = 24`、`grid = h3_5+17n`，分段 `[0,107)`、`[85,192)`、`[153,260)`（重疊 22 與 39，都是 5 + 17j）→ `segment_count = 3`、`total_frames = 260`。

---

#### 🎯 Select Segment (`MF_SelectSegment`)

從計畫取出一段（迴圈路徑）。輸出的 `segment` 是 Load Segment Frames、Load Segment Media、Encode H3 Segment、Extend Latent、Append Segment 共用的規格 —— fps、重疊、續接大小與本段素材只在 Edit Timeline 算一次。

**適用情境**：迴圈每輪一次，或線性串接時每段一次。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `segment_index` | INT | `0` (0–255, step 1) | 0 起算；迴圈中接 Start Loop 的 `iteration_index`。超出範圍 → `E_SEGMENT_INDEX` |

**輸入**：`segment_plan`（MF_SEGMENT_PLAN），來自 Edit Timeline。

**輸出**：`segment`（MF_SEGMENT）、`prompt` STRING（本段 prompt —— 本段空白時為全域 prompt，別名已解析成標籤；接你的文字編碼節點）、`length` INT（幀數，接你的空 latent 節點）、`start_sec` FLOAT（時間軸秒數）、`segment_count` INT。逐段 seed 不在這裡輸出 —— 只有 Sample Segments 會套用。

---

#### 📽️ Load Segment Frames (`MF_LoadSegmentFrames`)

解碼計畫來源影片落在某一段內的畫面與音訊，並重取樣到時間軸 fps（來源 fps 相同時逐幀精確，不同時取最近幀）。

**適用情境**：給模型的參考素材（例如 video-to-video conditioning），或不經模型直接組裝時的畫面本身（`timeline_frames_assembly.json`）。需要在 Edit Timeline 選了 `video`。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `resize` | dropdown | `none` | `none` 原尺寸；`fit` 等比縮放留邊；`fill` 等比縮放裁切；`stretch` 拉伸 |
| `width` | INT | `1344` (2–7680, step 2) | `resize = none` 時隱藏 |
| `height` | INT | `768` (2–4320, step 2) | `resize = none` 時隱藏 |
| `shortfall` | dropdown | `error` | 來源不足以涵蓋本段時：`error`（`E_SOURCE_SHORTFALL`）、`hold_last`（延用最近一幀 —— 完全沒有畫面時不能延用）、`black`（補黑） |

**輸入**：`segment`（MF_SEGMENT）。

**輸出**：`frames` IMAGE、`audio` AUDIO（來源沒有音軌時為 `None`；段落超出來源的部分補靜音）、`frame_count` INT。

**說明**：每次執行都重新解析來源路徑並比對指紋；Edit Timeline 執行後來源被換掉 → `E_SOURCE_CHANGED`。讀取第一條不是封面圖的影像串流。

---

#### 🗂️ Load Segment Media (`MF_LoadSegmentMedia`)

在迴圈路徑中，把某段的一個素材 —— 某一類的第 N 個，順序同標籤 —— 取成 IMAGE / AUDIO，讓你自己接線。

**適用情境**：H3 以外的模型（例如給 Wan / LTX conditioning 節點的參考圖或錨定畫面）。H3 請用 `🧩 Encode H3 Segment`，它一次把本段所有素材編進 conditioning。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `kind` | dropdown | `picture` | `picture` / `video` / `audio`（參考）、`keyframe` / `guide` / `lock`（錨定） |
| `ordinal` | INT | `1` (1–512, step 1) | 本段該類的第幾個，1 起算。超出範圍 → `E_MEDIA_MISSING`（不會補黑圖或靜音假裝存在） |
| `resize` | dropdown | `none` | `none` / `fit` / `fill` / `stretch`，同 Load Segment Frames。`kind = audio` 或 `lock` 時隱藏 |
| `width` | INT | `1344` (2–7680, step 2) | `resize = none` 或 `kind = audio` / `lock` 時隱藏 |
| `height` | INT | `768` (2–4320, step 2) | 同上 |

**輸入**：`segment`（MF_SEGMENT），來自 Select Segment。

**輸出** —— `count` INT 一律是本段該 `kind` 的項目數；音訊一律雙聲道（單聲道複製、多聲道下混）、素材原取樣率：

| `kind` | `image` IMAGE | `audio` AUDIO | `frame_idx` INT |
|---|---|---|---|
| `picture` | `[1,H,W,3]`（套用 EXIF 方向與 `resize`） | None | 0（參考沒有位置） |
| `video` | 有效幀的切片 `[F,H,W,3]`（時間軸 fps） | 參考設 `audio = true` 時為配對音軌，否則 None | 0 |
| `audio` | None | 參考音訊 `[in, in + length)` | 0 |
| `keyframe` | `[1,H,W,3]` | None | 段內幀 |
| `guide` | 錨定片段 `[F,H,W,3]` | `audio = true` 時為其音軌，否則 None | 段內幀 |
| `lock` | None | 本段範圍內的原聲 | 段內幀 |

**說明**：None 輸出接到不接受 None 的節點會在該節點報錯。`kind = lock` 只交出音訊 —— 不會在 latent 裡鎖定任何東西。素材檔會與 Edit Timeline 的快照比對指紋（`E_SOURCE_CHANGED`）。

---

#### ➕ Append Segment (`MF_AppendSegment`)

接收完成的一段，用你的 VAE 解碼（或直接吃 `frames`），去掉與前段的重疊、套用 incoming crossfade，並把無損分段檔暫存到磁碟。回傳新的 state；輸入的 state 不會被修改。

**適用情境**：接在 sampler 之後，每段一次。

沒有 widget —— 只有 socket：

| 輸入 | 型別 | 說明 |
|---|---|---|
| `segment` | MF_SEGMENT | 必填；來自 Select Segment（Sample Segments 傳入的是精簡 view） |
| `state` *(選用)* | MF_SEGMENT_STATE | 前一段的 state；第一段留空（迴圈中接 Start Loop 的 `current_iteration_value`） |
| `latent` *(選用)* | LATENT | 本段取樣結果；也是下一段續接的來源 |
| `vae` *(選用)* | VAE | 解碼 latent 的畫面；沒接 `frames` 時必接 |
| `audio_vae` *(選用)* | VAE | 解碼同一個 AV latent 的音訊 stream |
| `frames` *(選用)* | IMAGE | 直接提供本段畫面，不經 VAE |
| `audio` *(選用)* | AUDIO | 本段音訊 |

**規則**：`frames` 與 `vae` / `audio_vae` 互斥（接 `frames` 時 `latent` 可保留作續接來源）；沒接 `frames` 時 `latent` 與 `vae` 都必接；`audio` 與 `audio_vae` 擇一；下一段要從本段續接時必須接 `latent`。

**輸出**：`state`（MF_SEGMENT_STATE）→ End Loop 的 `next_iteration_value` + `output_value`，或線性串接時下一段的 Extend / Append。

**檢查**：分段順序與目前已組裝內容的身分（`E_SEQUENCE`、`E_PLAN`）；latent 上的續接標記（缺少 → `W_PROVENANCE`，不符 → `E_PROVENANCE`）；續接區是否被 sampler 改變（`E_CARRY_DRIFT`）；鎖定的音訊 token 是否未變、`mf_lock` metadata 是否還在（`E_LOCK_DRIFT`）；RAM / 磁碟預估（`E_RAM`、`E_DISK`）；各段解析度與音訊格式一致（`E_AUDIO_LAYOUT`）。`audio_curve = auto` 的 crossfade：音訊由 `audio_vae` 從同一個續接的 H3 latent 解碼時用 `linear`，其他情況用 `equal_power`。

---

#### 🧵 Assemble Segments (`MF_AssembleSegments`)

把暫存的分段檔編碼成最終影片 —— Timeline 流程中唯一一次有損編碼 —— 也是流程的 output node。

**適用情境**：接在 End Loop 之後（`outputs` 接 `state`），或線性串接的最後一個 Append 之後。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `filename_prefix` | STRING | `MediaForge/timeline` | → `output/<prefix>_NNNNN.mp4`（ProRes 為 `.mov`） |
| `codec` | dropdown | smart（有 NVENC 用、沒有用 libx264） | 跟其他編碼節點共用 catalog |
| `crf` | INT | `18` (0–51) | `prores (prores_ks)` 時隱藏。刻度意義隨 codec 不同，見[智慧 GPU codec 預設](#智慧-gpu-codec-預設) |
| `preset` | dropdown | `medium` | `ultrafast` … `veryslow`。`prores (prores_ks)` 時隱藏 |
| `lock_audio` *(選用)* | dropdown | `original` | `original`：[鎖定原音](#保留原音)的區間換回原聲（48 kHz 雙聲道母帶）；`generated`：保留生成音軌。沒有鎖音時沒有作用。接上 `audio` 時隱藏 |

**輸入**：`state`（MF_SEGMENT_STATE，必填）—— 來自 End Loop、最後一個 Append 或 Sample Segments；`audio`（AUDIO，選用）—— 整條取代最終音軌，自動補靜音 / 截斷到影片長度。

**輸出**：`final_video_path` STRING；`report` STRING（JSON：幀數、fps、尺寸、色彩假設、音訊來源（`internal` / `override` / `locked_original` / `none`）/ 取樣率 / samples、補靜音數、各接縫診斷、`effective` 鎖音欄位）。emit `ui.images` 給 `/history`。

**說明**：假設 BT.709（full-range RGB → limited-range YUV）並寫入對應標記；核對幀數，有內部音軌時也核對精確 sample 數；分段檔被刪除（例如清掉 `.mf_tmp/`，或被快取逐出）→ `E_SPILL_MISSING`，請重跑迴圈。這裡不刪分段檔，因為快取住的 state 可能還會被重用。

---

#### 🎞️ Start Video Batch (`MF_StartVideoBatch`)

把影片一批一批送進你的節點。和 `🎬 End Video Batch` 配對使用，說明見 [Video Batch](#video-batch批次處理長影片)。

**何時使用**：影片太長、放不進記憶體，又要通過 IMAGE 節點時。內建檔案選擇，流程維持兩個節點；這是「用 `📂 Select Video` 選檔」慣例的唯一例外。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `video` | dropdown + 上傳 | | `input/` 內的影片。接了 `video_path` 時連同上傳鈕一起隱藏 |
| `resize` | dropdown | `none` | `none` / `fit`（留邊）/ `fill`（裁切）/ `stretch` |
| `width`、`height` | INT | `1280`、`720` | `resize = none` 時隱藏 |
| `video_path` *（選用）* | STRING socket | | 優先於 `video`。不能依賴批次內的節點 |

**輸出**：`frames` IMAGE — 本批，永遠是 `frames_per_batch` 幀；`audio` AUDIO — 本批音訊，只有接線才解碼；`fps` FLOAT；`frame_count` INT — 本批幀數，含重複的幀，不是全片長度。

**備註**：支援 mp4 / mov / mkv / webm / avi。MPEG-TS 與 MPEG 沒有關鍵幀索引可以定位，HDR 與廣色域影片尚未處理，都會被拒絕並提示轉檔指令。fps 取自來源，除非 End 有設定；可變幀率會重取樣成該 fps。來源檔案在工作途中改變時會停止，不會混到兩支影片。

---

#### 🎬 End Video Batch (`MF_EndVideoBatch`)

負責整個工作：要每一批、寫到磁碟、最後編成影片。是輸出節點。

**何時使用**：和 `🎞️ Start Video Batch` 配對。工作的設定都在這裡。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `frames_per_batch` | INT | `64` | 一次送進節點的幀數，越小越省 RAM 與 VRAM。H3 需符合 17n+5，例如 124 |
| `fps` | FLOAT | `0` | 轉成這個固定 fps；`0` 使用來源的 fps。多支影片 fps 不同時，`0` 會自動選一個（見「兩支以上的影片」） |
| `max_frames` | INT | `0` | 只處理前幾幀，試跑用；`0` 全部 |
| `codec` | dropdown | 智慧（有 NVENC 用 NVENC，否則 libx264） | 與其他編碼節點相同 |
| `keep_source_audio` | BOOLEAN | `true` | 保留來源音軌。接了 `audio` 時隱藏 |
| `filename_prefix` *（進階）* | STRING | `MediaForge/video_batch` | → `output/<prefix>_NNNNN.mp4`（ProRes 為 `.mov`） |
| `overlap_frames` *（進階）* | INT | `0` | 相鄰兩批重疊並淡接的幀數，最多 `frames_per_batch` 的一半 |
| `crf` *（進階）* | INT | `18`（0–51） | `prores (prores_ks)` 時隱藏 |
| `preset` *（進階）* | dropdown | `medium` | `prores (prores_ks)` 時隱藏 |

**輸入**：`frames`（IMAGE，必接）— 處理後的畫面；`audio`（AUDIO，選用）— 見[音訊表](#video-batch批次處理長影片)。這裡的 widget 都只能在節點上設定、沒有接點，因為 Start 要在 End 執行前就讀到它們。

**輸出**：`final_video_path` STRING；`report` STRING（JSON：幀數、fps、尺寸、批數、來源、音訊來源 `internal` / `override` / `none`、取樣率與樣本數、每個接縫的診斷）。

**備註**：End 之後的節點可以讀 `final_video_path`，但不能讀批次內的畫面，因為只會拿到其中一批。錯誤說明見 [Video Batch 訊息](#video-batch-訊息)。

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

### `MediaForge/AI` — Latent 工具與 provider-agnostic AI 服務

Schema 標記為 **experimental** — `AI_CONFIG` API 在 Phase 5 內可能改，直到 Whisper / Translate 在所有 provider recipe 都 e2e 驗證完才會凍結。`🧬 Extend Latent`、`🧩 Encode H3 Segment`、`🎛️ Sample Segments`（本分類最後三個）是 Timeline 節點 —— 不吃 `AI_CONFIG`，也不發任何網路請求。

#### ⚙️ AI Config (`MF_AIConfig`)

集中管理 provider 設定。輸出 `AI_CONFIG` dict、所有 AI node 吃這個 — 一處切換 provider / model / endpoint，整條 chain 跟著換。

**適用情境**：所有 AI workflow。每個 backend 配一個 `MF_AIConfig`（例如一個給 Groq ASR、一個給 OpenAI translate）、扇出接給各 consumer。

**節點介面**：最上面的 `服務` 和 `模型` 下拉會把值填進下表的欄位，不用自己查網址和模型名稱，用不到的欄位也會隱藏。服務選 `自訂` 會顯示全部欄位，模型選 `自訂…` 可以手打。這兩個下拉只存在前端、不進 workflow JSON，舊 workflow 和 API 呼叫一樣只用下表五個欄位。切換服務時 `api_key` 會換成新服務的預設值，明文 key 不會帶到別的服務。模型清單內建在外掛裡、不連網；Ollama / LM Studio 要手打已下載的模型名稱。

| Widget | 預設 | 說明 |
|---|---|---|
| `provider` | `openai_compatible` | `openai_compatible`（任意 `/v1/...` HTTP endpoint）/ `faster_whisper_local`（in-process） |
| `base_url` | `https://api.openai.com/v1` | 尾端 `/` 會自動 strip |
| `api_key` | `""` | **建議填 `env:OPENAI_API_KEY`** — `env:` 前綴會在執行時解析同名環境變數，secret 不會進 workflow JSON（明文 key 會原樣序列化進每一份匯出／分享的 workflow — 畫面遮罩擋不了這條路；變數不存在會給明確錯誤）。防外送保護：`env:` 解析出的 key 只允許送往信任 host（內建：`api.openai.com` / `api.groq.com` / localhost 家族；`openrouter.ai` 只接受 `OPENROUTER_API_KEY`，`generativelanguage.googleapis.com` 只接受 `GEMINI_API_KEY`）— 可用環境變數 `MF_AI_KEY_ALLOWED_HOSTS`（全域，逗號分隔 hostname）或 `MF_AI_KEY_ALLOWED_HOSTS_<變數名>`（綁單一變數）擴充，讓惡意分享的 workflow 無法用 `env:任意秘密` 配攻擊者 endpoint 外送。`env:` 解析的 key 對非 loopback host 一律要求 **HTTPS**（明文 HTTP 會暴露 Bearer header）— 只有 `localhost` / `127.0.0.1` / `::1` 可走 `http://`；內網 IP 視同遠端，即使加進 allowlist 也需要 HTTPS（或改用明文 key，兩道防護都不適用）。log 只露前 4 字 + `***`；節點畫面上明文 key 以 `•` 遮罩顯示（`env:` 引用直接顯示），點擊可編輯／顯示真值（見 `web/ai_config_mask.js`） |
| `model` | `gpt-6-luna` | 字串，可填清單外的任何模型。Whisper 認出來不像 STT id 時會自動換成該服務的語音模型（見 Whisper Transcribe 的 Model 自動替換） |
| `device` | `auto` | `cpu` / `cuda` / `auto` — 只 `faster_whisper_local` 用 |

**輸出**：`ai_config`（AI_CONFIG dict）。

**詳見 [AI Provider Recipes](#ai-provider-recipes)** — 有 OpenAI / Groq / OpenRouter / Gemini / Ollama / faster-whisper 可直接複貼的 `provider` / `base_url` / `model` 組合。

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
- `openai_compatible` — POST 到 `<base_url>/audio/transcriptions`，要求 `verbose_json` 並由節點組成 SRT，所以端點要能回傳有時間軸的 `segments`（OpenAI `whisper-1`、Groq 的文件都有列；沒有就報錯，不會回傳空字幕）。使用 `requirements.txt` 裡的 `requests`。
- `faster_whisper_local` — lazy-import `faster-whisper`。吃 `ai_config.device`（`cpu` / `cuda` / `auto`）跟 `ai_config.model`（`tiny` / `base` / `small` / `medium` / `large-v3`）。第一次跑會下載到 HF cache。需要 `pip install faster-whisper`。

**Model 自動替換**：`ai_config.model` 看起來不像 STT model id（例如同一個 `AI_CONFIG` 還要餵 Translate、所以是 `gpt-6-luna`），Whisper 會自動換成該服務的語音模型 — Groq 用 `whisper-large-v3-turbo`、其他 OpenAI-compatible 端點用 `whisper-1`、faster-whisper-local 用 `base`，實際模型會印在 log。要明確指定 STT 模型（`whisper-large-v3` / `distil-large-v3` 等）直接填到 `ai_config.model`。OpenRouter 和 Gemini 目前不支援語音辨識，接 Whisper 會直接報錯。

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

**推薦模型**：`gpt-6-luna`（OpenAI 目前最便宜的文字模型）；長段或專業詞彙可改用 `gpt-6.1-terra` / `llama-3.3-70b-versatile`。

---

#### 🧬 Extend Latent (`MF_ExtendLatent`)

把前一段 latent 的尾端複製到本段空 latent 的開頭，並設定 `noise_mask`，讓 sampler 保留（或部分重新生成）這一區。只做 tensor 運算 —— 不載入模型、不取樣。

**適用情境**：每一段都接在你的空 latent 節點與 sampler 之間。第一段（沒有與前段重疊）直接通過、不做續接。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `mask` | dropdown | `hard` | `hard` 保留全部續接 token；`taper` 放寬續接區尾端；`linear` 逐步放寬（最後一個續接 token 可完全去噪；只續接 1 個 token 時例外保留） |
| `carry_audio` | BOOLEAN | `False` | 僅 H3 音訊續接（其他 latent → `E_AUDIO_CARRY`）；關閉時不改音訊 latent／遮罩 |
| `video_taper_tokens` *(選用)* | INT | `2` (0–1000000, step 1) | `taper` 時 video 續接區尾端放寬的 token 數，不得超過續接 token 數。`mask = hard` 或 `linear` 時隱藏 |
| `audio_taper_tokens` *(選用)* | INT | `2` (0–1000000, step 1) | audio 版本，規則同上。`mask = hard` 或 `linear` 時隱藏，`carry_audio` 關閉時也隱藏 |
| `experimental_causal` *(選用)* | BOOLEAN | `False` | 啟用因果 grid（Wan / LTX）的續接；接縫可能不連續。關閉時在這些 grid 上續接 → `E_CAUSAL` |
| `on_conflict` *(選用)* | dropdown | `error` | `error`：既有遮罩與續接區衝突時停止（`E_MASK_CONFLICT`）；`carry_wins`：覆寫續接區的 samples 與 mask |

**Socket**：

| 輸入 | 型別 | 說明 |
|---|---|---|
| `target_latent` | LATENT | 必填。本段的空 latent，形狀需已是最終尺寸；時間長度必須等於本段 token 數（否則 `E_TOKENS`） |
| `segment` | MF_SEGMENT | 必填；來自 Select Segment |
| `state` *(選用)* | MF_SEGMENT_STATE | 前一段 Append 的 `state`（迴圈：Start Loop 的 `current_iteration_value`）；與 `previous_latent` 擇一 |
| `previous_latent` *(選用)* | LATENT | 前一段取樣後的 latent（線性串接）；與 `state` 擇一 |

**輸出**：`latent` LATENT（→ 你的 sampler）；`report` STRING（JSON：複製區間、token 數、要求與實際生效的遮罩值、衝突、provenance、警告）。

**說明**：輸出的 latent 帶有 `mf_carry` 標記，Append 會檢查它。不檢查 conditioning（prompt、keyframe、reference）—— `report` 固定寫 `conditioning_check: "unavailable"`。有重疊的段卻 `state` 與 `previous_latent` 都沒接 → `E_CARRY_SOURCE`。[鎖定原音](#保留原音)與續接的音訊 token 重疊時，該段略過音訊續接（`W_LOCK_CARRY`；report 的 `effective` 欄位寫 `lock_conflict`）。

**範例**：H3 迴圈 —— `mask = hard`、`carry_audio = True`、`state` ← Start Loop 的 `current_iteration_value`。Wan 2.2 high / low 見[兩段式取樣器](#兩段式取樣器)。

---

#### 🧩 Encode H3 Segment (`MF_EncodeH3Segment`)

把一段的 prompt 與素材編成 MiniMax H3 conditioning 與空 AV latent：參考經 core `MiniMaxH3ReferenceToVideo`、關鍵幀與錨定片段依幀序經 core `MiniMaxH3AddGuide`，最後把[鎖定原音](#保留原音)寫進 latent。素材只在這次呼叫內解碼。

**適用情境**：H3 迴圈路徑，取代 `MiniMaxH3ImageToVideo` 的位置（Select Segment 的 `segment` → 這裡 → Extend Latent 的 `target_latent`）。Sample Segments 內部也用它。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `width` | INT | `1344` (32–8192, step 32) | 生成尺寸 |
| `height` | INT | `768` (32–8192, step 32) | |
| `ref_image_size` | dropdown | `match` | `match`：參考圖縮到生成畫面的面積（較快）；`max`：短邊 2048（較接近原圖、慢數倍）。短邊超過 2048 的圖片載入時都會先縮小 |
| `ref_video_short_edge` *(選用)* | INT | `0` (0–4096, step 32) | 參考影片解碼時短邊最多到此值（32 對齊），可減少參考 token 與記憶體。`0`：短邊超過 768 的影片以 768 解碼，其餘交給 core 照常縮放 |

**Socket**：`segment`（MF_SEGMENT）、`clip`（H3 文字編碼器）、`vae`（H3 video VAE）、`audio_vae` *(選用)* —— 本段有參考音訊、設了 `audio = true` 的參考或錨定片段、或鎖定原音時必接。

**輸出**：`positive` CONDITIONING；`latent` LATENT（有鎖音時帶 `noise_mask` 與 Append 會檢查的 `mf_lock` metadata）。

**說明**：只限 H3 grid。每段上限與裁切由 Edit Timeline 套用：圖片 ≤ 9、影片 ≤ 3、獨立音訊參考 ≤ 3（`E_REF_LIMIT`）；參考影片先限制到段長、再向下裁到 5 + 17k 幀（< 5 → `E_REF_SHORT`）。core 的 H3 節點不存在或介面改變 → `E_H3_NODES`。Edit Timeline 執行後素材檔被換掉 → `E_SOURCE_CHANGED`。

---

#### 🎛️ Sample Segments (`MF_SampleSegments`)

MiniMax H3 時間軸的低記憶體單節點取樣器。執行時逐段展開成一組節點 —— `🧩 Encode H3 Segment` → `🧬 Extend Latent` → core `RandomNoise` + `BasicGuider` / `CFGGuider` + `SamplerCustomAdvanced` → `➕ Append Segment` —— 並回傳最後一個 Append 的 state。見[時間軸與長片](#時間軸與長片)、[鎖定原音](#保留原音)與[增量重算](#增量重算)。

**適用情境**：H3 時間軸（`generate`、`grid = h3_5+17n`、24 fps；其他設定會在展開前報錯），取代迴圈。

| Widget | 型別 | 預設 | 說明 |
|---|---|---|---|
| `noise_seed` | INT | `0`（+ `control_after_generate`） | 時間軸上沒有自己 seed 的段都用這個 |
| `width` | INT | `1344` (32–8192, step 32) | 生成尺寸 |
| `height` | INT | `768` (32–8192, step 32) | |
| `mask` | dropdown | `hard` | 同 Extend Latent |
| `carry_audio` | BOOLEAN | `True` | H3 音訊續接；鎖定原音落在續接音訊上的段會自動略過（`W_LOCK_CARRY`） |
| `cfg` *(選用)* | FLOAT | `1.0` (0–100, step 0.1) | 只在接了 `negative`（CFG guider）時使用；沒接 `negative` 時隱藏 |
| `ref_image_size` *(選用)* | dropdown | `match` | 同 Encode H3 Segment |
| `ref_video_short_edge` *(選用)* | INT | `0` (0–4096, step 32) | 同 Encode H3 Segment |
| `video_taper_tokens` *(選用)* | INT | `2` (0–1000000, step 1) | 同 Extend Latent。`mask = hard` 或 `linear` 時隱藏 |
| `audio_taper_tokens` *(選用)* | INT | `2` (0–1000000, step 1) | 同 Extend Latent。`mask = hard` 或 `linear`、以及 `carry_audio` 關閉時隱藏 |
| `on_conflict` *(選用)* | dropdown | `error` | 同 Extend Latent |

**Socket**：

| 輸入 | 型別 | 說明 |
|---|---|---|
| `model` | MODEL | H3 模型（例如 UNet + turbo LoRA） |
| `clip` | CLIP | H3 文字編碼器 |
| `vae` | VAE | H3 video VAE —— 編碼參考素材、逐段解碼畫面 |
| `segment_plan` | MF_SEGMENT_PLAN | 來自 Edit Timeline |
| `sampler` | SAMPLER | 例如 core `KSamplerSelect` |
| `sigmas` | SIGMAS | 例如 core `BasicScheduler` |
| `audio_vae` *(選用)* | VAE | H3 audio VAE —— 解碼音訊、編碼音訊類素材；時間軸有參考音訊、配對音軌或鎖定原音時必接。沒接時成品沒有音軌 |
| `negative` *(選用)* | CONDITIONING | 接上時以 `CFGGuider` 與 `cfg` 取樣；不接時用 `BasicGuider`（turbo 模型） |

**輸出**：`state`（MF_SEGMENT_STATE）→ Assemble Segments；`report` STRING（逐段 JSON：有效 seed、是否為鏡頭起點、三種身分、標籤、參考影片的要求 / 有效幀數、錨定、latent 鎖音 token、是否略過音訊續接、素材指紋、各鎖音的編碼政策（`ctx_v1`）與規劃的上下文 token、逐段 planned `effective` 清單（seed、音訊續接、taper、鎖音——之後由 Append 的漂移檢查驗證——與參考裁切），以及 `estimates`（解碼後 RGB 位元組、spill 位元組、參考素材解碼大小）；另有 `cfg` 與音訊解碼的 `effective` 欄位）。估算是大小，不是記憶體保證。

**說明**：所有模型輸入都從你自己的節點連進節點組；每段規格以常數傳入，所以沒變的段直接取自快取。組內節點的錯誤顯示在這個節點上，訊息標籤是內部節點名稱。只支援 batch 1。

## AI Provider Recipes

`MF_AIConfig` 輸出的 dict 由所有 AI 節點消費。同一個 `provider` / `base_url` / `api_key` / `model` 介面 — 只是值換。節點上選服務會自動填好，下面這些給 API 呼叫或 `自訂` 服務用：

### OpenAI（官方）

```
provider   = openai_compatible
base_url   = https://api.openai.com/v1
api_key    = sk-...
model      = whisper-1         # 給 MF_WhisperTranscribe
           = gpt-6-luna        # 給 MF_TranslateSubtitle
```

要錢；最穩定。OpenAI 公告 `whisper-1` 將於 2027-02-26 停用；官方建議的 `gpt-transcribe` 文件只列 JSON 輸出、沒有時間軸，目前無法用來產生字幕。

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

### OpenRouter（僅翻譯）

```
provider   = openai_compatible
base_url   = https://openrouter.ai/api/v1
api_key    = env:OPENROUTER_API_KEY
model      = ~openai/gpt-sol-latest           # `~` 開頭的別名永遠指向該系列最新模型
           = ~anthropic/claude-sonnet-latest
```

改用其他 `env:` 變數名稱時，要在啟動 ComfyUI 的環境另外設定 `MF_AI_KEY_ALLOWED_HOSTS_<變數名>=openrouter.ai`。

### Gemini（僅翻譯）

```
provider   = openai_compatible
base_url   = https://generativelanguage.googleapis.com/v1beta/openai
api_key    = env:GEMINI_API_KEY
model      = gemini-3.8-flash
```

改用其他 `env:` 變數名稱時，要在啟動 ComfyUI 的環境另外設定 `MF_AI_KEY_ALLOWED_HOSTS_<變數名>=generativelanguage.googleapis.com`。

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
- **MF_SEGMENT_PLAN**（v2）：`MF_EditTimeline` 輸出的不可變計畫 dict —— `plan_id` / `execution_id`、mode、fps、grid、來源參照、全域 prompt、素材快照（相對 `ref`、指紋、讀到的長度 / 尺寸 / 音訊格式）、refs、anchors、分段、逐段規格、`total_frames`。不含絕對路徑。
- **MF_SEGMENT**（v2）：不可變單段規格 —— 段號、start / end / length、重疊、續接大小、接縫、來源區間、解析後的 prompt、本段素材、鎖定原音，以及三種身分：`encode_id`（送進編碼器的內容）、`sampling_id`（往前串到上一個鏡頭切換，加上 seed）、`assembly_id`（串過前面每一段）。`MF_SelectSegment` 輸出 `view = full`；Sample Segments 給每個內部節點精簡的 `encode` / `extend` / `assemble` view。消費端會檢查 view 與欄位（`E_VIEW`）。
- **MF_SEGMENT_STATE**（v2）：不可變 dict；每次 `MF_AppendSegment` 都回傳新的一份（session、暫存目錄、已提交的幀 / samples、`assembly_id`、鎖音、存在磁碟的待 crossfade 尾端、續接用的 latent 尾端）。它在程序內持有分段暫存檔，所以不能序列化進 workflow。
- **LATENT `mf_carry`**：`MF_ExtendLatent` 在輸出的 LATENT dict 加上 `{sampling_id, segment_index, streams_carried, carry_dtypes, mask, taper}`；core sampler 會保留它。它在取樣**之前**寫入：能擋下計畫內容或 seed 不同的 latent 混接，但不能證明 sampler 真的用了該 seed、模型或 sigmas，也不涵蓋主來源影片（由 Edit Timeline 的指紋與 Load Segment Frames 檢查）。Append 把它當一致性提示（consistent / absent / mismatch），不是來源證明；`carry_dtypes`（續接區寫入時的 dtype，例如 `bfloat16`）是 `E_CARRY_DRIFT` 比對的基準。
- **LATENT `mf_lock`**：`MF_EncodeH3Segment` 寫入鎖定原音時加上 `{policy, tokens: [[K0, K1], …], target: [tensor, …]}` —— 鎖定的音訊 token 範圍與寫入值的獨立複本。Append 以 `target` 比對取樣後的 token（`E_LOCK_DRIFT`）。

## Architecture

```
comfyui_MediaForge/
├── __init__.py              # pkgutil 自動發現 nodes/ — 丟檔進去就出現
├── pyproject.toml
├── requirements.txt         # ffmpeg binary fallback + 輕量純 Python 相依；大型選用相依仍走 lazy import
├── nodes/                   # 一節點一檔，類別命名 MF_<Verb><Noun>
│   ├── ai_config.py            # MF_AIConfig
│   ├── append_segment.py       # MF_AppendSegment  — Timeline：解碼、去重疊、暫存到 .mf_tmp/
│   ├── assemble_segments.py    # MF_AssembleSegments  — Timeline：最終編碼（OUTPUT_NODE）
│   ├── start_video_batch.py    # MF_StartVideoBatch  — Video Batch：每次執行解碼一批
│   ├── end_video_batch.py      # MF_EndVideoBatch  — Video Batch：驅動各批、最終編碼（OUTPUT_NODE）
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
│   ├── edit_timeline.py        # MF_EditTimeline  — Timeline 計畫（+ web/timeline_editor.js）
│   ├── encode_h3_segment.py    # MF_EncodeH3Segment  — Timeline：經 core MiniMaxH3* 節點產生 H3 conditioning + 空 AV latent
│   ├── extend_latent.py        # MF_ExtendLatent  — Timeline latent 尾端續接（MediaForge/AI）
│   ├── extract_audio.py        # MF_ExtractAudio  — MediaForge/Audio（Phase 6 第一個節點）
│   ├── load_segment_frames.py  # MF_LoadSegmentFrames  — Timeline：逐段讀來源畫面 / 音訊
│   ├── load_segment_media.py   # MF_LoadSegmentMedia  — Timeline：把本段一個素材取成 IMAGE / AUDIO
│   ├── load_video_frames.py    # MF_LoadVideoFrames
│   ├── loop_video.py           # MF_LoopVideo
│   ├── probe_media.py          # MF_ProbeMedia
│   ├── sample_segments.py      # MF_SampleSegments  — Timeline：H3 單節點取樣器（以 graph expansion 組合 core 節點）
│   ├── save_video_frames.py    # MF_SaveVideoFrames
│   ├── select_segment.py       # MF_SelectSegment  — Timeline：每輪取出一段規格
│   ├── select_video.py         # MF_SelectVideo  — input/ 影片 dropdown picker
│   ├── translate_subtitle.py   # MF_TranslateSubtitle
│   ├── trim_by_ranges.py       # MF_TrimByRanges
│   └── whisper_transcribe.py   # MF_WhisperTranscribe
├── utils/
│   ├── ass_style.py         # ASS 字幕 style helpers(BurnSubtitle + ComposeBurnSubtitle 共用)
│   ├── audio_mix.py         # amix / afade / volume / loudnorm filter builders
│   ├── cache_key.py         # path_fingerprint：給 IS_CHANGED 用的 mtime / size 指紋
│   ├── color.py             # hex_to_ass_color:#RRGGBB → ASS BGR+alpha
│   ├── compose_ir.py        # ComposeIR + AudioOp + compile_ir + compile_audio_chain
│   ├── compose_ops.py       # MF_COMPOSE_OPS / AUDIO_OPS dispatch + watermark resolver
│   ├── encoder.py           # codec catalog + NVENC probe + pick_default_codec + build_encoder_args
│   ├── ffmpeg.py            # ensure_ffmpeg / run_ffmpeg / probe / escape_filter_path
│   ├── h3_adapter.py        # Timeline：core MiniMax H3 節點 adapter + latent 鎖定原音
│   ├── latent_carry.py      # Timeline：latent stream 拆分、尾端擷取、續接遮罩、provenance、鎖音檢查
│   ├── output_path.py       # resolve_output_path + output_path_to_ui_entry (API metadata helper)
│   ├── plugin_tmp.py        # .mf_tmp/ 暫存檔 + 清理過期的檔案與 session 目錄
│   ├── segment_spill.py     # Timeline：不可變 state、FFV1 / WAV 分段暫存、crossfade、鎖音母帶、最終編碼
│   ├── video_batch.py       # Video Batch：接線規則、分批數學、來源檢查、重複執行同一批 node ID 的工作
│   ├── timeline_materials.py# Timeline：materials / refs / anchors、幾何、逐段 manifest、H3 標籤、別名
│   ├── timeline_media.py    # Timeline：素材快照、圖片 / 影片 / 音訊切片解碼
│   ├── timeline_messages.py # Timeline：訊息目錄（穩定的 E_* / W_* key）
│   ├── timeline_plan.py     # Timeline：時基、grid、timeline_data 驗證、分段規格、view、身分
│   ├── timeline_routes.py   # Timeline：編輯器用的 /mediaforge/timeline/probe + /proxy route
│   ├── timeline_source.py   # Timeline：input/ 來源解析、安全的 FFmpeg 輸入旗標、檔案指紋
│   └── video_io.py          # rawvideo pipe ↔ IMAGE/AUDIO + encode_tensor_to_tempfile
├── font/                    # 丟 .ttf / .otf 進來給 MF_BurnSubtitle 的 font_file dropdown
├── example_workflows/       # Timeline 範例 workflow（會出現在 ComfyUI 範本瀏覽器）
├── web/
│   ├── ai_config_mask.js    # 前端 extension：在 canvas 上把 MF_AIConfig 的 api_key 遮罩起來
│   ├── ai_config_services.js # MF_AIConfig 的服務與模型清單
│   ├── ai_config_ui.js      # 前端 extension：MF_AIConfig 的服務 / 模型下拉
│   ├── dual_input_lock.js   # 前端 extension：依 dual-input 模式隱藏不適用 widget
│   │                        #   - lock_widget / hidden_when_connected: tensor 模式隱藏
│   │                        #   - linked_widgets: path 模式隱藏
│   ├── timeline_edit.js     # 編輯器的編輯命令（純函式：建立 / 移動 / 修剪 / 分割、鏡頭切換）
│   ├── timeline_editor.js   # Edit Timeline 視覺化編輯器（DOM widget）
│   ├── timeline_grid.js     # grid / 驗證規則，與 utils/timeline_plan.py 對拍
│   ├── timeline_materials.js# 素材規則，與 utils/timeline_materials.py 對拍
│   └── timeline_strings.js  # 編輯器介面文字（繁體中文）
└── tests/                   # 開發者本地 pytest suite — gitignored，不隨公開 repo 發佈（見「測試」章節）
    ├── test_compose_ir.py        # IR spike case（Phase 4 prerequisite）
    ├── test_compose_e2e.py       # real-ffmpeg e2e
    ├── test_video_io_roundtrip.py# PSNR > 38 dB rawvideo roundtrip
    ├── test_timeline_*.py        # Timeline 計畫 / 素材 / 節點 / route / 範例 / 真 core 快取（T09）
    ├── test_sample_segments_core.py # Sample Segments 展開 + 快取，跑在真 core executor（T19）
    ├── js/*.test.mjs             # grid / 素材 / 編輯命令規則對共用 golden vectors（node --test）
    └── test_codex_r*_fixes.py    # 各輪 codex review 留下的回歸測試
```

**加新節點**：丟一個 `nodes/<verb>_<noun>.py`，內含 `MF_<Verb><Noun>` 類別 + `NODE_CLASS_MAPPINGS` / `NODE_DISPLAY_NAME_MAPPINGS`。Aggregator 會自動撿到 — 重啟 ComfyUI 即可。

> ⚠️ **Import 失敗**：`__init__.py` 逐一 import 每個節點 module —— import 失敗的 module 會被跳過，console 印出 `[MediaForge] 節點模組 <name> 載入失敗：<錯誤>`，其他節點照常註冊。要看完整 traceback：`python -c "from custom_nodes.comfyui_MediaForge.nodes.<your_file> import *"`。選用相依（`requests`、`faster-whisper`、`yt-dlp`）**必須** lazy import 在 FUNCTION method 裡，不能放 module top。

## 系統需求

- ComfyUI
- Python ≥ 3.10
- **FFmpeg + FFprobe** — 建議裝在系統 PATH；沒有時改用 `requirements.txt` 裡的 `imageio-ffmpeg`（只有 ffmpeg）與 `static-ffmpeg`（ffmpeg + ffprobe，第一次用到時下載）
- `requirements.txt` 的 Python 套件 — ComfyUI Manager 會自動安裝；手動 clone 後請執行 `pip install -r requirements.txt`
- 選用：`faster-whisper`（本地 Whisper backend） — 第一次用到才會 lazy import
- Timeline 節點：不需額外 pip 套件（`psutil`、`torch`、`numpy`、`Pillow` 隨 ComfyUI 安裝）；Encode H3 Segment / Sample Segments 需要 core 的 `MiniMaxH3ReferenceToVideo` / `MiniMaxH3AddGuide` 節點；測試環境為 ComfyUI core 0.42.5 / frontend 1.55.18 —— 不是最低版本需求（見[相容性](#相容性)）

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
pip install -r comfyui_MediaForge/requirements.txt
```

重啟 ComfyUI — 節點會出現在 `MediaForge/Subtitle | Video | Analysis | Audio | Compose | AI`。

### 選用相依（lazy — 你會用到才裝）

```bash
pip install faster-whisper    # 只給 backend="faster_whisper_local" 用
```

## 疑難排解

| 症狀 | 可能原因 | 修法 |
|---|---|---|
| 重啟後 menu 找不到節點 | Module-level import 錯 —— console 會印 `[MediaForge] 節點模組 <name> 載入失敗：…`，其他節點照常載入 | `python -c "from custom_nodes.comfyui_MediaForge.nodes.<file> import *"` 看完整 traceback |
| `RuntimeError: ffmpeg ... failed` | FFmpeg exit code 非 0 | 往上滑 — raise 前面已經印了 FFmpeg stderr 最後 30 行 |
| FFmpeg 在 filter 階段抱怨路徑裡的 `:`（Windows） | 路徑沒 escape 就進 filter graph | 我們會自動走 `escape_filter_path()` — 只有自製節點繞過它才會中招 |
| 檔名含 `[ ] ' , ;` 等 filter 特殊字元，字幕/drawtext/overlay 節點出錯 | 跟 `:` 同一個根因 — 這些字元在 filtergraph 語法裡也有特殊意義 | 同樣自動處理：`escape_filter_path()` 現在做完整的兩層 FFmpeg escape（`: ' \ [ ] , ;`），不只 colon。只影響 filter graph 內的路徑（`subtitles=`、`textfile=` 等）— 純 `-i` 參數本來就不需要 escape |
| `MF_ConcatVideos` `mode=copy` raise 出一份逐檔 codec/解析度/pix_fmt/音訊差異列表 | Preflight probe 發現輸入彼此不相容、無法 stream copy | 正常行為 — concat demuxer + `-c copy` 對不相容輸入以前會 exit 0 但輸出從第二段開始壞掉。改用 `mode=transcode`（一定可用、一定 re-encode） |
| Whisper local backend 超慢 | CTranslate2 在 CPU 上推論 | 在 `MF_AIConfig` 設 `device = "cuda"`；或改 hosted backend（Groq 比本地 CPU 快非常多） |
| `loop` filter 在超長素材報錯 | Frame 緩衝上限是 `MAX_LOOP_FRAMES = 32767`（INT16） | 降低 fps，或改 `crossfade` mode（走 xfade chain，沒有單一緩衝上限） |
| 翻譯輸出行數對不上原文 | 小 LLM 把編號批次搞混了 | 換大模型（`gpt-6.1-terra`、`llama-3.3-70b-versatile`）；prompt 已加行號對齊，但小模型超過 50 行還是會漏 |
| `reverse` filter 在長片爆記憶體 | `reverse` 會把整段串流載入 RAM | 目前是硬上限；先裁短或自己 split-and-stitch |

### 時間軸訊息

Timeline 節點的訊息格式為 `[<節點名稱>] <KEY>：<訊息>`，以下依 key 索引。Sample Segments 內部發生的錯誤顯示在 Sample Segments 節點上，`<節點名稱>` 則是實際報錯的內部節點。

| Key | 節點 | 訊息 | 修法 |
|---|---|---|---|
| `E_TIMELINE_JSON` | Edit Timeline | 時間軸 JSON 無效：{reason}。請修正 {location}。 | 修正訊息指出的欄位（格式見[時間軸 prompt](#時間軸-prompt)）；編輯器會在該段標出同一個錯誤 |
| `E_H3_FPS` | Edit Timeline | H3 grid 僅支援 24 fps。請將 fps 設為 24。 | 設 `fps = 24`，或改用其他 `grid` |
| `W_SOURCE_TIME_UNKNOWN` | Edit Timeline | 無法取得來源的起始時間，暫以 0 計算；預覽與來源時間可能略有偏移。 | 僅警告，執行會繼續 |
| `E_SEGMENT_INDEX` | Select Segment | segment_index 超出範圍：計畫共 {count} 段（0 起算），收到 {index}。 | Start Loop 的 `max_iteration` 接 Edit Timeline 的 `segment_count` |
| `E_SOURCE_CHANGED` | Load Segment Frames / Encode H3 Segment / Load Segment Media / Assemble Segments | 來源影片已變更，請重新執行 Edit Timeline。 | Edit Timeline 執行後來源或素材檔被換掉 —— 重新 queue，讓 Edit Timeline 重讀檔案 |
| `E_SOURCE_SHORTFALL` | Load Segment Frames / Encode H3 Segment / Load Segment Media | 來源影片不足以涵蓋第 {segment} 段。請改用 hold_last / black 或調整時間框。 | Load Segment Frames：`shortfall` 改 `hold_last` / `black`，或把該段移回來源範圍內。素材：影片實際解出的幀數比讀到的長度少 —— 重新轉檔或縮短內容範圍 |
| `E_MATERIAL` | Edit Timeline | 素材「{name}」無法使用：{reason}。請確認檔案在 input/ 內且格式正確。 | 修正或替換檔案（選取卡片、改檔案），或刪除該素材 |
| `E_REF` | Edit Timeline | 參考 {ref} 無效：{reason}。請在時間軸上調整。 | 參考起點超出時間軸總長、需要超出素材長度的幀，或對沒有音軌的檔案開了 `audio` —— 移動或縮短它，或關掉 `audio` |
| `E_ANCHOR` | Edit Timeline | 錨定 {anchor} 無效：{reason}。請在時間軸上調整。 | 錨定位置必須在時間軸總長之內；錨定片段必須完整落在單一段的輸出區間內，H3 長度須為 5、22、39 …（17k + 5）幀；內容不得超出素材；`audio` 需要音軌 |
| `E_ANCHOR_OVERLAP` | Edit Timeline | {stream} 錨定 {first} 與 {second} 的時間重疊。請把它們錯開。 | video：關鍵幀與錨定片段；audio：鎖定原音與開了 `audio` 的錨定片段 —— 移開其中一個 |
| `E_REF_LIMIT` | Edit Timeline | 第 {segment} 段的 {kind} 有 {count} 個，超過 H3 上限 {limit}。請縮小參考範圍或移到其他段。 | H3 每段上限：圖片 ≤ 9、影片 ≤ 3、獨立音訊參考 ≤ 3 |
| `E_REF_SHORT` | Edit Timeline | 第 {segment} 段的參考影片「{name}」只有 {frames} 幀（至少 5 幀）。請延長參考或移出本段。 | 參考影片在它所屬的每一段內都需要 ≥ 5 幀 |
| `E_PROMPT_REF` | Edit Timeline | 第 {segment} 段 prompt 的素材引用無效：{reason}。 | `<@名稱>`：沒有這個素材；素材不在本段的參考中（或只用於錨定）；本段有兩個它的參考 —— 改寫標籤；`:audio` 用在沒有配對音軌的參考；標籤編號超出本段數量；非 H3 grid 使用別名或標籤 |
| `E_H3_NODES` | Encode H3 Segment | 找不到可用的 core MiniMax H3 節點（{nodes}）。請更新 ComfyUI。 | core 的 `MiniMaxH3ReferenceToVideo` / `MiniMaxH3AddGuide` 不存在或介面改變（或 audio VAE 回傳的形狀不符）—— 更新 ComfyUI；測試環境為 core 0.42.5 |
| `E_MEDIA_MISSING` | Load Segment Media | 第 {segment} 段沒有第 {ordinal} 個 {kind}（本段共 {count} 個）。請調整 ordinal。 | `ordinal` 不要超過 `count`；素材只會出現在它所屬的段 |
| `E_VIEW` | 吃 `segment` 的節點 | segment 格式不符：{reason}。 | 舊版 segment —— 重新執行 Edit Timeline；或在需要完整規格處接了精簡 view —— 改接 Select Segment 的 `segment` |
| `E_TOKENS` | Extend Latent | latent 時間長度不符：預期 {expected}，收到 {actual}。請檢查 grid、VACE reference 與 guide 接線。 | 用 Select Segment 的 `length` 建空 latent、`grid` 與模型相符；`target_latent` 不能含 VACE reference 或 guide token |
| `E_AUDIO_CARRY` | Extend Latent | 此 latent 不支援音訊續接。請關閉 carry_audio。 | 關閉 `carry_audio`（僅限 H3）；LTX-2 改把解碼後的音訊接 Append 的 `audio` |
| `E_CARRY_SOURCE` | Extend Latent | 本段需要續接資料。請連接 state 或 previous_latent（擇一）。 | 迴圈：Start Loop 的 `current_iteration_value` → `state`；線性串接：前一段取樣後的 latent → `previous_latent` |
| `E_CAUSAL` | Extend Latent | 此續接模式仍在實驗階段。確認後開啟 experimental_causal。 | 開啟 `experimental_causal`（因果 grid：Wan / LTX） |
| `E_MASK_CONFLICT` | Extend Latent | 續接區與既有遮罩衝突。請調整接線，或選擇 carry_wins。 | 設 `on_conflict = carry_wins`，或移除另一個遮罩 |
| `W_LOCK_CARRY` | Extend Latent | 第 {segment} 段的鎖定原音落在續接區，略過本段音訊續接（video 續接照常）。 | 只是警告。該接縫需要音訊續接時，把鎖音移到重疊區之後 |
| `E_PROVENANCE` | Extend Latent / Append Segment | 續接標記與本段不符（{field}）。請檢查分段接線。 | 同一輪的節點都要接同一個 Select Segment 輸出；latent 必須來自本段的 Extend → sampler |
| `W_PROVENANCE` | Extend Latent / Append Segment | 缺少續接標記；將繼續執行。請檢查分段與 guide 接線。 | 確認進入 Append 的 latent（或 `previous_latent`）有經過 Extend Latent |
| `E_SEQUENCE` | Append Segment / Assemble Segments | 預期第 {expected} 段，收到第 {actual} 段。請連接前一段的 state。 | 第一段 `state` 留空；之後各段接前一段 Append 的 `state`；Assemble 要接最後一段之後的 state |
| `E_PLAN` | Append Segment | 分段與 state 屬於不同計畫。請重新執行整個迴圈。 | state 目前累積的分段與這個計畫不符 —— 改過時間軸或來源後，整個迴圈重跑 |
| `E_CARRY_DRIFT` | Append Segment | 續接區在取樣後被改變（最大差 {drift}）。兩段式取樣（例如 Wan 2.2 high / low）請在第二個 sampler 前再接一次 Extend Latent，on_conflict 設 carry_wins。 | 見[兩段式取樣器](#兩段式取樣器) |
| `E_LOCK_DRIFT` | Append Segment | 鎖定原音檢查失敗（{reason}）。請確認 sampler 保留 noise_mask 與 latent 的 mf_lock，或改用完整 schedule。 | 取樣後的 latent 遺失 `mf_lock`（Encode H3 Segment 與 Append 之間有節點把它丟掉）或鎖定 token 被改變 —— 改用會遵守 `noise_mask` 的 core sampler 與完整 schedule |
| `E_RAM` | Append Segment | 本段估計需 {gib} GiB RAM，超過預檢門檻。請縮短段長或降低解析度。 | 照訊息處理；門檻是可用 RAM 的 80% |
| `E_DISK` | Append Segment | 暫存空間不足，需至少 {gib} GiB。請釋出空間後重試。 | 清出 plugin `.mf_tmp/` 所在磁碟的空間 |
| `E_AUDIO_LAYOUT` | Append Segment | 各段音訊的有無、取樣率或聲道必須一致。請在上游統一音訊格式。 | 每段音訊都來自同一個來源 / VAE，或全部不接 |
| `W_AUDIO_PAD` | Append Segment / Encode H3 Segment / Load Segment Media / Assemble Segments | 已在接縫或結尾補入 {samples} 個靜音樣本。 | Append：僅提示（H3 會補片尾，例如 267 samples）。素材節點：素材音訊比讀到的長度提早結束 —— 重新轉檔或縮短使用範圍 |
| `E_SPILL_MISSING` | Assemble Segments | 分段暫存檔已不存在。請重跑整個迴圈，再組裝。 | 分段檔被刪了（例如清掉 `.mf_tmp/`，或被快取逐出）；重跑迴圈 |
| `W_PROXY` | 時間軸編輯器 | 預覽建立失敗；時間軸仍可編輯。請重試。 | 編輯器顯示「預覽失敗（時間軸仍可編輯）」與「重試」按鈕；沒有預覽也能編輯 |
| — | core Start / End Loop | `Loop body is not closed` | 迴圈本體內的 preview / 副作用節點沒接到 End Loop 的 `terminations` —— 接上，或移到迴圈外 |

### Video Batch 訊息

Video Batch 節點的訊息開頭是 `[Start Video Batch]` 或 `[End Video Batch]`。

| 訊息 | 怎麼辦 |
|---|---|
| 需要接到一個 End Video Batch | 加上 End，把處理後的畫面接到它的 `frames` |
| 一個工作流只能有一個 End Video Batch | 刪掉多餘的 |
| 有 Start Video Batch 沒有接到 End Video Batch | 接上或刪掉 |
| `frames` 必須來自 Start Video Batch | 把 End 的 `frames` 接回 Start（可以經過你的節點） |
| End Video Batch 下游的節點不能直接讀批次內的畫面 | 那個節點只會拿到其中一批，請改接 `final_video_path` |
| 批次內不能放 Start / End Loop | 移到批次外 |
| Start 的輸入不能依賴批次內的節點 | 改接批次外的來源 |
| End 的設定不能接線 | 直接在節點上設定 |
| `frames_per_batch` 至少是 1；`overlap_frames` 必須介於 0 和它的一半之間 | 改數值 |
| 批次內有 MiniMax H3 節點，`frames_per_batch` 必須符合 17n+5 | 用訊息建議的值，例如 124 |
| 還沒有選影片；找不到影片 | 上傳或選一支影片，或接 `video_path` |
| 格式尚未通過批次解碼驗證（TS、MPG） | 轉檔：`ffmpeg -i in.ts -c copy out.mkv` |
| HDR 或廣色域影片尚未支援 | 先轉成 SDR / BT.709 |
| 無法判斷影片起點；無法判斷 fps | 先轉檔；fps 可在 End 設定 |
| 某支影片低於 5 fps，其他支比它快 | 通常是上游 fps 設錯，例如 Loop Video 的 `tensor_fps`；確定要這樣就在 End 填 `fps` |
| 這批畫面約需 X GiB RAM（Start，解碼前） | 調低 `frames_per_batch`，或用 `resize` 縮小畫面 |
| 從第 N 幀起沒有畫面 | 確認影片沒有損毀、不是空檔案 |
| 來源在處理途中改變了 | 執行中不要替換檔案，重新執行 |
| 第 N 批沒有拿到畫面；第 N 批被上游節點阻擋 | 檢查接到 `frames` 的節點，批次流程不能跳過某一批 |
| `frames` 必須是 `[幀數,高,寬,3]` 的 RGB；必須是 0–1 的浮點數 | 轉換輸出，例如拿掉 alpha |
| 第 N 批送進 B 幀，處理後變成 M 幀 | 這個節點改變了幀數，請移到 End 之後 |
| 畫面含 NaN 或無限大 | 上游節點可能失敗了 |
| 第 N 批只有 L 幀，沒有超過重疊 | 來源可能在處理中改變了，重新執行 |
| `audio` 必須是單一條音訊；被上游阻擋；是空的 | 只接一條，或拔掉 `audio` |
| 第 N 批音訊的取樣率或聲道和前面不同 | 讓每一批的音訊規格一致 |
| 來源沒有音軌，不能使用 `audio` 輸出 | 拔掉 Start 的 `audio`，或換有音軌的影片 |
| 兩個來源都有音軌，無法判斷要用哪一條 | 把要用的音訊接到 End 的 `audio`，或關閉 `keep_source_audio` |
| 音軌規格異常；音軌太大（暫存 WAV 上限約 4 GiB）；解碼音軌的 RAM 不夠 | 轉成 48 kHz 雙聲道：`ffmpeg -i 影片 -c:v copy -ac 2 -ar 48000 -c:a aac 輸出.mp4`，或關閉 `keep_source_audio` |
| 處理後的畫面是 W×H，這個 codec 需要偶數寬高 | 調整節點的縮放倍率，或在 End 前加縮放節點 |
| 這個 codec 試編碼失敗 | 改用其他 codec，例如 `h264 (libx264)` |
| 試編碼超過 60 秒沒有完成 | GPU 可能正忙。再跑一次，或改用 `h264 (libx264)` |
| 磁碟空間可能不夠 | 清出空間，或用 `max_frames`、`resize` 減少處理量 |
| 處理後每批約需 X GiB RAM | 調低 `frames_per_batch`，或用 `resize` 縮小畫面 |
| 此 ComfyUI 版本未通過相容檢查 | 這個工作依賴 core 內部介面，驗證過的版本是 core 0.42.5。請更新 MediaForge，或回報你的版本 |

## 常見問題

**Q：為什麼不全用 VideoHelperSuite 就好？**
A：VHS 在 IMAGE-batch workflow 很強。MediaForge 補的是**檔案級**操作（跨 codec 拼接、音訊感知裁切、廣播級編碼器）和**單次編碼多層 overlay** Compose。兩個一起裝。

**Q：會加 diffusion / 生成節點嗎？**
A：不會加模型載入或 model patch，也沒有自己的取樣程式 —— 這是設計決策，生成仍屬於 ComfyUI 核心 / 專門 plugin。MediaForge 提供後製端 AI（Whisper 轉錄、LLM 翻譯）與 Timeline 工具：Extend Latent 改寫你傳入的 `LATENT`、Append 用你接上的 `VAE` 解碼，最後交給 FFmpeg 編碼。MiniMax H3 的部分，Encode H3 Segment 只透過 core 的 `MiniMaxH3*` 節點產生 conditioning，Sample Segments 只以展開成 core 節點類別（`RandomNoise`、`BasicGuider` / `CFGGuider`、`SamplerCustomAdvanced`）的方式取樣，模型、sampler 與 sigmas 都來自你自己的節點（見[時間軸與長片](#時間軸與長片)）。

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

Timeline 測試（`test_timeline_*.py`、`test_latent_carry.py`、`test_segment_spill.py`、`test_load_segment_frames.py`、`test_h3_adapter_core.py`、`test_sample_segments_core.py`）跟 `tests/` 其他測試一樣只在開發者本地。真 core 測試 `tests/test_timeline_core_cache.py`、`tests/test_h3_adapter_core.py`、`tests/test_sample_segments_core.py` 會用 ComfyUI 的 interpreter 與 core 類別（快取測試會啟動多個 `main.py --cpu` 實例），必須在 ComfyUI 的 Linux / WSL 環境內跑。JS 測試需要 Node.js：

Video Batch 測試：`test_video_batch_plan.py`（規則與分批數學）、`test_video_batch_decode.py`（每個批次窗口與連續解碼比對；支援的 FFmpeg 版本都要跑）、`test_video_batch_core.py`（真 core 執行器，五種快取模式；需要 ComfyUI，設定 `COMFYUI_ROOT`）、`test_video_io_streaming.py`（Load / Save Video Frames 的峰值記憶體）。

Save Video 測試：`test_serial_h264.py`（路徑、中繼資料、用 ffprobe 與完整解碼檢查輸出檔、取消與失敗、同前綴並行、執行緒數；Linux 與 Windows 都能跑）、`test_save_video_core.py`（真 core 執行器，另起一個 `--disable-metadata` 的 server；需要 ComfyUI，設定 `COMFYUI_ROOT`）。

```bash
python -m pytest -q tests/test_timeline_core_cache.py tests/test_h3_adapter_core.py tests/test_sample_segments_core.py   # 在 ComfyUI 的 Linux / WSL 環境內
node --test tests/js/timeline_grid.test.mjs tests/js/timeline_edit.test.mjs
```

## Roadmap

- **Phase 2** ✅ Foundation bridges — LoadVideoFrames / SaveVideoFrames
- **Phase 3** ✅ Gap-priority — DetectSilence / TrimByRanges / ConcatVideos
- **Phase 4** ✅ Compose pipeline v1 — single-encode multi-overlay (Start + Finalize)
- **Phase 4.5** ✅ Compose pipeline v2 — 合一 ComposeVideo + 字幕進 chain + audio chain(Volume / AudioMix / Fade / Normalize)
- **Phase 5** 🚧 AI — WhisperTranscribe / TranslateSubtitle(schema 仍 experimental)
- **Phase 6** 🚧 獨立 Audio domain — `MF_ExtractAudio` 先上；檔案級 denoise、normalize-file、audio cut/trim、ducking 仍規劃中(Phase 4.5 的 Compose audio chain 是子集)
- **Timeline** ✅ 時間軸與長片 — Edit Timeline 編輯器、分段迴圈、latent 尾端續接、落盤組裝（MiniMax H3 AV 支援；LTX-2 / Wan 2.2 為實驗性）
- **Timeline v2** ✅ Director — 素材庫、參考 / 關鍵幀 / 錨定片段 / 鎖定原音、Encode H3 Segment、Load Segment Media、單節點 H3 Sample Segments（已用 H3 turbo 4-step 在 GPU 驗證，見[相容性](#相容性)）
- **Phase 7** ⏳ Net domain — yt-dlp ingest、HTTP fetch(lazy import)

## License

MIT — © YingLiang Lu (leon80148)。

## 致謝

- **FFmpeg** — 真正幹活的引擎；這個 plugin 90% 是在格式化 argument
- **ComfyUI** — runtime 跟節點圖引擎
- **VideoHelperSuite** — 先行作品，定義了 IMAGE-batch 契約，MediaForge 與其互通
- **faster-whisper / CTranslate2** — 本地 Whisper backend
- 時間軸規劃、素材時間軸與 latent 尾端續接的設計概念受到 [ComfyUI MiniMax H3 Timeline Director](https://github.com/Songssx/ComfyUI-MiniMaxH3-TimelineDirector) 啟發；MediaForge 獨立實作這些功能，不沿用該專案程式碼，並維持 MIT 授權。
- 💾 Save Video · Serial H.264 的匯出行為比照 [MiniMax H3 Audio T8](https://github.com/T8mars/comfyui-minimax-h3-audio-T8) 的單執行緒影片存檔節點；依行為規格撰寫，不沿用該專案程式碼，與 MediaForge 其他部分一樣採 MIT 授權。
