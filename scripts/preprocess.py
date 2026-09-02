"""AIS photo -> model-ready input: background removed, square-padded PNG.

Usage: python scripts/preprocess.py testdata/raw/*.jpg -o testdata/
Requires: pip install rembg onnxruntime pillow
"""
import argparse
from pathlib import Path

from PIL import Image
from rembg import remove

# community-contributed photos flow through here — cap decode size so an
# oversized upload can't exhaust worker memory
Image.MAX_IMAGE_PIXELS = 40_000_000


def preprocess(src: Path, outdir: Path, size: int = 1024) -> Path:
    raw = Image.open(src)
    raw.thumbnail((2048, 2048))
    img = remove(raw).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    # square-pad with 5% margin
    side = int(max(img.size) * 1.1)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    canvas = canvas.resize((size, size), Image.LANCZOS)
    out = outdir / f"{src.stem}.png"
    canvas.save(out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("-o", "--outdir", type=Path, default=Path("testdata"))
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    for src in args.images:
        print(preprocess(src, args.outdir))
