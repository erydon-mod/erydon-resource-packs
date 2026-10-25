"""Validate approved branding exports and their consumers without building archives.

This file is shared unchanged by the ERYDON family source repositories. Each
repository owns its portable config/branding-assets.json; no sibling checkout
or external marketing directory is needed for local checks or CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def local_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    require(not Path(relative).is_absolute() and path.is_relative_to(root),
            f"Path leaves the repository: {relative}")
    return path


def gif_metadata(data: bytes) -> dict:
    require(data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 13, "Invalid GIF header")
    width, height = struct.unpack_from("<HH", data, 6)
    cursor = 13 + (3 * (2 << (data[10] & 7)) if data[10] & 128 else 0)
    durations, loop, delay = [], None, None

    def blocks(position: int) -> tuple[bytes, int]:
        chunks = []
        while True:
            require(position < len(data), "Truncated GIF block")
            length = data[position]
            position += 1
            if not length:
                return b"".join(chunks), position
            require(position + length <= len(data), "Truncated GIF payload")
            chunks.append(data[position:position + length])
            position += length

    while cursor < len(data):
        marker = data[cursor]
        cursor += 1
        if marker == 0x3B:
            require(bool(durations), "GIF has no frames")
            return {"size": [width, height], "frames": len(durations),
                    "durations_ms": durations, "loop": loop}
        if marker == 0x21:
            require(cursor < len(data), "Truncated GIF extension")
            label = data[cursor]
            cursor += 1
            payload, cursor = blocks(cursor)
            if label == 0xF9:
                require(len(payload) == 4, "Invalid GIF timing extension")
                delay = struct.unpack_from("<H", payload, 1)[0] * 10
            elif label == 0xFF and payload[:11] in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
                require(len(payload) >= 14 and payload[11] == 1, "Invalid GIF loop extension")
                loop = struct.unpack_from("<H", payload, 12)[0]
        elif marker == 0x2C:
            require(cursor + 9 <= len(data), "Truncated GIF frame")
            left, top, frame_width, frame_height, flags = struct.unpack_from("<HHHHB", data, cursor)
            require(frame_width > 0 and frame_height > 0
                    and left + frame_width <= width and top + frame_height <= height,
                    "GIF frame leaves its canvas")
            cursor += 9 + (3 * (2 << (flags & 7)) if flags & 128 else 0)
            require(cursor < len(data), "Missing GIF image data")
            cursor += 1  # LZW minimum code size; payload identity is checked by SHA-256.
            _, cursor = blocks(cursor)
            durations.append(delay)
            delay = None
        else:
            raise ValueError(f"Unknown GIF marker: {marker:#x}")
    raise ValueError("Missing GIF trailer")


def png_metadata(data: bytes) -> dict:
    require(data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
            and len(data) >= 33, "Invalid PNG header")
    width, height, depth, colour = struct.unpack_from(">IIBB", data, 16)
    return {"size": [width, height], "bit_depth": depth, "colour_type": colour}


def audit(root: Path) -> dict:
    root = root.resolve()
    contract = json.loads((root / "config/branding-assets.json").read_text(encoding="utf-8"))
    require(contract.get("schema") == 1 and contract.get("assets"), "Invalid branding contract")
    declared, checked = set(), []
    for asset in contract["assets"]:
        relative = asset["path"]
        require(relative not in declared, f"Duplicate branding asset: {relative}")
        declared.add(relative)
        path = local_path(root, relative)
        require(path.is_file(), f"Missing branding asset: {relative}")
        data = path.read_bytes()
        require(hashlib.sha256(data).hexdigest() == asset["sha256"],
                f"Approved artwork changed: {relative}")
        require(len(data) == asset["bytes"], f"Branding byte size changed: {relative}")
        if path.suffix.lower() == ".gif":
            info = gif_metadata(data)
            require(info["loop"] == 0, f"README animation must loop continuously: {relative}")
            require(info["frames"] == asset["frames"], f"GIF frame count changed: {relative}")
            require(all(delay == 50 for delay in info["durations_ms"]),
                    f"README animation must retain approved 20-fps timing: {relative}")
            readme = local_path(root, asset["readme"]).read_text(encoding="utf-8")
            require(relative in readme, f"README does not reference its animation: {relative}")
            require(not relative.startswith(("src/", "packs/")),
                    f"Documentation GIF must stay outside runtime resources: {relative}")
        else:
            info = png_metadata(data)
            require(info["bit_depth"] == 8 and info["colour_type"] == 6,
                    f"Runtime branding must be an 8-bit RGBA export: {relative}")
            if asset["role"] == "static_icon":
                require(info["size"][0] == info["size"][1] and info["size"][0] <= 512,
                        f"Static icon must be a purpose-sized square emblem: {relative}")
            elif asset["role"] in ("creative_emblem", "gui_logo", "gui_emblem"):
                frame_width, frame_height = asset["frame_size"]
                width, height = info["size"]
                require(width % frame_width == 0 and height % frame_height == 0
                        and width // frame_width * (height // frame_height) >= asset["frames"],
                        f"Animation frames leave their sheet: {relative}")
                metadata_path = Path(relative + ".mcmeta")
                metadata = json.loads(local_path(root, str(metadata_path)).read_text(encoding="utf-8"))
                declared.add(metadata_path.as_posix())
                if asset["role"] == "creative_emblem":
                    require(frame_width == frame_height == 64 and width == 64,
                            f"Creative emblems must use 64-pixel square frames: {relative}")
                    require(width // frame_width * (height // frame_height) == asset["frames"],
                            f"Creative sheet contains unexpected frames: {relative}")
                    require(metadata.get("animation") == {"frametime": 1, "interpolate": False},
                            f"Creative emblem timing changed: {relative}")
                else:
                    require(metadata == {"texture": {"blur": True, "clamp": True}},
                            f"Manually drawn GUI sheets need filtering and one animation owner: {relative}")
                    require(frame_width <= 512 and frame_height <= 512,
                            f"GUI frames must be purpose-sized exports: {relative}")
                    if asset["role"] == "gui_emblem":
                        require(frame_width == frame_height == 64,
                                f"Compact GUI emblem must use square 64-pixel frames: {relative}")
            else:
                raise ValueError(f"Unknown branding role: {asset['role']}")
        require(info["size"] == asset["size"], f"Image dimensions changed: {relative}")
        checked.append({"path": relative, "role": asset["role"], "bytes": len(data)})

    for relative in contract.get("forbidden", []):
        require(not local_path(root, relative).exists(), f"Unused full-resolution master in runtime: {relative}")
    for relative in contract.get("runtime_roots", []):
        directory = local_path(root, relative)
        require(directory.is_dir(), f"Missing runtime resource root: {relative}")
        for pattern in ("*/logo.*", "*/icon.*", "*/textures/gui/*logo*",
                        "*/textures/gui/*emblem*", "*/textures/item/emblem*"):
            for path in directory.glob(pattern):
                require(path.relative_to(root).as_posix() in declared,
                        f"Undeclared runtime branding asset: {path.relative_to(root).as_posix()}")
    for reference in contract.get("references", []):
        text = local_path(root, reference["path"]).read_text(encoding="utf-8")
        if "pointer" in reference:
            value = json.loads(text)
            for key in reference["pointer"]:
                value = value[key]
            require(value == reference["equals"], f"Branding consumer changed: {reference['path']}")
        else:
            require(re.search(reference["pattern"], text) is not None,
                    f"Branding renderer/reference changed: {reference['path']}")
    return {"status": "ok", "project": contract["project"], "assets": checked}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        print(json.dumps(audit(args.root), sort_keys=True))
    except (ValueError, KeyError, OSError, struct.error) as error:
        parser.exit(1, f"Branding audit failed: {error}\n")


if __name__ == "__main__":
    main()
