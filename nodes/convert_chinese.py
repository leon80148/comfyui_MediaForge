"""MF_ConvertChinese — Chinese simplified ↔ traditional conversion (OpenCC).

字元級轉換、適用於任意中文文字（含 SRT）。SRT 的 timestamp / 序號 / 空行
都是 ASCII，不會被 mapping 動到 — 整段直接 .convert() 即可、不需要解析 SRT 結構。

I/O 三段式：
1. text widget 貼上 / wire 上游 → in-memory chain (Whisper → Translate → OpenCC → Burn)
2. input_path 填路徑 → 沒 text 時讀檔（支援 .srt / .txt / 任意 UTF-8 文字檔）
3. filename_prefix 非空 → 同 BurnSubtitle 模式，counter 遞增寫到 output/...
   副檔名 auto-detect：含 `-->` 視為 .srt、否則 .txt
"""
import os

from ..utils.output_path import resolve_output_path


def _read_text_with_encoding_detect(path):
    """讀文字檔、自動偵測 encoding。

    為什麼：SRT 多種編碼 — 現代多為 UTF-8 (with/without BOM)；
    舊簡體 Windows 通常 GBK / GB18030；舊繁體 Windows 通常 BIG5；
    部分 Windows 工具會輸出 UTF-16 LE/BE。直接 `open(..., encoding="utf-8")` 在後三者會 UnicodeDecodeError。

    `charset-normalizer` 是純 Python、小、且已經是 `requests` 的 transitive dep（我們 requirements.txt 有 requests），
    所以實質上一定裝得到 — 但 lazy-import 加 fallback 保險，沒裝時退回 UTF-8 + 友善 error。
    """
    try:
        from charset_normalizer import from_path
    except ImportError:
        # Fallback：試 UTF-8，失敗給導引訊息
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError as e:
            raise RuntimeError(
                f"[Convert Chinese] {path} 不是 UTF-8 編碼，自動偵測套件 charset-normalizer 未安裝。"
                "請 `pip install charset-normalizer` 啟用自動偵測，或先把檔案另存為 UTF-8 再丟進來。"
            ) from e

    result = from_path(path).best()
    if result is None:
        raise RuntimeError(
            f"[Convert Chinese] 無法偵測 {path} 的編碼（檔案可能損毀或非文字）。請手動轉成 UTF-8 後重試。"
        )
    detected = result.encoding
    if detected and detected.lower() not in ("utf-8", "utf_8", "ascii"):
        # 非 UTF-8 明確標出 — 讓使用者知道發生了 implicit transcoding
        print(f"[Convert Chinese] 偵測到 {path} 為 {detected} 編碼（已自動以 UTF-8 處理）")
    return str(result)


# 4 個主要 profile — dropdown 展示文字 → opencc config 字串
# s2twp 是台灣使用者最常用：簡 → 繁，含詞庫轉換（「电脑」→「電腦」、「软件」→「軟體」）
PROFILES = {
    "s2twp (簡→繁台灣詞庫)": "s2twp",
    "s2t   (簡→繁通用)":   "s2t",
    "tw2sp (繁台灣→簡)":   "tw2sp",
    "t2s   (繁通用→簡)":   "t2s",
}


class MF_ConvertChinese:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "profile": (list(PROFILES.keys()), {"default": "s2twp (簡→繁台灣詞庫)"}),
                # text widget — 貼上 / wire upstream STRING（例 MF_TranslateSubtitle.translated_srt）
                "text": ("STRING", {"default": "", "multiline": True}),
            },
            "optional": {
                # 沒 text 時用 input_path 讀檔；兩者都沒給 → ValueError
                "input_path": ("STRING", {"default": ""}),
                # 非空 → auto-counter 寫檔到 output/<prefix>_NNNNN.{srt|txt}
                "filename_prefix": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("converted_text", "saved_path")
    FUNCTION = "convert"
    CATEGORY = "MediaForge/Subtitle"

    def convert(self, profile, text, input_path="", filename_prefix=""):
        profile_code = PROFILES[profile]

        # 決定輸入來源：text 有內容用它，否則讀檔
        source_text = text if text.strip() else ""
        if not source_text:
            if not input_path.strip():
                raise ValueError(
                    "[Convert Chinese] text 為空且 input_path 也沒填。"
                    "請在 text 貼上文字、wire 上游 STRING、或填 input_path 指向檔案。"
                )
            if not os.path.exists(input_path):
                raise FileNotFoundError(f"[Convert Chinese] 找不到輸入檔：{input_path}")
            # 自動偵測 encoding — 處理 GBK / BIG5 / UTF-16 等 non-UTF-8 SRT 檔
            source_text = _read_text_with_encoding_detect(input_path)

        # Lazy import — 對齊 translate_subtitle / whisper_transcribe 的 ImportError 友善訊息 pattern
        try:
            from opencc import OpenCC
        except ImportError as e:
            raise RuntimeError(
                "[Convert Chinese] 需要 `opencc-python-reimplemented` 套件，"
                "請 `pip install opencc-python-reimplemented`。"
            ) from e

        converter = OpenCC(profile_code)
        converted = converter.convert(source_text)

        # 可選寫檔
        saved_path = ""
        if filename_prefix.strip():
            # Heuristic：含 `-->` 視為 SRT（標準 SRT timestamp 符號），否則 .txt
            ext = ".srt" if "-->" in converted else ".txt"
            saved_path = resolve_output_path(filename_prefix.strip(), ext)
            with open(saved_path, "w", encoding="utf-8") as f:
                f.write(converted)
            print(f"[Convert Chinese] 寫入: {saved_path}")

        print(f"[Convert Chinese] {profile_code}: {len(source_text)} 字 → {len(converted)} 字")
        return (converted, saved_path)


NODE_CLASS_MAPPINGS = {"MF_ConvertChinese": MF_ConvertChinese}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_ConvertChinese": "🀄 Convert Chinese (OpenCC)"}
