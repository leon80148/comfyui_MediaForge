# Viggle Remake 接 Edit Timeline

主流程保持三顆節點：

```text
Edit Timeline → Sample Remake Segments → Assemble Segments
                     ↑
          model、CLIP、video VAE、audio VAE
```

拖入同目錄的 `timeline_viggle_remake.json`。這是接線範本，模型與素材必須選自己的檔案；範例檔名沿用官方 swap 範例。此版本已做 CPU 合約與接線檢查，尚未完成真模型 GPU 生成驗收。

## 為什麼不能只掛 LoRA

Edit Timeline 輸出 `MF_SEGMENT_PLAN`；官方 Conditioning 需要的是影片 IMAGE batch、參考圖、prompt、pins，不能直接相接。
原本的 Sample Segments 使用 H3 原生參考編碼與 RandomNoise；Viggle 額外使用純文字呈現參考、特定 reference latent 排列、全段乾淨原音、按 sigma 釋放 keyframe，以及不同的亂數消耗順序。

新增的取樣器把官方 RemakeConditioning、RemakeKeyframeUntil、RemakeNoise，以及 core Euler／ManualSigmas／BasicGuider／SamplerCustomAdvanced 收進 graph expansion。MediaForge 只解碼時間軸素材、沿用 video latent 續接與逐段輸出，不複製官方模型 patch 或取樣引擎。

| 官方節點 | 職責 | 此整合如何處理 |
|---|---|---|
| Viggle Remake Conditioning | 來源影片、1–4 張改繪參考圖、pins、乾淨 audio latent | 每段解碼素材後呼叫已安裝的官方節點 |
| Viggle Remake Keyframe Until | sigma 低於門檻時釋放 keyframe，輸入／輸出 MODEL | 在內部子圖串接官方節點，預設 0.9 |
| Viggle Remake Noise | 依參考與 keyframe 形狀消耗亂數，再產生目標 noise | 每段串接官方節點，保留時間軸 seed 覆寫 |
| Viggle Remake Prefix | 將前一段 RGB 重編碼成乾淨前綴 | 此版沿用 MediaForge 的 hard video latent carry，並不宣稱等同 Prefix |

## 模型接法

未合併 LoRA：`fl2va base → LoraLoaderBypassModelOnly → Sample Remake Segments.model`。不要沿用 turbo LoRA 或四步 turbo 排程。CLIP 使用 H3 的 Qwen3-VL，type=minimax；video VAE、audio VAE 都要接。

已合併 Remake int8／GGUF：`對應 loader → Sample Remake Segments.model`，不再疊 Remake LoRA。`mode` 只切換 sigmas，不會替換上游模型；swap／edit 必須與所選權重一致。此版不從檔名猜測或驗證權重身分。

| mode | 六步 sigmas |
|---|---|
| swap | `1.0, 0.990476, 0.965243, 0.923077, 0.8, 0.631579, 0.0` |
| edit | `1.0, 0.983607, 0.96, 0.923077, 0.857143, 0.705882, 0.0` |

Euler、BasicGuider（CFG=1）已內建，不必再接 sampler／scheduler。

## 時間軸操作

1. 設為 generate、24 fps。將來源影片加入素材庫，放入「跟隨時間軸」的影片參考，完整覆蓋每個要生成的段落。**上方 video 下拉欄是預覽／來源定位；單獨選它不會自動變成 Remake 的模型輸入。**
2. 將 1–4 張改繪後的圖片加成該段的圖片參考。在 prompt 用 `@` 選素材；來源會解析成 `<Video 1>`，圖片解析成 `<Picture N>`。
3. 想固定改繪構圖時，把同一張圖片再放成關鍵幀。每張參考圖都要有一個 pin，或全部不 pin；關鍵幀在時間軸上的位置會自動換成該段內的 frame index。例如段落從第 85 幀開始，關鍵幀在第 97 幀，會傳 `pins=12`。
4. 先用單段 124 幀、short_edge=480。每段必須為 90–345 幀且符合 `17n+5`；合法值包括 90、107、124、141、158、175、192、209、226、243、260、277、294、311、328、345。16:9 來源對應 832×480 或 1280×736；直式寬高對調。
5. 要延長再加段落。保留既有 H3 重疊格點與接縫設定；跨段影片必須使用相同畫布尺寸。來源先依全域 24 fps 時鐘取樣，再取各段，避免每段重設 fps filter 造成接縫時序偏移。

## 提示詞與聲音

使用官方六段式：`subject_definitions`、`summary`、`retention_analysis`、`detailed_description`、`overall_soundscape`、`non_diegetic_music`。描述目標畫面，以及保留哪些來源內容。每段 prompt 的時間戳從 `00:00` 起算，必須符合該段長度；此版只檢查六段名稱，不自動改寫或完整解析時間戳。

`keep_source_audio=True` 會把該段來源音軌交給官方 Conditioning，全段 audio noise_mask=0；沒有來源音軌時是靜音。來源影片參考的「配對音訊」開關不決定這個設定，也不會建立官方未使用的 `<Audio N>` 提示詞。此版的輸出音軌經 audio VAE 重建，**不保證 PCM 逐 sample 與原始檔相同**。重疊自動音訊混合使用 linear，避免相同原音等功率疊加。

## 範圍與驗證限制

- 可保留：時間軸 GUI、分段 prompt、素材庫、圖片參考、圖片關鍵幀、seed、video latent 重疊續接、逐段落盤、最後一次編碼。
- 尚不支援：額外參考音訊、guide 影片錨定、獨立鎖音、latent 放大、二階段生成、SelfLift。遇到這些設定會先報錯。
- direct 模式的尺寸由取樣器 `short_edge` 決定；Edit Timeline 裡原本給 H3 生成用的 width／height 不會決定 Remake 畫布。
- 先縮放解碼來源，以限制 RGB 記憶體。此處使用 FFmpeg 的 fill 縮放；官方節點仍負責 conditioning。不能因此宣稱與官方直接輸入／sample.py 逐像素相同。
- 官方 RemakeConditioning 在實際編碼時解析參考與 pins；MediaForge 先檢查每段只有一支完整影片、1–4 張圖，以及每張圖各一個 pin 或全無 pin。
- 目前的 CPU 驗證不代表人物替換品質、長片接縫品質或實際顯存占用已通過驗收。

官方參考：[ComfyUI 安裝與模型接法](https://huggingface.co/Viggle/Viggle-Remake/blob/main/COMFYUI.md)、[節點原始碼](https://huggingface.co/Viggle/Viggle-Remake/blob/main/comfyui/ComfyUI-Viggle-Remake/nodes.py)、[參考圖與提示詞](https://huggingface.co/Viggle/Viggle-Remake/blob/main/PROMPTING.md)。
