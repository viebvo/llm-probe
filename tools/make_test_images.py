"""Generate the synthetic OCR test images in llm_probe/assets/images (receipt.jpg and a note per language).

Not needed at runtime - the images are part of the repository. The texts are the ground truth of the built-in
OCR prompts (llm_probe/prompts.py), so regenerate the images whenever you change them.

Needs Pillow and numpy plus two fonts:
    python tools/make_test_images.py --mono DejaVuSansMono.ttf --hand Caveat.ttf
(DejaVu Sans Mono: Bitstream Vera license; Caveat: SIL Open Font License, from github.com/google/fonts.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_probe.prompts import IMAGES_ASSETS, NOTES, RECEIPT_TEXT  # noqa: E402


def perspective_coeffs(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> list[float]:
    """Coefficients for Image.transform(PERSPECTIVE) that map dst corners back to src corners."""
    rows = []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    return np.linalg.solve(np.array(rows, float), np.array(src, float).reshape(8)).tolist()


def photo(paper: Image.Image, size: tuple[int, int], corners: list[tuple[float, float]], seed: int,
          background: tuple[int, int, int], light: float) -> Image.Image:
    """Lay the paper on a textured surface, photographed at an angle with uneven light, blur and noise."""
    rng = np.random.default_rng(seed)
    w, h = size
    # surface: coloured noise, smoothed into a wood/fabric-like texture
    tex = rng.normal(0, 18, (h // 4, w // 4, 1)) + np.array(background)
    surface = Image.fromarray(np.clip(tex, 0, 255).astype("uint8")).resize((w, h), Image.BICUBIC)
    surface = surface.filter(ImageFilter.GaussianBlur(3))
    pw, ph = paper.size
    coeffs = perspective_coeffs([(0, 0), (pw, 0), (pw, ph), (0, ph)], corners)
    warped = paper.transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    mask = Image.new("L", paper.size, 255).transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    # soft shadow of the paper on the surface
    shadow = mask.filter(ImageFilter.GaussianBlur(14))
    surface = Image.composite(Image.new("RGB", (w, h), (20, 18, 16)), surface,
                              shadow.point(lambda v: int(v * 0.55)))
    img = Image.composite(warped, surface, mask)
    # uneven light: bright spot top left, darker towards the bottom right
    yy, xx = np.mgrid[0:h, 0:w]
    gain = 1.08 - light * np.hypot(xx / w - 0.2, yy / h - 0.15)
    arr = np.asarray(img, float) * gain[..., None]
    img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8")).filter(ImageFilter.GaussianBlur(1.1))
    arr = np.asarray(img, float) + rng.normal(0, 7, (h, w, 1))
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))


def receipt(mono: str, out: Path) -> None:
    lines = RECEIPT_TEXT.splitlines()
    font = ImageFont.truetype(mono, 30)
    lh, cw = 40, font.getlength("M")
    width = int(cw * max(len(line) for line in lines)) + 80
    paper = Image.new("RGB", (width, lh * len(lines) + 120), (243, 240, 230))
    d = ImageDraw.Draw(paper)
    rng = np.random.default_rng(7)
    for i, line in enumerate(lines):
        fade = int(rng.uniform(20, 95))  # thermal print: some rows lighter than others
        d.text((40, 60 + i * lh), line, font=font, fill=(fade, fade, fade + 10))
    # crumpled paper: a few soft diagonal creases
    arr = np.asarray(paper, float)
    yy, xx = np.mgrid[0:paper.height, 0:paper.width]
    for k in range(4):
        a, b = rng.uniform(-0.6, 0.6), rng.uniform(0, paper.height)
        dist = np.abs(yy - (a * xx + b))
        arr -= (18 * np.exp(-dist / 6))[..., None]
    paper = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
    pw, ph = paper.size
    s = min(1380 / ph, 900 / pw)
    w2, h2 = pw * s / 2, ph * s / 2
    # rotated by 5 degrees; the top edge is farther away from the camera (narrower)
    a = np.radians(-5)
    quad = [(-w2 * 0.92, -h2), (w2 * 0.92, -h2), (w2, h2), (-w2, h2)]
    corners = [(550 + x * np.cos(a) - y * np.sin(a), 825 + x * np.sin(a) + y * np.cos(a)) for x, y in quad]
    photo(paper, (1100, 1650), corners, 3, (105, 72, 45), 0.55).save(out, quality=72)


def note(hand: str, out: Path, text: str, seed: int) -> None:
    lines = text.splitlines()
    rng = np.random.default_rng(seed)
    paper = Image.new("RGB", (1000, 120 + 92 * len(lines)), (250, 247, 225))
    d = ImageDraw.Draw(paper)
    for i in range(len(lines) + 1):  # ruled paper
        y = 112 + i * 92
        d.line([(0, y), (1000, y)], fill=(160, 190, 220), width=2)
    d.line([(90, 0), (90, paper.height)], fill=(225, 140, 140), width=3)
    for i, line in enumerate(lines):
        # handwriting: every line slightly shifted, sized and tilted
        f = ImageFont.truetype(hand, int(64 * rng.uniform(0.92, 1.06)))
        layer = Image.new("L", (1000, 140), 0)
        ImageDraw.Draw(layer).text((110 + rng.uniform(-8, 14), 30), line, font=f, fill=255)
        layer = layer.rotate(rng.uniform(-1.8, 1.8), center=(500, 70), resample=Image.BICUBIC)
        ink = Image.new("RGB", layer.size, (30, 45, 120))
        paper.paste(ink, (0, int(i * 92 + 18 + rng.uniform(-6, 6))), layer)
    pw, ph = paper.size
    corners = [(130, 170), (1180, 90), (1240, 90 + ph * 1.05), (80, 160 + ph * 1.0)]
    photo(paper, (1400, ph + 360), corners, seed - 6, (70, 85, 95), 0.7).save(out, quality=70)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mono", required=True, help="monospaced TTF for the receipt (e.g. DejaVuSansMono.ttf)")
    ap.add_argument("--hand", required=True, help="handwriting TTF for the note (e.g. Caveat.ttf)")
    ap.add_argument("--out", type=Path, default=IMAGES_ASSETS)
    ap.add_argument("--only", nargs="*", default=[], help="file names to generate (default: all)")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if not a.only or "receipt.jpg" in a.only:
        receipt(a.mono, a.out / "receipt.jpg")
    # the seeds of the existing notes keep their images the same; a new language gets its own
    seeds = {"de": 11, "pl": 23, "en": 37}
    for lang, (name, text) in NOTES.items():
        if not a.only or name in a.only:
            note(a.hand, a.out / name, text, seeds.get(lang, 41 + len(seeds)))
    print(f"written to {a.out}")


if __name__ == "__main__":
    main()
