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
class ComposeIR:
    inputs: dict[str, ComposeInput] = field(default_factory=dict)
    ops: list[ComposeOp] = field(default_factory=list)
    _label_counter: int = 0
    main_label: str = ""  # 第一個 video input 的 stream label，op chain 起點
    main_audio_label: str | None = None  # 第一個 video input 的 audio (若有)，Finalize 直接 map
    target_fps: float = 30.0
    target_width: int = 1920
    target_height: int = 1080

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
        new = ComposeIR(
            inputs=dict(self.inputs),
            ops=cloned_ops,
            _label_counter=self._label_counter,
            main_label=self.main_label,
            main_audio_label=self.main_audio_label,
            target_fps=self.target_fps,
            target_width=self.target_width,
            target_height=self.target_height,
        )
        return new

    def to_dict(self) -> dict:
        return {
            "inputs": {
                k: {"source_type": v.source_type, "path": v.path, "index": v.index}
                for k, v in self.inputs.items()
            },
            "ops": [op.__dict__ for op in self.ops],
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
    return path.replace("\\", "/").replace(":", r"\:")


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

    raise NotImplementedError(f"[ComposeIR] 未支援的 op.kind={op.kind!r}")


def compile_ir(ir: ComposeIR) -> tuple[str, str, list[str]]:
    """IR → (filter_complex_script, final_video_label, cleanup_paths).

    final_video_label 是最後一個 op 的 output；若 ops 為空，回到 main_label。
    cleanup_paths 是 compile 過程中產生的 tmp 檔（目前是 drawtext text 檔），
    caller 跑完 ffmpeg 必須 unlink 這些路徑。
    """
    if not ir.main_label:
        raise RuntimeError("[ComposeIR] IR 沒有 main video input — Compose 流程必須從 MF_ComposeStart 開始")

    cleanup_paths: list[str] = []
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
    # 否則 parallel fan-out raise 時 tmpfile 會 leak。
    for op in ir.ops:
        if op.kind == "drawtext":
            fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="mf_drawtext_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(op.params.get("text", "")))
            op.params["_textfile_path"] = tmp
            cleanup_paths.append(tmp)

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
