"""Prepare Canva photo strips and render three shots in top-to-bottom order."""

import argparse
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageChops, ImageOps


TEMPLATE_ROOT = Path(__file__).resolve().parent / "Template"
READY = TEMPLATE_ROOT / "Ready"
# Rows in the empty gutters of the original Canva exports.
CUTS = {"2": (3000, 4900), "3": (2980, 4780)}


def prepare(page: str, source: Path = TEMPLATE_ROOT) -> Path:
    cuts = CUTS[page]
    with ZipFile(source / "Black.zip") as black, ZipFile(source / "White.zip") as white:
        black_png, white_png = (archive.read(f"{page}.png") for archive in (black, white))
    with Image.open(BytesIO(black_png)) as black, Image.open(BytesIO(white_png)) as white:
        if black.size != white.size or black.mode != "RGB" or white.mode != "RGB":
            raise ValueError("Use matching, opaque RGB PNG exports.")
        if any(high for _, high in ImageChops.subtract(black, white).getextrema()):
            raise ValueError("The white export darkens some pixels. Check that only the frames changed.")
        response = ImageChops.subtract(white, black)
        if any(response.crop((0, y, black.width, y + 1)).getbbox() for y in cuts):
            raise ValueError("The expected gaps between photo slots are not empty.")
        edges = (0, *cuts, black.height)
        slots = []
        masks = []
        for index, (top, bottom) in enumerate(zip(edges, edges[1:]), 1):
            band = response.crop((0, top, black.width, bottom))
            box = band.getbbox()
            if box is None:
                raise ValueError(f"No photo area was detected in slot {index}.")
            left, upper, right, lower = box
            masks.append(band.crop(box))
            slots.append([left, top + upper, right, top + lower])

    folder = source / "Ready" / f"page-{page}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "base.png").write_bytes(black_png)
    for index, mask in enumerate(masks, 1):
        mask.save(folder / f"slot-{index}.png")
    (folder / "slots.json").write_text(json.dumps(slots, indent=2) + "\n", encoding="utf-8")
    prepare_fast(folder)
    return folder


def prepare_fast(template: Path) -> None:
    """Pre-scale the artwork and masks, keeping the masks on the same pixel grid."""
    target = template / "fast"
    target.mkdir(exist_ok=True)
    with Image.open(template / "base.png") as base:
        width, height = base.width // 2, base.height // 2
        base.crop((0, 0, width * 2, height * 2)).resize((width, height), Image.Resampling.BOX).save(
            target / "base.png")
    slots = json.loads((template / "slots.json").read_text(encoding="utf-8"))
    scaled = []
    for index, (left, top, right, bottom) in enumerate(slots, 1):
        x, y = left // 2, top // 2
        end_x, end_y = min(width, (right + 1) // 2), min(height, (bottom + 1) // 2)
        size = end_x - x, end_y - y
        with Image.open(template / f"slot-{index}.png") as mask:
            padded = Image.new("RGB", (size[0] * 2, size[1] * 2))
            padded.paste(mask, (left - 2 * x, top - 2 * y))
            padded.resize(size, Image.Resampling.BOX).save(target / f"slot-{index}.png")
        scaled.append([x, y, end_x, end_y])
    (target / "slots.json").write_text(json.dumps(scaled, indent=2) + "\n", encoding="utf-8")


def render_strip(template: Path, photos: list[Image.Image]) -> Image.Image:
    """Center-crop each photo and render at the prepared template's size."""
    if len(photos) != 3:
        raise ValueError("A strip needs exactly three photos, in top-to-bottom order.")
    slots = json.loads((template / "slots.json").read_text(encoding="utf-8"))
    if len(slots) != 3:
        raise ValueError("The template needs exactly three slots.")
    with Image.open(template / "base.png") as base:
        output = base.convert("RGB")
    for index, (photo, box) in enumerate(zip(photos, slots), 1):
        left, top, right, bottom = box
        size = (right - left, bottom - top)
        shot = ImageOps.fit(ImageOps.exif_transpose(photo).convert("RGB"), size, Image.Resampling.LANCZOS)
        with Image.open(template / f"slot-{index}.png") as mask:
            # The black export already includes the artwork in front of each frame.
            # Add only the contribution from the photo, avoiding a dark halo at the edges.
            contribution = ImageChops.multiply(shot, mask.convert("RGB"))
        output.paste(ImageChops.add(output.crop(box), contribution), (left, top))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="generate assets for pages 2 and 3")
    render = commands.add_parser("render", help="make a strip from three existing images")
    render.add_argument("page", choices=CUTS)
    render.add_argument("output", type=Path)
    render.add_argument("photos", nargs=3, type=Path, metavar="PHOTO")
    args = parser.parse_args()
    if args.command == "prepare":
        for page in CUTS:
            print(prepare(page))
    else:
        photos = []
        for file in args.photos:
            with Image.open(file) as image:
                photos.append(image.copy())
        image = render_strip(READY / f"page-{args.page}", photos)
        if args.output.suffix.lower() in {".jpg", ".jpeg"}:
            image.save(args.output, quality=92)
        else:
            image.save(args.output)
        print(args.output)


if __name__ == "__main__":
    main()
