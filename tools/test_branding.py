"""Exercise the portable branding audit with disposable, self-contained assets."""
from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from contextlib import contextmanager
from pathlib import Path

from tools.audit_branding import audit


def png_rgba(width: int, height: int, colour: bytes = b"\x00\x00\x00\x00") -> bytes:
    """Encode a real RGBA PNG using only the standard library."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixels = (b"\x00" + colour * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def animated_gif(delay_cs: int = 5, loop: int | None = 0, frames: int = 2) -> bytes:
    """Encode alternating one-pixel GIF frames with explicit timing and looping."""
    result = bytearray(b"GIF89a" + struct.pack("<HHBBB", 2, 1, 0x80, 0, 0))
    result.extend(b"\x00\x00\x00\xff\xff\xff")  # Two-colour global table.
    if loop is not None:
        result.extend(b"\x21\xff\x0bNETSCAPE2.0\x03\x01")
        result.extend(struct.pack("<H", loop) + b"\x00")
    for frame in range(frames):
        result.extend(b"\x21\xf9\x04\x00" + struct.pack("<H", delay_cs) + b"\x00\x00")
        result.extend(b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0))
        # LZW minimum size 2: clear=4, pixel=0/1, end=5; three 3-bit codes.
        codes = 4 | ((frame % 2) << 3) | (5 << 6)
        result.extend(b"\x02\x02" + struct.pack("<H", codes) + b"\x00")
    result.extend(b"\x3b")
    return bytes(result)


class BrandingFixture:
    ICON = "src/main/resources/assets/example/icon.png"
    CREATIVE = "src/main/resources/assets/example/textures/item/emblem.png"
    GUI = "src/main/resources/assets/example/textures/gui/brand_logo.png"
    GIF = "docs/branding/example-logo.gif"
    CONSUMER = "src/main/resources/fabric.mod.json"
    RENDERER = "src/main/java/Logo.java"
    MASTER = "src/main/resources/assets/example/logo.png"

    def __init__(self, root: Path):
        self.root = root
        self.contract = {
            "schema": 1,
            "project": "example",
            "assets": [],
            "forbidden": [self.MASTER],
            "runtime_roots": ["src/main/resources/assets"],
            "references": [
                {"path": self.CONSUMER, "pointer": ["icon"],
                 "equals": "assets/example/icon.png"},
                {"path": self.RENDERER, "pattern": r"FRAME_COUNT\s*=\s*2\b"},
            ],
        }
        self.add_asset(self.ICON, png_rgba(2, 2), role="static_icon", size=[2, 2])
        self.add_asset(self.CREATIVE, png_rgba(64, 128), role="creative_emblem",
                       size=[64, 128], frame_size=[64, 64], frames=2)
        self.add_asset(self.GUI, png_rgba(32, 16), role="gui_logo",
                       size=[32, 16], frame_size=[16, 16], frames=2)
        self.add_asset(self.GIF, animated_gif(), role="readme_logo",
                       size=[2, 1], frames=2, readme="README.md")
        self.write_json(self.CREATIVE + ".mcmeta",
                        {"animation": {"frametime": 1, "interpolate": False}})
        self.write_json(self.GUI + ".mcmeta", {"texture": {"blur": True, "clamp": True}})
        self.write_json(self.CONSUMER, {"icon": "assets/example/icon.png"})
        self.write(self.RENDERER, b"class Logo { static final int FRAME_COUNT = 2; }\n")
        self.write("README.md", f"![Example animated logo]({self.GIF})\n".encode())

    def write(self, relative: str, data: bytes) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_json(self, relative: str, value: object) -> None:
        self.write(relative, json.dumps(value).encode())

    def add_asset(self, relative: str, data: bytes, **metadata: object) -> None:
        self.write(relative, data)
        self.contract["assets"].append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                                         "bytes": len(data), **metadata})

    def approve_replacement(self, relative: str, data: bytes, **metadata: object) -> None:
        """Refresh identity so tests can reach semantic checks beyond the checksum."""
        self.write(relative, data)
        asset = next(item for item in self.contract["assets"] if item["path"] == relative)
        asset.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), **metadata)

    def run(self) -> dict:
        self.write_json("config/branding-assets.json", self.contract)
        return audit(self.root)


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix="branding-test-") as directory:
        yield BrandingFixture(Path(directory))


class BrandingAuditTest(unittest.TestCase):
    def test_valid_contract_and_consumers_are_self_contained(self):
        with fixture() as case:
            result = case.run()
            self.assertEqual("ok", result["status"])
            self.assertEqual("example", result["project"])
            self.assertEqual({case.ICON, case.CREATIVE, case.GUI, case.GIF},
                             {item["path"] for item in result["assets"]})

    def test_changed_artwork_is_rejected(self):
        with fixture() as case:
            case.write(case.ICON, png_rgba(2, 2, b"\xff\x00\x00\xff"))
            with self.assertRaisesRegex(ValueError, "Approved artwork changed"):
                case.run()

    def test_forbidden_runtime_master_is_rejected(self):
        with fixture() as case:
            case.write(case.MASTER, png_rgba(1024, 1))
            with self.assertRaisesRegex(ValueError, "Unused full-resolution master in runtime"):
                case.run()

    def test_broken_json_and_renderer_consumers_are_rejected(self):
        for change, expected in [("wrong_icon", "Branding consumer changed"),
                                 ("malformed_json", "Expecting"),
                                 ("wrong_renderer", "Branding renderer/reference changed")]:
            with self.subTest(change=change), fixture() as case:
                if change == "wrong_icon":
                    case.write_json(case.CONSUMER, {"icon": "assets/example/old.png"})
                elif change == "malformed_json":
                    case.write(case.CONSUMER, b"{not json}")
                else:
                    case.write(case.RENDERER, b"class Logo { static final int FRAME_COUNT = 3; }\n")
                with self.assertRaisesRegex(ValueError, expected):
                    case.run()

    def test_gif_timing_loop_and_frame_count_are_checked_after_identity(self):
        replacements = [
            (animated_gif(delay_cs=4), "20-fps timing"),
            (animated_gif(loop=1), "loop continuously"),
            (animated_gif(loop=None), "loop continuously"),
            (animated_gif(frames=3), "GIF frame count changed"),
        ]
        for data, expected in replacements:
            with self.subTest(expected=expected), fixture() as case:
                case.approve_replacement(case.GIF, data)
                with self.assertRaisesRegex(ValueError, expected):
                    case.run()

    def test_animation_sheets_require_capacity_and_one_filtered_gui_owner(self):
        for change, expected in [("capacity", "Animation frames leave their sheet"),
                                 ("unfiltered", "filtering and one animation owner"),
                                 ("double_animation", "filtering and one animation owner"),
                                 ("creative_timing", "Creative emblem timing changed")]:
            with self.subTest(change=change), fixture() as case:
                if change == "capacity":
                    case.approve_replacement(case.GUI, png_rgba(16, 16), size=[16, 16])
                elif change == "unfiltered":
                    case.write_json(case.GUI + ".mcmeta", {"texture": {"blur": False, "clamp": True}})
                elif change == "double_animation":
                    case.write_json(case.GUI + ".mcmeta", {
                        "texture": {"blur": True, "clamp": True}, "animation": {"frametime": 1}})
                else:
                    case.write_json(case.CREATIVE + ".mcmeta",
                                    {"animation": {"frametime": 2, "interpolate": False}})
                with self.assertRaisesRegex(ValueError, expected):
                    case.run()

    def test_asset_and_consumer_paths_cannot_escape_the_repository(self):
        for change in ("asset", "consumer"):
            with self.subTest(change=change), fixture() as case:
                if change == "asset":
                    case.contract["assets"][0]["path"] = "../outside.png"
                else:
                    case.contract["references"][0]["path"] = "../outside.json"
                with self.assertRaisesRegex(ValueError, "Path leaves the repository"):
                    case.run()

    def test_unlisted_runtime_logo_is_rejected(self):
        with fixture() as case:
            case.write("src/main/resources/assets/example/textures/gui/unlisted_logo.png",
                       png_rgba(2, 1))
            with self.assertRaisesRegex(ValueError, "Undeclared runtime branding asset"):
                case.run()


if __name__ == "__main__":
    unittest.main()
