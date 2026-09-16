"""Draw the UFI-Fix icon: a memory chip with a green check, on a blue tile."""
from PIL import Image, ImageDraw

S = 512
img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# tile with a vertical gradient
top, bottom = (59, 130, 246), (29, 64, 175)
grad = Image.new('RGBA', (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
mask = Image.new('L', (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
img.paste(grad, (0, 0), mask)

# chip body
pad = int(S * 0.27)
body = [pad, pad, S - pad, S - pad]
d.rounded_rectangle(body, radius=int(S * 0.045), fill=(255, 255, 255, 235))
inner = int(S * 0.055)
d.rounded_rectangle([body[0] + inner, body[1] + inner, body[2] - inner, body[3] - inner],
                    radius=int(S * 0.025), outline=(29, 64, 175, 255), width=int(S * 0.018))

# pins
pin_len = int(S * 0.075)
pin_w = int(S * 0.045)
gap = (body[2] - body[0] - 3 * pin_w) / 4.0
for i in range(3):
    off = body[0] + gap * (i + 1) + pin_w * i
    d.rounded_rectangle([off, body[1] - pin_len, off + pin_w, body[1] + 4],
                        radius=pin_w // 3, fill=(255, 255, 255, 235))
    d.rounded_rectangle([off, body[3] - 4, off + pin_w, body[3] + pin_len],
                        radius=pin_w // 3, fill=(255, 255, 255, 235))
    d.rounded_rectangle([body[0] - pin_len, off, body[0] + 4, off + pin_w],
                        radius=pin_w // 3, fill=(255, 255, 255, 235))
    d.rounded_rectangle([body[2] - 4, off, body[2] + pin_len, off + pin_w],
                        radius=pin_w // 3, fill=(255, 255, 255, 235))

# green check badge
r = int(S * 0.20)
cx, cy = S - r - int(S * 0.05), S - r - int(S * 0.05)
d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(34, 197, 94, 255),
          outline=(255, 255, 255, 255), width=int(S * 0.022))
w = int(S * 0.045)
d.line([(cx - r * 0.42, cy + r * 0.02), (cx - r * 0.08, cy + r * 0.36),
        (cx + r * 0.46, cy - r * 0.38)], fill=(255, 255, 255, 255), width=w, joint='curve')

img.save('icon.ico', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
img.resize((256, 256), Image.LANCZOS).save('icon_preview.png')
print('icon.ico written')
