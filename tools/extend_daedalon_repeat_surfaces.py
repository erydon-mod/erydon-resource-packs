#!/usr/bin/env python3
"""Extend Daedalon's 6x6 material sheets to two horizontal periods."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


PREFIX = "statue_spartan_promachos_"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="extend stale square source sheets; otherwise only validate",
    )
    return parser.parse_args()


def texture_roots() -> tuple[Path, Path]:
    repository = Path(__file__).resolve().parents[1]
    relative = Path("assets/daedalon/textures/block")
    return repository / "packs/32x" / relative, repository / "packs/64x" / relative


def validate_or_extend(path: Path, write: bool) -> bool:
    with Image.open(path) as opened:
        opened.load()
        image = opened.copy()

    width, height = image.size
    if width == height:
        if not write:
            return False
        extended = Image.new(image.mode, (width * 2, height))
        if image.mode == "P":
            extended.putpalette(image.getpalette())
        extended.paste(image, (0, 0))
        extended.paste(image, (width, 0))
        temporary = path.with_name(path.name + ".tmp")
        try:
            extended.save(temporary, format="PNG", optimize=False, compress_level=9)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return True

    if width != height * 2:
        raise ValueError(
            f"{path} must be a square 6x6 source or a 12x6 extended sheet, got {width}x{height}"
        )

    left = image.crop((0, 0, height, height))
    right = image.crop((height, 0, width, height))
    if left.mode != right.mode or left.tobytes() != right.tobytes():
        raise ValueError(f"{path} right repeat does not exactly match its left 6x6 source")
    return True


def main() -> int:
    args = parse_args()
    stale: list[Path] = []
    checked = 0

    for root in texture_roots():
        paths = sorted(root.glob(f"{PREFIX}*.png"))
        if not paths:
            raise FileNotFoundError(f"No {PREFIX}*.png files found under {root}")
        for path in paths:
            checked += 1
            if not validate_or_extend(path, args.write):
                stale.append(path)

    if stale:
        print("Daedalon repeat surfaces require horizontal extension:")
        for path in stale:
            print(f"  {path}")
        return 1

    action = "written/validated" if args.write else "validated"
    print(f"ERYDON resource-pack Daedalon repeat surfaces {action}: files={checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
