def hex_to_ass_color(hex_color, alpha="00"):
    """將 #RRGGBB 轉換為 ASS 的 &H[Alpha]BBGGRR&，支援透明度 (00為完全不透明, FF為完全透明)"""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 6:
        r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
        return f"&H{alpha}{b}{g}{r}&"
    return f"&H{alpha}FFFFFF&"
