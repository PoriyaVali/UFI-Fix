"""Draw the TRON (TRX) mark: the angular three-point emblem on a red disc."""
from PIL import Image, ImageDraw

S = 256                     # drawn big, scaled down for clean edges
img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

RED = (236, 9, 40, 255)
WHITE = (255, 255, 255, 255)

d.ellipse([0, 0, S - 1, S - 1], fill=RED)

# the TRON emblem: three corners, drawn as the outer triangle plus the two
# inner folds that give the mark its origami look
def P(x, y):
    return (S * x, S * y)

a = P(0.17, 0.30)           # top left
b = P(0.84, 0.40)           # right
c = P(0.47, 0.83)           # bottom
inner = P(0.43, 0.49)       # the fold point

d.polygon([a, b, c], fill=WHITE)
# cut the folds back out in red so the three facets read separately
d.line([a, inner], fill=RED, width=max(2, S // 42))
d.line([b, inner], fill=RED, width=max(2, S // 42))
d.line([c, inner], fill=RED, width=max(2, S // 42))

for size in (18, 20, 24, 32):
    img.resize((size, size), Image.LANCZOS).save('tron_%d.png' % size)
img.resize((20, 20), Image.LANCZOS).save('tron.png')
img.resize((96, 96), Image.LANCZOS).save('tron_preview.png')
print('tron.png written')
