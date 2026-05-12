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
            with open(input_path, "r", encoding="utf-8") as f:
                source_text = f.read()

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
