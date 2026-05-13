"""MediaForge Compose IR — FFmpeg filter_complex graph compiler.

v2.1 ROADMAP Phase 4 Prerequisite Spike 的承諾物件。

設計目的：把「many overlay ops + single re-encode」做成 IR + compile pass，
避免 user 連 N 個 ComposeOverlay 節點時每個都跑一次 ffmpeg（會 N 次 re-encode、畫質劣化）。

合約（凍結 — Phase 4 後只能加 field，不能改 / 刪 / rename）：

    ComposeIR
    ├── inputs: dict[str, ComposeInput]   全域 input stream 註冊表
    ├── ops: list[ComposeOp]              累積操作
    ├── _label_counter: int               global label allocator
    └── final_label: str | None           Finalize 設定的最終 video label

    ComposeOp = {kind: str, params: dict, depends_on: str, label: str}
    kind 目前支援：'drawtext' / 'overlay' / 'fade' / 'trim'

每個 op 隱含 setpts=PTS-STARTPTS, format=yuv420p, fps normalization；
compile() 自動展開，op author 只需專注於語意。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field

# Plugin root = utils/.. — used to locate the font/ subdirectory shared with burn_subtitle.
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FONT_DIR = os.path.join(_PLUGIN_DIR, "font")

# Common system fonts as fallback when plugin's font/ is empty.
# drawtext on Windows native ffmpeg has no fontconfig — without an explicit fontfile=
# 必爆 "Fontconfig error: Cannot load default config file"。集中在這裡解決。
_SYSTEM_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",                           # Windows
    "C:/Windows/Fonts/segoeui.ttf",                         # Windows Segoe UI
    "/System/Library/Fonts/Helvetica.ttc",                  # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",         # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      # Debian/Ubuntu
    "/usr/share/fonts/TTF/DejaVuSans.ttf",                  # Arch
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",               # Fedora/RHEL
)

_DEFAULT_FONT_CACHE: str | None = None


def _resolve_default_fontfile() -> str | None:
    """找一個 drawtext 能用的字型檔絕對路徑（cross-platform）。

    解析順序：plugin 自帶 font/ 目錄 → 常見 system font 路徑。Cache per-process。
    """
    global _DEFAULT_FONT_CACHE
    if _DEFAULT_FONT_CACHE is not None:
        return _DEFAULT_FONT_CACHE or None

    # 1. plugin font/ 目錄優先（跨平台一致、跟 burn_subtitle 共用）
    if os.path.isdir(_FONT_DIR):
        for fname in sorted(os.listdir(_FONT_DIR)):
            if fname.lower().endswith((".ttf", ".otf", ".ttc")):
                resolved = os.path.join(_FONT_DIR, fname).replace("\\", "/")
                _DEFAULT_FONT_CACHE = resolved
                return resolved

    # 2. system fallback
    for cand in _SYSTEM_FONT_CANDIDATES:
        # Windows path with `/` 也能被 os.path.exists 認得
        cand_native = cand.replace("/", os.sep)
        if os.path.exists(cand_native):
            _DEFAULT_FONT_CACHE = cand
            return cand

    _DEFAULT_FONT_CACHE = ""
    return None


@dataclass
class ComposeInput:
    """全域 input stream entry — 對映 ffmpeg -i 的順序。"""

    source_type: str  # 'video_path' | 'image_path'
    path: str
    index: int  # ffmpeg -i 的 0-based index


@dataclass
class ComposeOp:
    kind: str
    params: dict
    depends_on: str  # 此 op 的 video stream label
    label: str  # 此 op 的 output label
    extra_input: str | None = None  # overlay 類 op 的次要 input label (e.g., watermark image)


@dataclass
class AudioOp:
    """Audio chain operation — 跟 ComposeOp 平行,負責 audio filter graph。

    支援 kinds:
    - 'volume': scale 音量
    - 'amix': 混兩條音軌(keep_source=True) 或純粹用外部音軌(keep_source=False)
    - 'afade': 淡入淡出
    - 'loudnorm': 響度標準化(單 pass)

    compile_audio_chain 依 kind dispatch 到 utils/audio_mix.py 的對應 builder。
    """
    kind: str
    params: dict
    depends_on: str  # 此 op 的 audio stream label (上一個 op 的 output / main_audio_label)
    label: str       # 此 op 的 output label
    extra_audio_input: str | None = None  # amix 第二條 input (BGM)


@dataclass
class ComposeIR:
    inputs: dict[str, ComposeInput] = field(default_factory=dict)
    ops: list[ComposeOp] = field(default_factory=list)
    audio_ops: list[AudioOp] = field(default_factory=list)  # Phase 6: audio chain
    _label_counter: int = 0
    main_label: str = ""  # 第一個 video input 的 stream label，op chain 起點
    main_audio_label: str | None = None  # 第一個 video input 的 audio (若有)，Finalize 直接 map
    target_fps: float = 30.0
    target_width: int = 1920
    target_height: int = 1080
    # 由 caller 注入、需在 Finalize 跑完後 unlink 的 temp 路徑（例如 ComposeStart 把
    # 上游 IMAGE tensor 寫成的 temp .mp4）。compile_ir 會把它 prepend 進 cleanup_paths。
    tmp_paths_to_cleanup: list[str] = field(default_factory=list)

    def alloc_label(self, hint: str = "v") -> str:
        self._label_counter += 1
        return f"{hint}{self._label_counter}"

    def add_video_input(self, path: str, *, is_main: bool = False, has_audio: bool = False) -> str:
        idx = len(self.inputs)
        key = f"in{idx}"
        self.inputs[key] = ComposeInput(source_type="video_path", path=path, index=idx)
        label = f"{idx}:v"
        if is_main:
            self.main_label = label
            if has_audio:
                self.main_audio_label = f"{idx}:a"
        return label

    def add_image_input(self, path: str) -> str:
        idx = len(self.inputs)
        key = f"in{idx}"
        self.inputs[key] = ComposeInput(source_type="image_path", path=path, index=idx)
        return f"{idx}:v"  # image 也用 :v 取 video stream

    def add_audio_input(self, path: str) -> str:
        """External audio file 加進 ffmpeg `-i` list (e.g., BGM .mp3 / temp WAV from AUDIO dict)。
        Returns audio stream label 'N:a' where N = input index."""
        idx = len(self.inputs)
        key = f"in{idx}"
        self.inputs[key] = ComposeInput(source_type="audio_path", path=path, index=idx)
        return f"{idx}:a"

    def append_audio_op(self, kind: str, params: dict, *,
                        depends_on: str | None = None,
                        extra_audio_input: str | None = None) -> str:
        """Append audio op,自動處理 chain head。回傳 op output label。

        depends_on=None → 自動指向上一個 audio op 的 output、或 main_audio_label。
        amix 在 keep_source=False 模式時應顯式傳 depends_on=external_audio_label
        (因為 chain 起點變成外部音源、不從 main_audio_label 出發)。
        """
        src = depends_on if depends_on is not None else self._latest_audio_label()
        out_label = self.alloc_label("a")
        self.audio_ops.append(AudioOp(
            kind=kind, params=params,
            depends_on=src, label=out_label,
            extra_audio_input=extra_audio_input,
        ))
        return out_label

    def _latest_audio_label(self) -> str:
        if self.audio_ops:
            return self.audio_ops[-1].label
        if not self.main_audio_label:
            raise RuntimeError(
                "[ComposeIR] 還沒有 main audio stream — 第一個 audio op 必須是 amix "
                "with keep_source=False (從外部音源引入) 或者 source 影片要自帶音軌。"
            )
        return self.main_audio_label

    def append_op(self, kind: str, params: dict, *, depends_on: str | None = None,
                  extra_input: str | None = None) -> str:
        src = depends_on if depends_on is not None else self._latest_video_label()
        out_label = self.alloc_label("v")
        self.ops.append(ComposeOp(
            kind=kind, params=params,
            depends_on=src, label=out_label,
            extra_input=extra_input,
        ))
        return out_label

    def _latest_video_label(self) -> str:
        # chain 預設取上一個 op output；若還沒 op，從 main input 起步
        if self.ops:
            return self.ops[-1].label
        if not self.main_label:
            raise RuntimeError("[ComposeIR] 還沒有 main video input，先呼叫 add_video_input(is_main=True)")
        return self.main_label

    def clone(self) -> "ComposeIR":
        """Op 節點吃 IR → 吐 IR 時必須 clone，避免 mutate 上游已 cache 的物件。

        R6 P2 fix：op.params 是 dict、ComposeOp(**op.__dict__) 只是 shallow copy ComposeOp，
        params dict 仍 alias。compile_ir 寫入 _textfile_path 會反向污染源 IR；
        並行 Finalize 也會搶相同 tmp file。必須對每個 op.params 做 dict copy 切斷 alias。
        """
        cloned_ops = []
        for op in self.ops:
            new_op = ComposeOp(**op.__dict__)
            new_op.params = dict(op.params)
            cloned_ops.append(new_op)
        cloned_audio_ops = []
        for aop in self.audio_ops:
            new_aop = AudioOp(**aop.__dict__)
            new_aop.params = dict(aop.params)
            cloned_audio_ops.append(new_aop)
        new = ComposeIR(
            inputs=dict(self.inputs),
            ops=cloned_ops,
            audio_ops=cloned_audio_ops,
            _label_counter=self._label_counter,
            main_label=self.main_label,
            main_audio_label=self.main_audio_label,
            target_fps=self.target_fps,
            target_width=self.target_width,
            target_height=self.target_height,
            tmp_paths_to_cleanup=list(self.tmp_paths_to_cleanup),
        )
        return new

    def to_dict(self) -> dict:
        return {
            "inputs": {
                k: {"source_type": v.source_type, "path": v.path, "index": v.index}
                for k, v in self.inputs.items()
            },
            "ops": [op.__dict__ for op in self.ops],
            "audio_ops": [op.__dict__ for op in self.audio_ops],
            "_label_counter": self._label_counter,
            "main_label": self.main_label,
            "main_audio_label": self.main_audio_label,
            "target_fps": self.target_fps,
            "target_width": self.target_width,
            "target_height": self.target_height,
        }


# ----------------------------- Compile pass -----------------------------

# 超過此長度自動切到 -filter_complex_script tempfile
FILTER_SCRIPT_THRESHOLD = 6000


def _count_extra_input_consumers(ir: ComposeIR) -> dict[str, int]:
    """每個 extra_input label 被多少個 overlay op 引用 — split 只對 extra_input 啟用。

    為什麼不對 depends_on 啟用：Compose IR 在語意上是 *linear chain* —
    每個 op 吃上一個 op 的輸出、產生下一個 frame。如果允許 depends_on fan-out
    （兩個 op 都從 main 出發），compile 出來會有 dangling 輸出，FFmpeg 報
    `unconnected output` error。真實「兩個 overlay 在同個 stream」的語意是
    sequential chain (A → B-on-top-of-A)，不是 parallel branch。
    extra_input fan-out 例外：同一張 watermark image 被多個 overlay op 引用是合法的
    （兩個 overlay 用同張 PNG 不同位置）—— 對 extra_input 自動插 split。
    """
    counts: dict[str, int] = {}
    for op in ir.ops:
        if op.extra_input:
            counts[op.extra_input] = counts.get(op.extra_input, 0) + 1
    return counts


def _validate_linear_chain(ir: ComposeIR, norm_label: str) -> None:
    """確認 ops[0..N] 在 depends_on 上嚴格 linear — op[i].depends_on 必須等於上一個 op 的輸出。"""
    if not ir.ops:
        return
    expected = norm_label
    for op in ir.ops:
        if op.depends_on != expected:
            raise RuntimeError(
                f"[ComposeIR] op {op.label} 的 depends_on={op.depends_on!r} 違反 linear chain；"
                f"期待 {expected!r}。Compose IR 要求 depends_on 必須是 *上一個 op 的輸出*"
                f"（或第一個 op 的 normalized main）。兩個 op 不能同時依賴同個 head — "
                "若要多個 overlay，依序串接即可，每個 overlay 都吃上一個的輸出。"
            )
        expected = op.label


def _escape_filter_path(path: str) -> str:
    # 集中走 utils.ffmpeg.escape_filter_path，避免「修了一邊忘了另一邊」。
    # FFmpeg 8.x 需要雙層 escape（`:` → `\\:`），詳見 utils/ffmpeg.py:escape_filter_path。
    from .ffmpeg import escape_filter_path
    return escape_filter_path(path)


def _render_op(op: ComposeOp, label_in: str, label_extra: str | None) -> str:
    """單個 op → filter graph chunk (含 input labels + filter + output label)。"""
    if op.kind == "drawtext":
        p = op.params
        font_arg = ""
        if p.get("fontfile"):
            font_arg = f"fontfile={_escape_filter_path(p['fontfile'])}:"
        # 為什麼用 textfile= 而非 inline text='...'：filtergraph 的 single-quote 字串內
        # 不接受 \' escape，且 % 有 drawtext expansion 衝突。把任意 text 倒到 tmp file 後
        # 用 textfile= 是 FFmpeg 推薦解法（R5 P2 finding）。tmp 路徑由 compile_ir 預先生成
        # 並 inject 到 params['_textfile_path']。
        text_path = p.get("_textfile_path")
        if not text_path:
            raise RuntimeError(
                f"[ComposeIR] drawtext op {op.label} 缺 _textfile_path；compile_ir 未正確 pre-process"
            )
        enable = ""
        if p.get("start_sec") is not None and p.get("end_sec") is not None:
            enable = f":enable='between(t,{float(p['start_sec'])},{float(p['end_sec'])})'"
        return (
            f"[{label_in}]drawtext={font_arg}"
            f"textfile={_escape_filter_path(text_path)}:"
            f"fontsize={int(p.get('fontsize', 36))}:"
            f"fontcolor={p.get('fontcolor', 'white')}:"
            f"x={p.get('x', '(w-text_w)/2')}:y={p.get('y', 'h-text_h-20')}:"
            f"borderw={int(p.get('borderw', 2))}:"
            f"bordercolor={p.get('bordercolor', 'black')}"
            f"{enable}[{op.label}]"
        )

    if op.kind == "overlay":
        if not label_extra:
            raise RuntimeError(f"[ComposeIR] overlay op {op.label} 缺 extra_input")
        p = op.params
        enable = ""
        if p.get("start_sec") is not None and p.get("end_sec") is not None:
            enable = f":enable='between(t,{float(p['start_sec'])},{float(p['end_sec'])})'"
        # overlay 前的 watermark 影像處理鏈：format=rgba → 可選 scale → 可選 alpha 衰減 → 可選 tile
        pre = ["format=rgba"]
        if p.get("scale_w"):
            pre.append(f"scale={int(p['scale_w'])}:-1")
        alpha = p.get("alpha")
        if alpha is not None and float(alpha) < 0.999:
            # colorchannelmixer aa=k 把所有 pixel 的 alpha 通道乘 k；source 已是 rgba
            pre.append(f"colorchannelmixer=aa={float(alpha):.4f}")
        if p.get("tile"):
            # tile=NxM 把單張 watermark 平鋪。為避免接縫，加 setsar=1
            pre.append(f"tile={p['tile']}")
            pre.append("setsar=1")
        chain = [f"[{label_extra}]" + ",".join(pre) + f"[{op.label}_ov]"]
        # eof_action=pass：當 watermark (extra_input) 的 stream 結束（含 -loop 1 image
        # 在 -shortest 看不到結尾的情況），讓 main 繼續通過。沒這個的話 -loop 1 image
        # 會讓 overlay 輸出無限長，撞 -shortest 不到 timeline 死鎖。
        chain.append(
            f"[{label_in}][{op.label}_ov]overlay="
            f"x={p.get('x', '10')}:y={p.get('y', '10')}:eof_action=pass"
            f"{enable}[{op.label}]"
        )
        return ";".join(chain)

    if op.kind == "fade":
        p = op.params
        fade_type = p.get("type", "in")  # 'in' or 'out'
        return (
            f"[{label_in}]fade=t={fade_type}:"
            f"st={float(p.get('start_sec', 0))}:"
            f"d={float(p.get('duration_sec', 1))}[{op.label}]"
        )

    if op.kind == "trim":
        p = op.params
        return (
            f"[{label_in}]trim=start={float(p.get('start_sec', 0))}:"
            f"end={float(p.get('end_sec', 0))},setpts=PTS-STARTPTS[{op.label}]"
        )

    if op.kind == "subtitle":
        # 字幕燒錄 op (from MF_ComposeBurnSubtitle) — 走 utils/ass_style 共用 helper、
        # 跟 MF_BurnSubtitle 字幕渲染邏輯完全一致。
        from .ass_style import build_subtitles_filter
        p = op.params
        sub_filter = build_subtitles_filter(
            srt_path=p["srt_path"],
            style_string=p["style_string"],
            fontsdir=p.get("fontsdir"),  # None → 用 ass_style.FONT_DIR 預設
        )
        return f"[{label_in}]{sub_filter}[{op.label}]"

    raise NotImplementedError(f"[ComposeIR] 未支援的 op.kind={op.kind!r}")


def _render_audio_op(op: AudioOp, label_in: str, label_extra: str | None) -> str:
    """單個 audio op → filter graph chunk (含 input labels + filter + output label)。

    `label_in` = chain 上游 label (對 amix 是 source audio、對其他 op 是上一個 op 輸出)。
    `label_extra` = amix 的第二條 input (BGM)、其他 op kinds 不用。
    """
    from .audio_mix import (
        build_afade_filter,
        build_amix_filter,
        build_loudnorm_filter,
        build_volume_filter,
    )
    p = op.params

    if op.kind == "volume":
        return build_volume_filter(label_in, float(p.get("scale", 1.0)), out_label=op.label)

    if op.kind == "amix":
        keep_source = p.get("keep_source", True)
        if not keep_source:
            # 純外部音源:跳過 amix、用 anull 把外部 label 重命名成 op.label。
            # depends_on 就是外部 label (caller 在 append_audio_op 顯式傳)、extra_audio_input=None。
            return f"[{label_in}]anull[{op.label}]"
        # 混音模式:可選 BGM volume 衰減,常用 0.3 讓 source voice 蓋過
        if not label_extra:
            raise RuntimeError(f"[ComposeIR] amix op {op.label} keep_source=True 需要 extra_audio_input")
        bgm_volume = float(p.get("bgm_volume", 1.0))
        if bgm_volume != 1.0:
            scaled_bgm = f"{op.label}_bgmvol"
            return (
                build_volume_filter(label_extra, bgm_volume, out_label=scaled_bgm) + ";" +
                build_amix_filter(
                    [label_in, scaled_bgm],
                    duration=p.get("duration", "first"),
                    dropout_transition=int(p.get("dropout_transition", 0)),
                    out_label=op.label,
                )
            )
        return build_amix_filter(
            [label_in, label_extra],
            duration=p.get("duration", "first"),
            dropout_transition=int(p.get("dropout_transition", 0)),
            out_label=op.label,
        )

    if op.kind == "afade":
        return build_afade_filter(
            label_in,
            direction=p.get("direction", "in"),
            start_sec=float(p.get("start_sec", 0.0)),
            duration_sec=float(p.get("duration_sec", 2.0)),
            curve=p.get("curve", "tri"),
            out_label=op.label,
        )

    if op.kind == "loudnorm":
        return build_loudnorm_filter(
            label_in,
            target_i=float(p.get("target_i", -16.0)),
            target_tp=float(p.get("target_tp", -1.0)),
            target_lra=float(p.get("target_lra", 11.0)),
            linear=bool(p.get("linear", True)),
            out_label=op.label,
        )

    raise NotImplementedError(f"[ComposeIR] 未支援的 audio op.kind={op.kind!r}")


def compile_audio_chain(ir: ComposeIR) -> tuple[str | None, str | None]:
    """IR.audio_ops → (audio_filter_script, final_audio_label).

    Returns:
        audio_filter_script: ";"-joined filter chunks for audio chain,或 None 表示 no audio_ops
        final_audio_label: 最後一個 audio op 的 output label,或 ir.main_audio_label 表示
            chain 為空、直接用 source audio。可能是 None (source 也沒音軌)。

    跟 compile_ir 分開的設計原因:
    - 音訊 / 視訊在 ffmpeg `-filter_complex` 是獨立 chain,語意上分離
    - compile_ir 既有 caller (test_compose_ir / Compose chain) 不需要動 signature
    - ComposeVideo 把兩個 script 用 ";" join 後傳給 ffmpeg 即可

    Chain 規則 (跟 video 一致、linear):
    - 第一個 op 的 depends_on 通常是 ir.main_audio_label (source audio)
    - amix keep_source=False 例外:第一個 op depends_on=external_audio_label
    - 之後每個 op depends_on 必須是上一個 op 的 output label
    """
    if not ir.audio_ops:
        return None, ir.main_audio_label

    parts = []
    for op in ir.audio_ops:
        parts.append(_render_audio_op(op, op.depends_on, op.extra_audio_input))

    return ";".join(parts), ir.audio_ops[-1].label


def compile_ir(ir: ComposeIR) -> tuple[str, str, list[str]]:
    """IR → (filter_complex_script, final_video_label, cleanup_paths).

    final_video_label 是最後一個 op 的 output；若 ops 為空，回到 main_label。
    cleanup_paths = ir.tmp_paths_to_cleanup (e.g., ComposeStart 為 tensor input
    寫的 temp mp4) + compile 過程產生的 tmp 檔（drawtext text 檔）。
    Caller 跑完 ffmpeg 必須 unlink 這些路徑。
    """
    if not ir.main_label:
        raise RuntimeError("[ComposeIR] IR 沒有 main video input — Compose 流程必須從 MF_ComposeStart 開始")

    # 從 IR 帶入的 tmp paths（caller-injected, 例如 ComposeStart 的 tensor temp）
    cleanup_paths: list[str] = list(ir.tmp_paths_to_cleanup)
    parts: list[str] = []

    # 1. Normalization：main video 套 setpts/fps/format，方便後續 op 不重複煩心
    norm_label = ir.alloc_label("vmain")
    parts.append(
        f"[{ir.main_label}]setpts=PTS-STARTPTS,fps={ir.target_fps},"
        f"scale={ir.target_width}:{ir.target_height}:force_original_aspect_ratio=decrease,"
        f"pad={ir.target_width}:{ir.target_height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,format=yuv420p[{norm_label}]"
    )
    # 把所有 ops 中引用 main_label 的改指向 norm_label
    for op in ir.ops:
        if op.depends_on == ir.main_label:
            op.depends_on = norm_label

    # 2. 驗證 depends_on 是 linear chain — 違反就 raise，避免產出 dangling output
    _validate_linear_chain(ir, norm_label)

    # 2.5 為每個 drawtext op 寫 text tmpfile (R5 P2 fix)。延後到 validate 後才做，
    # 否則 parallel fan-out raise 時 tmpfile 會 leak。同時：drawtext 沒指定 fontfile
    # 在 Windows native ffmpeg 上必爆 (沒裝 fontconfig) — 自動 fallback 到 plugin
    # bundled font 或 system font。找不到才 raise 帶友善訊息。
    for op in ir.ops:
        if op.kind == "drawtext":
            fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="mf_drawtext_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(op.params.get("text", "")))
            op.params["_textfile_path"] = tmp
            cleanup_paths.append(tmp)
            if not op.params.get("fontfile"):
                fallback = _resolve_default_fontfile()
                if fallback:
                    op.params["fontfile"] = fallback
                else:
                    raise RuntimeError(
                        f"[ComposeIR] drawtext op {op.label} 沒指定 fontfile，且系統找不到任何"
                        " fallback 字型檔。請：(1) 在 ComposeOverlayText 節點設 fontfile=絕對路徑，"
                        " 或 (2) 把 .ttf/.otf/.ttc 字型檔放進 plugin 的 font/ 目錄。"
                    )

    # 3. extra_input fan-out split：同一張 image / aux stream 被多個 overlay op 引用就插 split
    consumers = _count_extra_input_consumers(ir)
    rewrite: dict[str, list[str]] = {}
    for label, count in consumers.items():
        if count >= 2:
            split_labels = [ir.alloc_label("vs") for _ in range(count)]
            parts.append(
                f"[{label}]split={count}["
                + "][".join(split_labels)
                + "]"
            )
            rewrite[label] = split_labels

    # 4. 逐 op render — 每個 extra_input consumer 從 rewrite queue 取一個 split label
    rewrite_cursor: dict[str, int] = {k: 0 for k in rewrite}

    def _consume_extra(label: str) -> str:
        if label in rewrite:
            i = rewrite_cursor[label]
            rewrite_cursor[label] += 1
            return rewrite[label][i]
        return label

    for op in ir.ops:
        extra_label = _consume_extra(op.extra_input) if op.extra_input else None
        parts.append(_render_op(op, op.depends_on, extra_label))

    final_video = ir.ops[-1].label if ir.ops else norm_label
    script = ";".join(parts)
    return script, final_video, cleanup_paths


def write_filter_script_if_long(script: str) -> tuple[str, str | None]:
    """超長 filter_complex 寫到 tempfile，回傳 (cli_arg_value, tmpfile_path|None)。"""
    if len(script) <= FILTER_SCRIPT_THRESHOLD:
        return script, None
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="mf_filter_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(script)
    return path, path


def serialize_ir(ir: ComposeIR) -> str:
    """IR → JSON for debugging / inspection 用途。"""
    return json.dumps(ir.to_dict(), ensure_ascii=False, indent=2)
