"""Icons of the Android apps (phone and TV) and of the macOS app, made from the logo
(static/icons/icon-512.png, logo on a transparent background). To run again when the logo changes:

    python3 android/make_icons.py

Android 8+: adaptive icon (plain background + separate logo): the launcher cuts it to its shape (round, rounded square…)
without surrounding it with white. The logo fits in the always-visible area (66 dp out of 108). A single-colour
version is used for Android 13 themed icons.
"""
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO = os.path.join(ROOT, "static", "icons", "icon-512.png")
BG = (11, 11, 14, 255)  # Noxa background
DENSITIES = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}


def logo():
    im = Image.open(LOGO).convert("RGBA")
    return im.crop(im.split()[3].getbbox())


def fit(im, box):
    """The logo shrunk to fit in a square of `box` pixels (proportions kept)."""
    s = box / max(im.size)
    return im.resize((max(1, round(im.size[0] * s)), max(1, round(im.size[1] * s))), Image.LANCZOS)


def centered(size, im):
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.alpha_composite(im, ((size - im.size[0]) // 2, (size - im.size[1]) // 2))
    return out


def android(module):
    res = os.path.join(ROOT, "android", module, "src", "main", "res")
    src = logo()
    for d, k in DENSITIES.items():
        folder = os.path.join(res, f"mipmap-{d}")
        os.makedirs(folder, exist_ok=True)
        size = round(108 * k)
        fg = centered(size, fit(src, round(size * 0.5)))  # 54 dp out of 108: margin within the 66 dp visible area
        fg.save(os.path.join(folder, "ic_launcher_foreground.png"), optimize=True)
        mono = Image.new("RGBA", fg.size, (255, 255, 255, 0))
        mono.putalpha(fg.split()[3].point(lambda a: 255 if a > 90 else 0))
        mono.save(os.path.join(folder, "ic_launcher_monochrome.png"), optimize=True)
        # icon from before Android 8 (and fallback): full square, no transparency around
        legacy = Image.new("RGBA", (round(48 * k),) * 2, BG)
        lg = fit(src, round(48 * k * 0.66))
        legacy.alpha_composite(lg, ((legacy.size[0] - lg.size[0]) // 2, (legacy.size[1] - lg.size[1]) // 2))
        legacy.save(os.path.join(folder, "ic_launcher.png"), optimize=True)
    any_dpi = os.path.join(res, "mipmap-anydpi-v26")
    os.makedirs(any_dpi, exist_ok=True)
    with open(os.path.join(any_dpi, "ic_launcher.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n'
                '<!-- icône adaptative : fond uni + logo (android/make_icons.py) -->\n'
                '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <background android:drawable="@color/ic_launcher_background" />\n'
                '    <foreground android:drawable="@mipmap/ic_launcher_foreground" />\n'
                '    <monochrome android:drawable="@mipmap/ic_launcher_monochrome" />\n'
                '</adaptive-icon>\n')
    values = os.path.join(res, "values")
    os.makedirs(values, exist_ok=True)
    with open(os.path.join(values, "ic_launcher_background.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
                '    <color name="ic_launcher_background">#0B0B0E</color>\n</resources>\n')


def macos():
    """macOS: rounded square the size of Apple's icons (824 px in 1024), the logo in the centre."""
    size, square = 1024, 824
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (square, square), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, square - 1, square - 1), radius=185, fill=255)
    tile = Image.new("RGBA", (square, square), BG)
    tile.putalpha(mask)
    out.alpha_composite(tile, ((size - square) // 2, (size - square) // 2))
    lg = fit(logo(), round(square * 0.62))
    out.alpha_composite(lg, ((size - lg.size[0]) // 2, (size - lg.size[1]) // 2))
    folder = os.path.join(ROOT, "apps", "desktop", "assets")
    os.makedirs(folder, exist_ok=True)
    out.save(os.path.join(folder, "icon-mac.png"), optimize=True)


def tv_banner():
    """Android TV home screen banner (320 x 180, xhdpi): the logo with its glow, and the name."""
    w, h = 320, 180
    out = Image.new("RGBA", (w, h), BG)
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((14, 38, 130, 142), fill=(255, 140, 30, 90))
    out = Image.alpha_composite(out, glow.filter(ImageFilter.GaussianBlur(22)))
    out.alpha_composite(fit(logo(), 96), (24, 42))
    font = ImageFont.truetype(os.path.join(ROOT, "static", "fonts", "outfit.woff"), 64)
    font.set_variation_by_name("Bold")  # variable font: the weight of the interface titles
    ImageDraw.Draw(out).text((132, h // 2), "Noxa", font=font, fill=(255, 255, 255, 255), anchor="lm")
    folder = os.path.join(ROOT, "android", "app", "src", "main", "res", "drawable-xhdpi")
    out.convert("RGB").save(os.path.join(folder, "banner.png"), optimize=True)


if __name__ == "__main__":
    android("mobile")
    android("app")
    macos()
    tv_banner()
    print("icons written")
