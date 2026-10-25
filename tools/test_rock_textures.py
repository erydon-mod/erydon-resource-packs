from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from PIL import Image


SCRIPT = Path(__file__).with_name("generate_rock_textures.py")
SPEC = importlib.util.spec_from_file_location("erydon_generate_rock_textures", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ROCK_TEXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROCK_TEXTURES)


class RockTexturesTest(unittest.TestCase):
    def test_committed_32x_and_64x_outputs_are_complete(self) -> None:
        ROCK_TEXTURES.validate_outputs()

    def test_family_matches_complete_hewn_material_set(self) -> None:
        for tier in ROCK_TEXTURES.TIERS:
            ctm_root = (
                ROCK_TEXTURES.REPO_ROOT
                / "packs"
                / tier
                / "assets"
                / "minecraft"
                / "textures"
                / "optifine"
                / "ctm"
            )
            hewn_materials = tuple(
                sorted(
                    path.name.removesuffix("_hewn")
                    for path in ctm_root.glob("*_hewn")
                    if path.is_dir()
                )
            )
            self.assertEqual(ROCK_TEXTURES.MATERIALS, hewn_materials)

    def test_height_filters_preserve_rgb_and_reduce_fine_relief(self) -> None:
        source = Image.new("RGBA", (64, 64))
        pixels = source.load()
        for y in range(64):
            for x in range(64):
                broad_height = 200 if x < 32 else 245
                fine_variation = -4 if (x + y) % 2 == 0 else 4
                pixels[x, y] = (
                    64 + x,
                    96 + y,
                    255 - x,
                    broad_height + fine_variation,
                )

        def fine_relief(channel: Image.Image) -> float:
            total = 0
            samples = 0
            for y in range(8, 56):
                for x in range(8, 24):
                    value = channel.getpixel((x, y))
                    total += abs(value - channel.getpixel((x + 1, y)))
                    total += abs(value - channel.getpixel((x, y + 1)))
                    samples += 2
            return total / samples

        def step_contrast(channel: Image.Image) -> float:
            return sum(
                abs(channel.getpixel((31, y)) - channel.getpixel((32, y)))
                for y in range(channel.height)
            ) / channel.height

        source_height = source.getchannel("A")
        for tier, settings in ROCK_TEXTURES.ROCK_HEIGHT_FILTERS.items():
            with self.subTest(tier=tier):
                unchanged = ROCK_TEXTURES.prepare_sheet(tier, "", source)
                smoothed = ROCK_TEXTURES.prepare_sheet(tier, "_n", source)
                smoothed_height = smoothed.getchannel("A")

                self.assertEqual(source.tobytes(), unchanged.tobytes())
                self.assertEqual(
                    source.convert("RGB").tobytes(),
                    smoothed.convert("RGB").tobytes(),
                )
                self.assertNotEqual(
                    source_height.tobytes(), smoothed_height.tobytes()
                )
                self.assertEqual(
                    (
                        ROCK_TEXTURES.ROCK_HEIGHT_TARGET_MINIMUM,
                        ROCK_TEXTURES.ROCK_HEIGHT_TARGET_MAXIMUM,
                    ),
                    smoothed_height.getextrema(),
                )
                self.assertLess(
                    fine_relief(smoothed_height),
                    fine_relief(source_height),
                )

                broad_blur = ROCK_TEXTURES.wrap_gaussian_blur(
                    source_height, settings["base_blur_radius"]
                )
                broad_blur = ROCK_TEXTURES.scale_height_range(
                    source_height, broad_blur
                )
                self.assertGreater(
                    step_contrast(smoothed_height),
                    step_contrast(broad_blur),
                )


if __name__ == "__main__":
    unittest.main()
