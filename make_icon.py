# -*- coding: utf-8 -*-
"""生成应用图标 icon.ico（蓝橙渐变底 + 白色柱状图）"""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角矩形遮罩
radius = 56
mask = Image.new("L", (S, S), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=255)

# 纵向渐变：上蓝 #007aff 下橙 #ff9500
top = (0, 122, 255)
bot = (255, 149, 0)
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    r = int(top[0] + (bot[0] - top[0]) * t)
    g = int(top[1] + (bot[1] - top[1]) * t)
    b = int(top[2] + (bot[2] - top[2]) * t)
    gd.line([(0, y), (S, y)], fill=(r, g, b, 255))

img.paste(grad, (0, 0), mask)

# 白色三根柱子（递增），底部对齐
bar_w = 34
gap = 22
base_y = 196
heights = [58, 96, 134]
total = bar_w * 3 + gap * 2
x0 = (S - total) // 2
white = (255, 255, 255, 240)
for i, h in enumerate(heights):
    x = x0 + i * (bar_w + gap)
    d.rounded_rectangle([x, base_y - h, x + bar_w, base_y], radius=10, fill=white)

# 底座线
d.rounded_rectangle([x0 - 6, base_y + 12, x0 + total + 6, base_y + 20], radius=6, fill=white)

img.save("icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon.ico saved")
