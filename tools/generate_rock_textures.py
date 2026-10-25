#!/usr/bin/env python3
"""Generate and verify the ERYDON resource packs' 32x and 64x Rock textures.

The authored inputs are 6x6 albedo, normal, and specular tile sheets. This
tool splits them into Synapheia repeat tiles, creates the matching root block
textures and CTM properties, and registers each tile directory in the atlas.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter


REPO_ROOT = Path(__file__).resolve().parents[1]

MATERIALS = (
    "aganite",
    "aterzon",
    "borealis",
    "brectite",
    "calacattum",
    "chalstrom",
    "chrysonyx",
    "etruscus",
    "gelastrum",
    "glacium",
    "hesperion",
    "imperium",
    "kelastrion",
    "kylorion",
    "latmion",
    "laurentium",
    "mielonyx",
    "nerium",
    "noxoplis",
    "porphyros",
    "portorium",
    "psamatheon",
    "rosinium",
    "sanguenite",
    "selenephos",
    "solistra",
    "striatus",
)

TIERS = {
    "32x": 32,
    "64x": 64,
}

MAPS = (
    ("", "", "RGB"),
    ("_n", "_n", "RGBA"),
    ("_s", "_s", "RGB"),
)

# Iris POM reads the normal map's alpha channel as height. The authored Rock
# sheets retain useful broad rock-face relief, but their fine-grained height
# variation is too strong in game. Smooth only that alpha channel while
# preserving every RGB normal value. A small edge mask restores sharper relief
# at the strongest cracks and ledges, then the filtered height is fitted to the
# approved 190-255 range. The radii and edge thresholds are resolution-aware so
# 32x keeps the same visual treatment as the approved 64x result. Tiling before
# every filter keeps the 6x6 repeat sheet seamless at its outer edges.
ROCK_HEIGHT_FILTERS = {
    "32x": {
        "base_blur_radius": 1.0,
        "edge_blur_radius": 0.5,
        "edge_detection_blur_radius": 0.75,
        "edge_window": 3,
        "edge_low": 11,
        "edge_high": 16,
    },
    "64x": {
        "base_blur_radius": 2.0,
        "edge_blur_radius": 1.0,
        "edge_detection_blur_radius": 1.5,
        "edge_window": 5,
        "edge_low": 15,
        "edge_high": 22,
    },
}
ROCK_HEIGHT_TARGET_MINIMUM = 190
ROCK_HEIGHT_TARGET_MAXIMUM = 255

BASE_CTM_FORMS = (
    "block",
    "slab",
    "layer",
    "layer_vertical",
    "slope",
    "slope_shallow_lower",
    "slope_shallow_upper",
    "slope_steep_lower",
    "slope_steep_upper",
    "slope_vertical",
    "slope_vertical_shallow_narrow",
    "slope_vertical_shallow_broad",
    "arch_romanesque",
    "slice_vertical",
    "slice_horizontal",
    "post",
    "stairs",
    "wall",
    "alcove_georgian",
    "alcove_gothic",
)

ARCH_CTM_FORMS = ("arch_modern", "arch_gothic")


def wrap_filter(channel: Image.Image, image_filter: ImageFilter.Filter) -> Image.Image:
    width, height = channel.size
    tiled = Image.new("L", (width * 3, height * 3))
    for tile_y in range(3):
        for tile_x in range(3):
            tiled.paste(channel, (tile_x * width, tile_y * height))
    return tiled.filter(image_filter).crop(
        (width, height, width * 2, height * 2)
    )


def wrap_gaussian_blur(channel: Image.Image, radius: float) -> Image.Image:
    return wrap_filter(channel, ImageFilter.GaussianBlur(radius))


def scale_height_range(source: Image.Image, filtered: Image.Image) -> Image.Image:
    source_minimum, source_maximum = source.getextrema()
    filtered_minimum, filtered_maximum = filtered.getextrema()

    if source_minimum == source_maximum or filtered_minimum == filtered_maximum:
        return source.copy()

    target_minimum = ROCK_HEIGHT_TARGET_MINIMUM
    target_maximum = ROCK_HEIGHT_TARGET_MAXIMUM
    target_range = target_maximum - target_minimum
    scale = target_range / (filtered_maximum - filtered_minimum)
    height_lut = [
        max(
            0,
            min(
                255,
                round(target_minimum + (value - filtered_minimum) * scale),
            ),
        )
        for value in range(256)
    ]
    return filtered.point(height_lut)


def smooth_rock_height(tier: str, normal_sheet: Image.Image) -> Image.Image:
    settings = ROCK_HEIGHT_FILTERS[tier]
    red, green, blue, height = normal_sheet.convert("RGBA").split()
    base = wrap_gaussian_blur(height, settings["base_blur_radius"])
    edge_detail = wrap_gaussian_blur(height, settings["edge_blur_radius"])
    edge_reference = wrap_gaussian_blur(
        height, settings["edge_detection_blur_radius"]
    )
    local_maximum = wrap_filter(
        edge_reference, ImageFilter.MaxFilter(settings["edge_window"])
    )
    local_minimum = wrap_filter(
        edge_reference, ImageFilter.MinFilter(settings["edge_window"])
    )
    local_range = ImageChops.subtract(local_maximum, local_minimum)
    edge_mask_lut = [
        max(
            0,
            min(
                255,
                round(
                    (value - settings["edge_low"])
                    * 255
                    / (settings["edge_high"] - settings["edge_low"])
                ),
            ),
        )
        for value in range(256)
    ]
    edge_mask = local_range.point(edge_mask_lut)
    filtered_height = Image.composite(edge_detail, base, edge_mask)
    return Image.merge(
        "RGBA", (red, green, blue, scale_height_range(height, filtered_height))
    )


def prepare_sheet(
    tier: str,
    sheet_suffix: str,
    sheet: Image.Image,
) -> Image.Image:
    if tier in ROCK_HEIGHT_FILTERS and sheet_suffix == "_n":
        return smooth_rock_height(tier, sheet)
    return sheet.copy()


def expected_source_names() -> set[str]:
    return {
        f"{material}_rock{sheet_suffix}.png"
        for material in MATERIALS
        for sheet_suffix, _, _ in MAPS
    }


def validate_source_sheets(source_root: Path, tile_size: int) -> None:
    if not source_root.is_dir():
        raise RuntimeError(f"Rock source directory does not exist: {source_root}")

    missing = sorted(
        name for name in expected_source_names() if not (source_root / name).is_file()
    )
    if missing:
        raise RuntimeError(f"Missing Rock source sheets in {source_root}: {missing}")

    sheet_size = tile_size * 6
    for material in MATERIALS:
        for sheet_suffix, _, expected_mode in MAPS:
            source = source_root / f"{material}_rock{sheet_suffix}.png"
            with Image.open(source) as sheet:
                sheet.load()
                if sheet.size != (sheet_size, sheet_size):
                    raise RuntimeError(
                        f"Expected {sheet_size}x{sheet_size} sheet, found "
                        f"{sheet.size}: {source}"
                    )
                if sheet.mode != expected_mode:
                    raise RuntimeError(
                        f"Expected {expected_mode} sheet, found {sheet.mode}: {source}"
                    )


def ctm_properties(material: str, forms: tuple[str, ...]) -> str:
    tiles = " ".join(
        f"textures/optifine/ctm/{material}_rock/{index}"
        for index in range(36)
    )
    matches = [f"erydon:{material}_rock_{form}" for form in forms]
    continuation = " \\\n  "
    match_lines = continuation.join(matches)
    return (
        "method=repeat\n"
        "width=6\n"
        "height=6\n"
        f"tiles={tiles}\n"
        f"matchBlocks={match_lines}\n"
        "faces=all\n"
        "connect=block\n"
        "innerSeams=true\n"
        "priority=10\n"
    )


def add_atlas_sources(atlas_path: Path) -> None:
    data = atlas_path.read_bytes()
    newline = b"\r\n" if b"\r\n" in data else b"\n"

    for material in MATERIALS:
        hewn_path = f"optifine/ctm/{material}_hewn"
        rock_path = f"optifine/ctm/{material}_rock"
        rock_marker = f'"source":  "{rock_path}"'.encode("ascii")
        if data.count(rock_marker) == 1:
            continue
        if data.count(rock_marker) != 0:
            raise RuntimeError(f"Duplicate Rock atlas source in {atlas_path}: {rock_path}")

        def directory_entry(source: str) -> bytes:
            lines = (
                "                    {",
                '                        "type":  "directory",',
                f'                        "source":  "{source}",',
                f'                        "prefix":  "{source}/"',
                "                    },",
            )
            return newline.join(line.encode("ascii") for line in lines) + newline

        hewn_entry = directory_entry(hewn_path)
        if data.count(hewn_entry) != 1:
            raise RuntimeError(
                f"Could not find one exact Hewn atlas entry in {atlas_path}: {hewn_path}"
            )
        data = data.replace(hewn_entry, hewn_entry + directory_entry(rock_path), 1)

    atlas_path.write_bytes(data)


def generate_tier(tier: str, source_root: Path) -> None:
    tile_size = TIERS[tier]
    pack_root = REPO_ROOT / "packs" / tier
    ctm_texture_root = (
        pack_root
        / "assets"
        / "minecraft"
        / "textures"
        / "optifine"
        / "ctm"
    )
    ctm_property_root = (
        pack_root / "assets" / "minecraft" / "optifine" / "ctm"
    )
    block_texture_root = pack_root / "assets" / "erydon" / "textures" / "block"

    for material in MATERIALS:
        texture_dir = ctm_texture_root / f"{material}_rock"
        texture_dir.mkdir(parents=True, exist_ok=True)
        for sheet_suffix, tile_suffix, _ in MAPS:
            source = source_root / f"{material}_rock{sheet_suffix}.png"
            with Image.open(source) as authored_sheet:
                authored_sheet.load()
                sheet = prepare_sheet(tier, sheet_suffix, authored_sheet)
                for tile_index in range(36):
                    tile_x = (tile_index % 6) * tile_size
                    tile_y = (tile_index // 6) * tile_size
                    tile = sheet.crop(
                        (
                            tile_x,
                            tile_y,
                            tile_x + tile_size,
                            tile_y + tile_size,
                        )
                    )
                    tile.save(texture_dir / f"{tile_index}{tile_suffix}.png")

            block_texture_root.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                texture_dir / f"0{tile_suffix}.png",
                block_texture_root / f"{material}_rock_block{tile_suffix}.png",
            )

        property_dir = ctm_property_root / f"{material}_rock"
        property_dir.mkdir(parents=True, exist_ok=True)
        (property_dir / f"a_{material}_rock_base.properties").write_text(
            ctm_properties(material, BASE_CTM_FORMS),
            encoding="utf-8",
            newline="\n",
        )
        (property_dir / f"a_{material}_rock_arches.properties").write_text(
            ctm_properties(material, ARCH_CTM_FORMS),
            encoding="utf-8",
            newline="\n",
        )

    add_atlas_sources(
        pack_root / "assets" / "minecraft" / "atlases" / "blocks.json"
    )


def validate_tiles_against_source(tier: str, source_root: Path) -> None:
    tile_size = TIERS[tier]
    texture_root = (
        REPO_ROOT
        / "packs"
        / tier
        / "assets"
        / "minecraft"
        / "textures"
        / "optifine"
        / "ctm"
    )
    for material in MATERIALS:
        for sheet_suffix, tile_suffix, _ in MAPS:
            source = source_root / f"{material}_rock{sheet_suffix}.png"
            with Image.open(source) as authored_sheet:
                authored_sheet.load()
                sheet = prepare_sheet(tier, sheet_suffix, authored_sheet)
                for tile_index in range(36):
                    tile_x = (tile_index % 6) * tile_size
                    tile_y = (tile_index // 6) * tile_size
                    expected = sheet.crop(
                        (
                            tile_x,
                            tile_y,
                            tile_x + tile_size,
                            tile_y + tile_size,
                        )
                    )
                    target = (
                        texture_root
                        / f"{material}_rock"
                        / f"{tile_index}{tile_suffix}.png"
                    )
                    with Image.open(target) as actual:
                        actual.load()
                        if (
                            actual.mode != expected.mode
                            or actual.size != expected.size
                            or actual.tobytes() != expected.tobytes()
                        ):
                            raise RuntimeError(
                                f"{tier} Rock tile does not match its authored "
                                f"sheet region: {target}"
                            )


def validate_tier(tier: str) -> None:
    tile_size = TIERS[tier]
    pack_root = REPO_ROOT / "packs" / tier
    ctm_texture_root = (
        pack_root
        / "assets"
        / "minecraft"
        / "textures"
        / "optifine"
        / "ctm"
    )
    ctm_property_root = (
        pack_root / "assets" / "minecraft" / "optifine" / "ctm"
    )
    block_texture_root = pack_root / "assets" / "erydon" / "textures" / "block"
    atlas_path = pack_root / "assets" / "minecraft" / "atlases" / "blocks.json"
    atlas = json.loads(atlas_path.read_text(encoding="utf-8-sig"))
    atlas_directories = [
        source.get("source")
        for source in atlas.get("sources", [])
        if source.get("type") == "directory"
    ]

    for material in MATERIALS:
        texture_dir = ctm_texture_root / f"{material}_rock"
        expected_png_names = {
            f"{tile_index}{tile_suffix}.png"
            for tile_index in range(36)
            for _, tile_suffix, _ in MAPS
        }
        actual_png_names = {path.name for path in texture_dir.glob("*.png")}
        if actual_png_names != expected_png_names:
            missing = sorted(expected_png_names - actual_png_names)
            extra = sorted(actual_png_names - expected_png_names)
            raise RuntimeError(
                f"Unexpected {tier} Rock tiles for {material}: "
                f"missing={missing}, extra={extra}"
            )

        for _, tile_suffix, expected_mode in MAPS:
            height_extrema: list[tuple[int, int]] = []
            for tile_index in range(36):
                tile_path = texture_dir / f"{tile_index}{tile_suffix}.png"
                with Image.open(tile_path) as tile:
                    if tile.size != (tile_size, tile_size) or tile.mode != expected_mode:
                        raise RuntimeError(
                            f"Unexpected {tier} Rock tile format: {tile_path} "
                            f"{tile.size} {tile.mode}"
                        )
                    if tile_suffix == "_n":
                        height_extrema.append(tile.getchannel("A").getextrema())
            if tile_suffix == "_n":
                combined_extrema = (
                    min(minimum for minimum, _ in height_extrema),
                    max(maximum for _, maximum in height_extrema),
                )
                if combined_extrema != (
                    ROCK_HEIGHT_TARGET_MINIMUM,
                    ROCK_HEIGHT_TARGET_MAXIMUM,
                ):
                    raise RuntimeError(
                        f"Unexpected {tier} Rock height range for {material}: "
                        f"{combined_extrema}"
                    )
            root_texture = block_texture_root / f"{material}_rock_block{tile_suffix}.png"
            if root_texture.read_bytes() != (texture_dir / f"0{tile_suffix}.png").read_bytes():
                raise RuntimeError(
                    f"Root {tier} Rock texture is not CTM tile 0: {root_texture}"
                )

        property_dir = ctm_property_root / f"{material}_rock"
        expected_properties = {
            f"a_{material}_rock_base.properties": ctm_properties(
                material, BASE_CTM_FORMS
            ),
            f"a_{material}_rock_arches.properties": ctm_properties(
                material, ARCH_CTM_FORMS
            ),
        }
        expected_property_names = set(expected_properties)
        actual_property_names = {
            path.name for path in property_dir.glob("*.properties")
        }
        if actual_property_names != expected_property_names:
            raise RuntimeError(
                f"Unexpected {tier} Rock CTM properties for {material}: "
                f"{sorted(actual_property_names)}"
            )
        for filename, expected_text in expected_properties.items():
            data = (property_dir / filename).read_bytes()
            if data.startswith(b"\xef\xbb\xbf") or data.decode("utf-8") != expected_text:
                raise RuntimeError(
                    f"Invalid {tier} Rock CTM properties: {property_dir / filename}"
                )

        atlas_source = f"optifine/ctm/{material}_rock"
        if atlas_directories.count(atlas_source) != 1:
            raise RuntimeError(
                f"Expected one {tier} Rock atlas entry for {atlas_source}"
            )

    expected_root_names = {
        f"{material}_rock_block{tile_suffix}.png"
        for material in MATERIALS
        for _, tile_suffix, _ in MAPS
    }
    actual_root_names = {
        path.name
        for path in block_texture_root.glob("*_rock_block*.png")
    }
    if actual_root_names != expected_root_names:
        missing = sorted(expected_root_names - actual_root_names)
        extra = sorted(actual_root_names - expected_root_names)
        raise RuntimeError(
            f"Unexpected {tier} root Rock textures: missing={missing}, extra={extra}"
        )


def validate_outputs() -> None:
    for tier in TIERS:
        validate_tier(tier)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-32x",
        type=Path,
        help="Directory containing the 192x192 Rock sheets",
    )
    parser.add_argument(
        "--source-64x",
        type=Path,
        help="Directory containing the 384x384 Rock sheets",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed Rock outputs without rewriting them",
    )
    args = parser.parse_args()

    if args.check:
        validate_outputs()
        print(
            "ERYDON resource-pack Rock textures verified: 27 materials x 3 maps x "
            "36 tiles, in both 32x and 64x."
        )
        return 0

    if args.source_32x is None or args.source_64x is None:
        parser.error("--source-32x and --source-64x are both required")

    sources = {
        "32x": args.source_32x.resolve(),
        "64x": args.source_64x.resolve(),
    }
    for tier, tile_size in TIERS.items():
        validate_source_sheets(sources[tier], tile_size)
    for tier in TIERS:
        generate_tier(tier, sources[tier])
    validate_outputs()
    for tier in TIERS:
        validate_tiles_against_source(tier, sources[tier])
    print(
        "Generated and verified ERYDON resource-pack Rock textures: 27 materials x "
        "3 maps x 36 tiles, in both 32x and 64x."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
