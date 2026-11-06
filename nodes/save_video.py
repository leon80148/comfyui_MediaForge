"""MF_SaveVideo — 把 VIDEO 存成 H.264 MP4（有音訊時編成 AAC），解碼器、色彩轉換與編碼器各只用一條執行緒。

MediaForge 第一個 V3 節點：hidden prompt / extra_pnginfo 與 PreviewVideo 走 comfy_api.latest，V3 的 base class
讓 comfy_api 必須在 module top import。匯出邏輯在 utils/serial_h264.py，不需要 ComfyUI 也能測。
"""
import json

from comfy_api.latest import io, ui

from ..utils import serial_h264
from ..utils.video_batch import check_interrupt


class MF_SaveVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MF_SaveVideo",
            display_name="💾 Save Video · Serial H.264",
            category="MediaForge/Video",
            description="把 VIDEO 存成 H.264 MP4，有音訊時編成 AAC。讀檔時的解碼器、RGB→YUV 轉換、H.264 與 AAC 編碼器各只用一條執行緒；"
                        "每次執行寫進獨立的 <filename_prefix>-<識別碼>/video.mp4。",
            inputs=[
                io.Video.Input("video", tooltip="要存的影片。"),
                io.String.Input("filename_prefix", default="MediaForge/serial_video",
                                tooltip="output 目錄內的前綴，可含子資料夾。每次執行建立 <前綴>-<識別碼>/video.mp4。"),
            ],
            outputs=[
                io.Video.Output("video", tooltip="原本的輸入物件，未變更。"),
                io.String.Output("video_path", tooltip="完成的 MP4 絕對路徑。"),
                io.String.Output("report_json", tooltip="匯出報告（JSON）。"),
            ],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, video, filename_prefix) -> io.NodeOutput:
        import folder_paths
        from comfy.cli_args import args

        if args.disable_metadata:
            tags, metadata = {}, {"embedded": False, "reason": "--disable-metadata"}
        else:
            tags, replaced = serial_h264.metadata_tags(cls.hidden.extra_pnginfo, cls.hidden.prompt)
            metadata = {"embedded": bool(tags), "tags": list(tags), "nonfinite_replaced_with_null": replaced}
        check_interrupt()
        components, source = serial_h264.read_source(video)
        path, subfolder, report = serial_h264.export(
            components, bit_depth=video.get_bit_depth(), color_space=video.get_color_space(),
            output_root=folder_paths.get_output_directory(), filename_prefix=filename_prefix, tags=tags,
            check_interrupt=check_interrupt,
        )
        report["source"] = source
        report["metadata"] = metadata
        print(f"[{serial_h264.LABEL}] 輸出成功: {path}")
        preview = ui.PreviewVideo([ui.SavedResult(serial_h264.FINAL_NAME, subfolder, io.FolderType.output)])
        return io.NodeOutput(video, path, json.dumps(report, ensure_ascii=False), ui=preview)


NODE_CLASS_MAPPINGS = {"MF_SaveVideo": MF_SaveVideo}
NODE_DISPLAY_NAME_MAPPINGS = {"MF_SaveVideo": "💾 Save Video · Serial H.264"}
