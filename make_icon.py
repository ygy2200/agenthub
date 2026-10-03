# -*- coding: utf-8 -*-
"""AgentHub 图标生成：hub 拓扑（中心亮节点 + 环绕节点连线）贴"多 agent 大脑"定位。

运行：python make_icon.py
产出：assets/agenthub.ico（多尺寸帧，小尺寸单独画简化版保可辨识）+ assets/preview.png（自查用）。
依赖 Pillow（仅构建期用，运行时不需要）。
"""
from __future__ import annotations

import math
import os
from PIL import Image, ImageDraw, ImageFilter

SS = 4  # 超采样倍数（抗锯齿）

BG_TOP = (28, 40, 92)      # 深夜蓝
BG_BOT = (76, 58, 176)     # 紫青
NODE = (238, 246, 255)     # 节点近白
HUB = (103, 232, 249)      # 中心亮青
LINE = (158, 196, 252)     # 连线淡蓝


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _radial_glow(size: int, cx: float, cy: float, radius: float,
                 color: tuple, max_alpha: int) -> Image.Image:
    """径向渐变光晕图层（中心实、边缘透明），供 alpha_composite。"""
    s = size
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    steps = 22
    for i in range(steps, 0, -1):
        t = i / steps
        r = radius * t
        a = int(max_alpha * (1 - t) ** 2)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color + (a,))
    return glow


def _gradient_bg(size: int) -> Image.Image:
    """垂直渐变 + 圆角方形（圆角约 22%，iOS squircle 观感）。"""
    s = size * SS
    img = Image.new("RGB", (s, s))
    px = img.load()
    for y in range(s):
        c = _lerp(BG_TOP, BG_BOT, y / (s - 1))
        for x in range(s):
            px[x, y] = c
    # 左上柔光（提层次）
    glow = Image.new("L", (s, s), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([s * 0.05, s * 0.02, s * 0.85, s * 0.62], fill=42)
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.12))
    img = Image.composite(Image.new("RGB", (s, s), (96, 130, 220)), img, glow)
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, s - 1, s - 1], radius=round(s * 0.22), fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


def _draw_topology(size: int, simple: bool) -> Image.Image:
    """画 hub 拓扑。size>=48 用完整版；小尺寸 simple=True 只留大结构。"""
    s = size * SS
    img = _gradient_bg(size).resize((s, s), Image.LANCZOS)
    d = ImageDraw.Draw(img, "RGBA")
    cx = cy = s / 2
    r_orbit = s * (0.30 if simple else 0.30)
    n = 6
    # 连线（先画，被节点压住）
    for i in range(n):
        ang = math.pi / 2 + i * 2 * math.pi / n
        x2, y2 = cx + r_orbit * math.cos(ang), cy - r_orbit * math.sin(ang)
        w = max(2, round(s * 0.014))
        d.line([cx, cy, x2, y2], fill=LINE + (210,), width=w)
    # 中心径向光晕（真渐变图层，杜绝半透明叠脏色）
    if not simple:
        img = Image.alpha_composite(img, _radial_glow(s, cx, cy, s * 0.30, HUB, 130))
        d = ImageDraw.Draw(img, "RGBA")
    r_hub = s * (0.115 if simple else 0.105)
    d.ellipse([cx - r_hub, cy - r_hub, cx + r_hub, cy + r_hub], fill=HUB + (255,))
    r_hub_in = r_hub * 0.52
    d.ellipse([cx - r_hub_in, cy - r_hub_in, cx + r_hub_in, cy + r_hub_in], fill=(244, 254, 255, 255))
    # 环绕节点
    r_node = s * (0.075 if simple else 0.062)
    for i in range(n):
        ang = math.pi / 2 + i * 2 * math.pi / n
        x, y = cx + r_orbit * math.cos(ang), cy - r_orbit * math.sin(ang)
        d.ellipse([x - r_node, y - r_node, x + r_node, y + r_node], fill=NODE + (255,))
        if not simple:
            # 每个环绕节点一枚小卫星点（"每个 agent 还有自己的工具"的暗示）
            ang2 = ang + math.pi / 3
            x2, y2 = x + r_node * 2.1 * math.cos(ang2), y - r_node * 2.1 * math.sin(ang2)
            r_sat = r_node * 0.32
            d.ellipse([x2 - r_sat, y2 - r_sat, x2 + r_sat, y2 + r_sat], fill=HUB + (220,))
            d.line([x, y, x2, y2], fill=LINE + (140,), width=max(1, round(s * 0.006)))
    return img.resize((size, size), Image.LANCZOS)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)
    big = {s: _draw_topology(s, simple=False) for s in (256, 128, 64, 48)}
    small = {s: _draw_topology(s, simple=True) for s in (32, 24, 16)}
    frames = [big[256], big[128], big[64], big[48], small[32], small[24], small[16]]
    ico = os.path.join(out_dir, "agenthub.ico")
    frames[0].save(ico, format="ICO", append_images=frames[1:],
                   sizes=[(f.width, f.height) for f in frames])
    big[256].save(os.path.join(out_dir, "preview.png"))
    # 回读验证帧
    with Image.open(ico) as ico_img:
        sizes = sorted(ico_img.info.get("sizes") or [], reverse=True)
    print("ICO 生成:", ico, "帧:", sizes)


if __name__ == "__main__":
    main()
