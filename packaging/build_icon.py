#!/usr/bin/env python3
"""Build PhotoCurator's approved aurora + iOS-glass C icon."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

HERE = Path(__file__).resolve().parent
SIZE = 1024

def lerp(a, b, t):
    return int(a + (b-a)*t)

img = Image.new("RGBA", (SIZE, SIZE), (238, 247, 255, 255))
px = img.load()
for y in range(SIZE):
    for x in range(SIZE):
        nx, ny = x/(SIZE-1), y/(SIZE-1)
        # luminous aurora base: cyan upper-left, violet center, pink lower-right
        c1 = (188, 232, 255)
        c2 = (181, 178, 255)
        c3 = (255, 200, 237)
        t = (nx + ny) / 2
        if t < .55:
            u=t/.55; c=tuple(lerp(c1[i], c2[i], u) for i in range(3))
        else:
            u=(t-.55)/.45; c=tuple(lerp(c2[i], c3[i], u) for i in range(3))
        px[x,y]=(*c,255)

glow = Image.new("RGBA", img.size, (0,0,0,0))
gd = ImageDraw.Draw(glow)
for cx,cy,r,c in [
    (170,210,300,(60,229,255,115)),
    (830,180,320,(134,110,255,100)),
    (650,820,360,(255,115,210,90)),
]:
    gd.ellipse((cx-r,cy-r,cx+r,cy+r), fill=c)
glow = glow.filter(ImageFilter.GaussianBlur(95))
img = Image.alpha_composite(img, glow)

# Main glass tile
glass = Image.new("RGBA", img.size, (0,0,0,0))
d = ImageDraw.Draw(glass)
box=(72,72,952,952)
d.rounded_rectangle(box, radius=190, fill=(255,255,255,62), outline=(255,255,255,205), width=8)
d.rounded_rectangle((86,86,938,938), radius=176, outline=(170,215,255,100), width=4)
glass = glass.filter(ImageFilter.GaussianBlur(.5))
img = Image.alpha_composite(img, glass)

# Photo-card motif
cards = Image.new("RGBA", img.size, (0,0,0,0))
cd = ImageDraw.Draw(cards)
for i,(xy,fill) in enumerate([
    ((220,305,770,735),(255,255,255,66)),
    ((285,365,825,780),(255,255,255,82)),
]):
    cd.rounded_rectangle(xy, radius=55, fill=fill, outline=(255,255,255,175), width=5)
# mountain + sun on front card
cd.polygon([(365,690),(520,525),(635,630),(720,550),(820,735),(285,735)], fill=(235,244,255,110))
cd.ellipse((685,430,752,497), fill=(255,224,247,190))
img = Image.alpha_composite(img, cards)

# Glossy C with layered strokes
letter = Image.new("RGBA", img.size, (0,0,0,0))
ld = ImageDraw.Draw(letter)
font_path = Path(r"C:\Windows\Fonts\seguisb.ttf")
if not font_path.exists():
    font_path = Path(r"C:\Windows\Fonts\segoeuib.ttf")
font = ImageFont.truetype(str(font_path), 600) if font_path.exists() else ImageFont.load_default()
bbox = ld.textbbox((0,0), "C", font=font, stroke_width=0)
tw,th=bbox[2]-bbox[0],bbox[3]-bbox[1]
pos=((SIZE-tw)//2-2,(SIZE-th)//2-42)
# soft shadow
ld.text((pos[0]+16,pos[1]+24),"C",font=font,fill=(64,75,180,70),stroke_width=22,stroke_fill=(64,75,180,35))
letter=letter.filter(ImageFilter.GaussianBlur(9))
img=Image.alpha_composite(img,letter)

letter = Image.new("RGBA", img.size, (0,0,0,0)); ld=ImageDraw.Draw(letter)
# iridescent body built from vertical masked gradient
mask=Image.new("L",img.size,0); md=ImageDraw.Draw(mask)
md.text(pos,"C",font=font,fill=255,stroke_width=14,stroke_fill=255)
grad=Image.new("RGBA",img.size,(0,0,0,0)); gp=grad.load()
stops=[(0.0,(91,218,255)),(.45,(93,126,255)),(.72,(188,111,255)),(1.0,(255,155,220))]
for y in range(SIZE):
    t=y/(SIZE-1)
    for j in range(len(stops)-1):
        if stops[j][0] <= t <= stops[j+1][0]:
            a,ca=stops[j]; b,cb=stops[j+1]; u=(t-a)/(b-a)
            col=tuple(lerp(ca[k],cb[k],u) for k in range(3)); break
    else: col=stops[-1][1]
    for x in range(SIZE): gp[x,y]=(*col,255)
letter=Image.composite(grad,letter,mask)
img=Image.alpha_composite(img,letter)
# glass rim and highlight drawn separately so the gradient body remains intact
rim=Image.new("RGBA",img.size,(0,0,0,0)); rd=ImageDraw.Draw(rim)
rd.text(pos,"C",font=font,fill=(0,0,0,0),stroke_width=12,stroke_fill=(255,255,255,170))
highlight=Image.new("RGBA",img.size,(0,0,0,0)); hd=ImageDraw.Draw(highlight)
hd.text((pos[0]-5,pos[1]-10),"C",font=font,fill=(255,255,255,24),stroke_width=4,stroke_fill=(255,255,255,120))
img=Image.alpha_composite(img,rim)
img=Image.alpha_composite(img,highlight)

# Soft edge vignette/highlight
overlay=Image.new("RGBA",img.size,(0,0,0,0)); od=ImageDraw.Draw(overlay)
od.arc((95,95,930,930),190,350,fill=(255,255,255,190),width=8)
img=Image.alpha_composite(img,overlay)

# Transparent rounded outer silhouette. Windows taskbar/shortcut icons now
# keep the aurora glass tile but do not render an opaque square around it.
outer = Image.new("L", (SIZE, SIZE), 0)
od = ImageDraw.Draw(outer)
od.rounded_rectangle((38, 38, SIZE - 38, SIZE - 38), radius=220, fill=255)
soft = outer.filter(ImageFilter.GaussianBlur(1.2))
img.putalpha(ImageChops.multiply(img.getchannel("A"), soft))

png = HERE / "PhotoCurator.png"
ico = HERE / "PhotoCurator.ico"
img.resize((512,512), Image.Resampling.LANCZOS).save(png, optimize=True)
img.save(
    ico,
    format="ICO",
    sizes=[(16,16),(20,20),(24,24),(32,32),(40,40),(48,48),(64,64),(128,128),(256,256)],
)
print(ico)
