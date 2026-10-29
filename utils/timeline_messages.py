"""Timeline 節點的使用者訊息目錄。

每則訊息有穩定 key（README Troubleshooting 與測試都以 key 對應，不比對整句），
格式統一為 `[<節點名稱>] <KEY>：<訊息>`，節點名稱不含 emoji——cp950 / cp1252 主控台編不出
emoji，print() 會直接丟 UnicodeEncodeError，讓「警告後繼續」變成節點失敗。段號對使用者從 1 起算。
"""

MESSAGES = {
    "E_SEQUENCE": "預期第 {expected} 段，收到第 {actual} 段。請連接前一段的 state。",
    "E_PLAN": "分段與 state 屬於不同計畫。請重新執行整個迴圈。",
    "E_SEGMENT_INDEX": "segment_index 超出範圍：計畫共 {count} 段（0 起算），收到 {index}。",
    "E_PROVENANCE": "續接標記與本段不符（{field}）。請檢查分段接線。",
    "W_PROVENANCE": "缺少續接標記；將繼續執行。請檢查分段與 guide 接線。",
    "E_CARRY_SOURCE": "本段需要續接資料。請連接 state 或 previous_latent（擇一）。",
    "E_CARRY_DRIFT": "續接區在取樣後被改變（最大差 {drift}）。兩段式取樣（例如 Wan 2.2 high / low）"
                     "請在第二個 sampler 前再接一次 Extend Latent，on_conflict 設 carry_wins。",
    "E_CAUSAL": "此續接模式仍在實驗階段。確認後開啟 experimental_causal。",
    "E_SPILL_MISSING": "分段暫存檔已不存在。請重跑整個迴圈，再組裝。",
    "E_RAM": "本段估計需 {gib:.2f} GiB RAM，超過預檢門檻。請縮短段長或降低解析度。",
    "E_DISK": "暫存空間不足，需至少 {gib:.2f} GiB。請釋出空間後重試。",
    "E_SOURCE_CHANGED": "來源影片已變更，請重新執行 Edit Timeline。",
    "E_SOURCE_SHORTFALL": "來源影片不足以涵蓋第 {segment} 段。請改用 hold_last / black 或調整時間框。",
    "E_TOKENS": "latent 時間長度不符：預期 {expected}，收到 {actual}。請檢查 grid、VACE reference 與 guide 接線。",
    "E_H3_FPS": "H3 grid 僅支援 24 fps。請將 fps 設為 24。",
    "E_TIMELINE_JSON": "時間軸 JSON 無效：{reason}。請修正 {location}。",
    "E_MASK_CONFLICT": "續接區與既有遮罩衝突。請調整接線，或選擇 carry_wins。",
    "E_AUDIO_CARRY": "此 latent 不支援音訊續接。請關閉 carry_audio。",
    "E_AUDIO_LAYOUT": "各段音訊的有無、取樣率或聲道必須一致。請在上游統一音訊格式。",
    "W_AUDIO_PAD": "已在接縫或結尾補入 {samples} 個靜音樣本。",
    "W_PROXY": "預覽建立失敗；時間軸仍可編輯。請重試。",
    "W_SOURCE_TIME_UNKNOWN": "無法取得來源的起始時間，暫以 0 計算；預覽與來源時間可能略有偏移。",
    "E_MATERIAL": "素材「{name}」無法使用：{reason}。請確認檔案在 input/ 內且格式正確。",
    "E_REF": "參考 {ref} 無效：{reason}。請在時間軸上調整。",
    "E_ANCHOR": "錨定 {anchor} 無效：{reason}。請在時間軸上調整。",
    "E_ANCHOR_OVERLAP": "{stream} 錨定 {first} 與 {second} 的時間重疊。請把它們錯開。",
    "E_REF_LIMIT": "第 {segment} 段的 {kind} 有 {count} 個，超過 H3 上限 {limit}。請縮小參考範圍或移到其他段。",
    "E_REF_SHORT": "第 {segment} 段的參考影片「{name}」只有 {frames} 幀（至少 5 幀）。請延長參考或移出本段。",
    "E_PROMPT_REF": "第 {segment} 段 prompt 的素材引用無效：{reason}。",
    "E_H3_NODES": "找不到可用的 core MiniMax H3 節點（{nodes}）。請更新 ComfyUI。",
    "E_MEDIA_MISSING": "第 {segment} 段沒有第 {ordinal} 個 {kind}（本段共 {count} 個）。請調整 ordinal。",
    "E_LOCK_DRIFT": "鎖定原音檢查失敗（{reason}）。請確認 sampler 保留 noise_mask 與 latent 的 mf_lock，或改用完整 schedule。",
    "E_VIEW": "segment 格式不符：{reason}。",
    "W_LOCK_CARRY": "第 {segment} 段的鎖定原音落在續接區，略過本段音訊續接（video 續接照常）。",
}


def format_message(node_label, key, **fields):
    return f"[{node_label}] {key}：{MESSAGES[key].format(**fields)}"


class TimelineError(ValueError):
    """帶穩定 key 的 ValueError；`str(err)` 就是完整的使用者訊息。"""

    def __init__(self, node_label, key, **fields):
        self.key = key
        self.fields = fields
        super().__init__(format_message(node_label, key, **fields))
