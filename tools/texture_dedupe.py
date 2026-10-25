#!/usr/bin/env python3
"""Deterministic inventory and byte-identical texture alias pilot tooling."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import zlib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


TOOL_VERSION = "1.3.1"
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FORMAT = "erydon-texture-aliases"
MANIFEST_RELATIVE_PATH = PurePosixPath("texture_aliases/v1.json")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_DATA_CHUNKS = {b"IDAT", b"fdAT"}
PNG_NON_SEMANTIC_CHUNKS = {b"IEND", b"tIME"}
CSV_FIELDS = [
    "source",
    "tier",
    "path",
    "resource_id",
    "logical_id",
    "namespace",
    "asset_location",
    "dimensions",
    "width",
    "height",
    "mode",
    "bit_depth",
    "file_size",
    "zip_deflate_size_estimate",
    "sha256",
    "rgba_sha256",
    "alpha_min",
    "alpha_max",
    "alpha_varies",
    "png_metadata_sha256",
    "mcmeta_present",
    "mcmeta_sha256",
    "role",
    "is_ctm",
    "animated",
    "intentionally_native_high_res",
    "alias_supported",
    "known_references",
]
REFERENCE_TOKEN = re.compile(r"[A-Za-z0-9_.:/\\-]+")
REFERENCE_SUFFIXES = {
    ".json",
    ".mcmeta",
    ".mtl",
    ".obj",
    ".properties",
    ".txt",
}


class TextureToolError(RuntimeError):
    """A deterministic, user-facing validation failure."""


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    tier: str
    root: Path
    root_label: str
    namespaces: tuple[str, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    role_rules: tuple[tuple[str, str], ...]
    metadata_overrides: Mapping[str, Mapping[str, Any]]
    intentionally_native_high_res: tuple[str, ...]


@dataclass(frozen=True)
class PilotGroup:
    group_id: str
    selected_by_default: bool
    source_id: str
    tier: str
    namespace: str
    role: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ToolConfig:
    path: Path
    sha256: str
    sources: tuple[SourceConfig, ...]
    pilot_groups: tuple[PilotGroup, ...]

    @property
    def source_by_id(self) -> dict[str, SourceConfig]:
        return {source.source_id: source for source in self.sources}

    @property
    def group_by_id(self) -> dict[str, PilotGroup]:
        return {group.group_id: group for group in self.pilot_groups}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def write_bytes_if_changed(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    path.write_bytes(data)


def write_json(path: Path, value: Any) -> None:
    write_bytes_if_changed(path, canonical_json_bytes(value))


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TextureToolError(f"{label} must be a JSON object")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TextureToolError(f"{label} must be a non-empty string")
    return value


def require_sha256(value: Any, label: str) -> str:
    digest = require_string(value, label).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise TextureToolError(f"{label} must be a full lowercase SHA-256")
    return digest


def require_string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise TextureToolError(f"{label} must be an array of non-empty strings")
    return tuple(value)


def normalize_relative_path(value: str, label: str) -> str:
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or ":" in pure.parts[0]
    ):
        raise TextureToolError(f"{label} is not a safe relative path: {value!r}")
    return pure.as_posix()


def normalize_config_root(value: str, label: str) -> str:
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", "."} for part in pure.parts)
        or ":" in pure.parts[0]
    ):
        raise TextureToolError(f"{label} is not a safe relative root: {value!r}")
    return pure.as_posix()


def validate_namespace(value: str, label: str) -> str:
    namespace = require_string(value, label)
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_.-")
    if any(character not in allowed for character in namespace):
        raise TextureToolError(f"{label} is not a valid namespace: {namespace!r}")
    return namespace


def load_config(path: Path) -> ToolConfig:
    config_path = path.resolve()
    raw_bytes = config_path.read_bytes()
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TextureToolError(f"Invalid config JSON {config_path}: {exc}") from exc
    root = require_mapping(raw, "config")
    if root.get("schema_version") != 1:
        raise TextureToolError("config.schema_version must be 1")

    source_items = root.get("sources")
    if not isinstance(source_items, list) or not source_items:
        raise TextureToolError("config.sources must be a non-empty array")
    sources: list[SourceConfig] = []
    source_ids: set[str] = set()
    for index, item in enumerate(source_items):
        label = f"config.sources[{index}]"
        value = require_mapping(item, label)
        source_id = require_string(value.get("id"), f"{label}.id")
        if source_id in source_ids:
            raise TextureToolError(f"Duplicate source id: {source_id}")
        source_ids.add(source_id)
        tier = require_string(value.get("tier"), f"{label}.tier")
        root_label = normalize_config_root(
            require_string(value.get("root"), f"{label}.root"), f"{label}.root"
        )
        source_root = (config_path.parent / Path(root_label)).resolve()
        namespaces = tuple(
            validate_namespace(namespace, f"{label}.namespaces")
            for namespace in require_string_list(
                value.get("namespaces"), f"{label}.namespaces"
            )
        )
        include = require_string_list(value.get("include", []), f"{label}.include")
        exclude = require_string_list(value.get("exclude", []), f"{label}.exclude")
        role_items = value.get("role_rules", [])
        if not isinstance(role_items, list):
            raise TextureToolError(f"{label}.role_rules must be an array")
        role_rules: list[tuple[str, str]] = []
        for rule_index, rule_item in enumerate(role_items):
            rule_label = f"{label}.role_rules[{rule_index}]"
            rule = require_mapping(rule_item, rule_label)
            role_rules.append(
                (
                    require_string(rule.get("glob"), f"{rule_label}.glob"),
                    require_string(rule.get("role"), f"{rule_label}.role"),
                )
            )
        overrides_raw = value.get("metadata_overrides", {})
        overrides = require_mapping(overrides_raw, f"{label}.metadata_overrides")
        normalized_overrides: dict[str, Mapping[str, Any]] = {}
        for override_path, override_value in overrides.items():
            normalized_path = normalize_relative_path(
                require_string(override_path, f"{label}.metadata_overrides key"),
                f"{label}.metadata_overrides key",
            )
            normalized_overrides[normalized_path] = require_mapping(
                override_value, f"{label}.metadata_overrides[{override_path!r}]"
            )
        high_res_raw = value.get("intentionally_native_high_res", [])
        if not isinstance(high_res_raw, list) or not all(
            isinstance(pattern, str) and pattern for pattern in high_res_raw
        ):
            raise TextureToolError(
                f"{label}.intentionally_native_high_res must be an array of strings"
            )
        high_res = tuple(high_res_raw)
        sources.append(
            SourceConfig(
                source_id=source_id,
                tier=tier,
                root=source_root,
                root_label=root_label,
                namespaces=namespaces,
                include=include,
                exclude=exclude,
                role_rules=tuple(role_rules),
                metadata_overrides=normalized_overrides,
                intentionally_native_high_res=high_res,
            )
        )

    group_items = root.get("pilot_groups", [])
    if not isinstance(group_items, list):
        raise TextureToolError("config.pilot_groups must be an array")
    groups: list[PilotGroup] = []
    group_ids: set[str] = set()
    claimed_paths: set[tuple[str, str, str, str]] = set()
    source_by_id = {source.source_id: source for source in sources}
    for index, item in enumerate(group_items):
        label = f"config.pilot_groups[{index}]"
        value = require_mapping(item, label)
        group_id = require_string(value.get("id"), f"{label}.id")
        if group_id in group_ids:
            raise TextureToolError(f"Duplicate pilot group id: {group_id}")
        group_ids.add(group_id)
        selected = value.get("selected_by_default", False)
        if not isinstance(selected, bool):
            raise TextureToolError(f"{label}.selected_by_default must be boolean")
        source_id = require_string(value.get("source"), f"{label}.source")
        if source_id not in source_by_id:
            raise TextureToolError(f"{label}.source is unknown: {source_id}")
        tier = require_string(value.get("tier"), f"{label}.tier")
        source = source_by_id[source_id]
        if tier != source.tier:
            raise TextureToolError(
                f"{label}.tier {tier!r} does not match source tier {source.tier!r}"
            )
        namespace = validate_namespace(value.get("namespace"), f"{label}.namespace")
        if namespace not in source.namespaces:
            raise TextureToolError(
                f"{label}.namespace {namespace!r} is not in source namespaces"
            )
        role = require_string(value.get("role"), f"{label}.role")
        paths = tuple(
            normalize_relative_path(path_value, f"{label}.paths")
            for path_value in require_string_list(value.get("paths"), f"{label}.paths")
        )
        if len(paths) < 2:
            raise TextureToolError(f"{label}.paths must contain at least two PNGs")
        if len(set(paths)) != len(paths):
            raise TextureToolError(f"{label}.paths contains duplicates")
        for relative_path in paths:
            if (
                not relative_path.endswith(".png")
                or not relative_path.startswith(("textures/", "optifine/"))
            ):
                raise TextureToolError(
                    f"{label}.paths must be namespace-relative PNG paths under "
                    "textures/ or optifine/"
                )
            key = (source_id, tier, namespace, relative_path)
            if key in claimed_paths:
                raise TextureToolError(
                    f"Texture path appears in more than one pilot group: {relative_path}"
                )
            claimed_paths.add(key)
        groups.append(
            PilotGroup(
                group_id=group_id,
                selected_by_default=selected,
                source_id=source_id,
                tier=tier,
                namespace=namespace,
                role=role,
                paths=paths,
            )
        )

    return ToolConfig(
        path=config_path,
        sha256=sha256_bytes(raw_bytes),
        sources=tuple(sources),
        pilot_groups=tuple(groups),
    )


def import_pillow() -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise TextureToolError(
            "Pillow is required. Install it with: python -m pip install Pillow"
        ) from exc
    return Image


def parse_png_metadata(data: bytes, label: str) -> tuple[int, list[dict[str, Any]]]:
    if not data.startswith(PNG_SIGNATURE):
        raise TextureToolError(f"Not a PNG file: {label}")
    offset = len(PNG_SIGNATURE)
    bit_depth: int | None = None
    semantic_chunks: list[dict[str, Any]] = []
    chunk_occurrences: dict[str, int] = {}
    found_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise TextureToolError(f"Truncated PNG chunk in {label}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if crc_end > len(data):
            raise TextureToolError(f"Truncated PNG payload in {label}")
        payload = data[payload_start:payload_end]
        expected_crc = struct.unpack(">I", data[payload_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise TextureToolError(f"PNG CRC mismatch in {label}")
        type_name = chunk_type.decode("latin-1")
        occurrence = chunk_occurrences.get(type_name, 0)
        chunk_occurrences[type_name] = occurrence + 1
        if chunk_type == b"IHDR":
            if length != 13:
                raise TextureToolError(f"Invalid IHDR length in {label}")
            bit_depth = payload[8]
        if (
            chunk_type not in PNG_DATA_CHUNKS
            and chunk_type not in PNG_NON_SEMANTIC_CHUNKS
        ):
            semantic_chunks.append(
                {
                    "type": type_name,
                    "occurrence": occurrence,
                    "length": length,
                    "payload_sha256": sha256_bytes(payload),
                }
            )
        offset = crc_end
        if chunk_type == b"IEND":
            found_iend = True
            break
    if bit_depth is None or not found_iend or offset != len(data):
        raise TextureToolError(f"Invalid PNG structure in {label}")
    return bit_depth, semantic_chunks


def glob_matches(path: str, pattern: str) -> bool:
    candidates = {pattern}
    pending = [pattern]
    while pending:
        current = pending.pop()
        marker = current.find("**/")
        if marker < 0:
            continue
        without_directory = current[:marker] + current[marker + 3 :]
        if without_directory not in candidates:
            candidates.add(without_directory)
            pending.append(without_directory)
    return any(fnmatch.fnmatchcase(path, candidate) for candidate in candidates)


def matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(glob_matches(path, pattern) for pattern in patterns)


def role_for(source: SourceConfig, relative_path: str) -> str:
    override = source.metadata_overrides.get(relative_path)
    if override is not None and "role" in override:
        return require_string(
            override["role"], f"metadata override role for {relative_path}"
        )
    for pattern, role in source.role_rules:
        if glob_matches(relative_path, pattern):
            return role
    name = PurePosixPath(relative_path).name
    if name.endswith("_n.png"):
        return "normal"
    if name.endswith("_s.png"):
        return "specular"
    if "/optifine/ctm/" in f"/{relative_path}":
        return "ctm-albedo"
    if relative_path.startswith("textures/gui/"):
        return "ui"
    if relative_path.startswith("textures/item/"):
        return "item"
    if relative_path.startswith("textures/entity/"):
        return "entity"
    if relative_path.startswith("textures/particle/"):
        return "effect"
    if relative_path.startswith(("textures/", "optifine/")):
        return "albedo"
    return "unknown"


def logical_id(namespace: str, relative_path: str) -> str:
    if (
        not relative_path.endswith(".png")
        or not relative_path.startswith(("textures/", "optifine/"))
    ):
        raise TextureToolError(f"Cannot derive logical ID from {relative_path}")
    logical_path = relative_path[: -len(".png")]
    if logical_path.startswith("textures/"):
        logical_path = logical_path[len("textures/") :]
    return f"{namespace}:{logical_path}"


def resource_id(namespace: str | None, relative_path: str) -> str:
    if namespace is None:
        return relative_path
    return f"{namespace}:{relative_path}"


def is_alias_supported(namespace: str | None, relative_path: str) -> bool:
    return (
        namespace is not None
        and relative_path.endswith(".png")
        and relative_path.startswith(("textures/", "optifine/"))
    )


def inspect_png(
    source: SourceConfig,
    namespace: str | None,
    relative_path: str,
    absolute_path: Path,
) -> dict[str, Any]:
    Image = import_pillow()
    data = absolute_path.read_bytes()
    bit_depth, semantic_chunks = parse_png_metadata(data, str(absolute_path))
    try:
        with Image.open(io.BytesIO(data)) as image:
            mode = image.mode
            width, height = image.size
            frame_count = int(getattr(image, "n_frames", 1))
            rgba = image.convert("RGBA")
            rgba_bytes = rgba.tobytes()
            alpha_min, alpha_max = rgba.getchannel("A").getextrema()
    except Exception as exc:
        raise TextureToolError(f"Cannot decode PNG {absolute_path}: {exc}") from exc

    mcmeta_path = absolute_path.with_name(absolute_path.name + ".mcmeta")
    mcmeta_present = mcmeta_path.is_file()
    mcmeta_sha = sha256_file(mcmeta_path) if mcmeta_present else None
    mcmeta_animated = False
    if mcmeta_present:
        try:
            mcmeta = json.loads(mcmeta_path.read_text(encoding="utf-8-sig"))
            mcmeta_animated = isinstance(mcmeta, dict) and "animation" in mcmeta
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TextureToolError(f"Invalid mcmeta JSON {mcmeta_path}: {exc}") from exc

    alias_supported = is_alias_supported(namespace, relative_path)
    role = role_for(source, relative_path) if namespace is not None else "ui"
    animated = frame_count > 1 or mcmeta_animated
    if animated:
        role = "animation"
    return {
        "source": source.source_id,
        "tier": source.tier,
        "path": relative_path,
        "resource_id": resource_id(namespace, relative_path),
        "logical_id": logical_id(namespace, relative_path) if alias_supported else None,
        "namespace": namespace,
        "asset_location": "namespace" if namespace is not None else "pack-root",
        "dimensions": [width, height],
        "width": width,
        "height": height,
        "mode": mode,
        "bit_depth": bit_depth,
        "file_size": len(data),
        "zip_deflate_size_estimate": len(zlib.compress(data, level=9)),
        "sha256": sha256_bytes(data),
        "rgba_sha256": sha256_bytes(rgba_bytes),
        "alpha_min": alpha_min,
        "alpha_max": alpha_max,
        "alpha_varies": alpha_min != alpha_max,
        "png_metadata_sha256": sha256_bytes(
            json.dumps(
                semantic_chunks, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ),
        "mcmeta_present": mcmeta_present,
        "mcmeta_sha256": mcmeta_sha,
        "role": role,
        "is_ctm": "/optifine/ctm/" in f"/{relative_path}",
        "animated": animated,
        "intentionally_native_high_res": matches_any(
            relative_path, source.intentionally_native_high_res
        ),
        "alias_supported": alias_supported,
        "known_references": (
            ["implicit:resource-pack-icon"] if namespace is None else []
        ),
    }


def iter_source_pngs(
    source: SourceConfig,
) -> Iterable[tuple[str | None, str, Path]]:
    if not source.root.is_dir():
        raise TextureToolError(f"Configured source root is missing: {source.root}")
    for namespace in sorted(source.namespaces):
        texture_root = source.root / "assets" / namespace
        if not texture_root.is_dir():
            continue
        candidates = sorted(
            (
                path
                for path in texture_root.rglob("*.png")
                if path.is_file()
            ),
            key=lambda item: item.relative_to(texture_root).as_posix(),
        )
        for absolute_path in candidates:
            relative_path = absolute_path.relative_to(texture_root).as_posix()
            if source.include and not matches_any(relative_path, source.include):
                continue
            if source.exclude and matches_any(relative_path, source.exclude):
                continue
            yield namespace, relative_path, absolute_path
    pack_icon = source.root / "pack.png"
    if pack_icon.is_file():
        yield None, "pack.png", pack_icon


def normalize_reference_token(value: str) -> str:
    token = value.replace("\\", "/")
    while token.startswith("../"):
        token = token[3:]
    if token.startswith("./"):
        token = token[2:]
    return token


def reference_tokens(record: Mapping[str, Any]) -> set[str]:
    namespace = record.get("namespace")
    relative_path = str(record["path"])
    if namespace is None:
        return {relative_path}
    tokens = {
        str(record["resource_id"]),
        relative_path,
        relative_path[: -len(".png")],
        f"assets/{namespace}/{relative_path}",
    }
    logical = record.get("logical_id")
    if logical:
        tokens.add(str(logical))
        tokens.add(f"{logical}.png")
    if relative_path.startswith("textures/"):
        texture_path = relative_path[len("textures/") :]
        tokens.add(texture_path)
        tokens.add(texture_path[: -len(".png")])
    return tokens


def iter_reference_files(source: SourceConfig) -> Iterable[Path]:
    for path in sorted(source.root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        if path.suffix.lower() in REFERENCE_SUFFIXES:
            yield path


def read_reference_text(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def add_ctm_property_references(
    source: SourceConfig,
    record: Mapping[str, Any],
    properties_by_parent: Mapping[tuple[str, str], Sequence[str]],
    references: set[str],
) -> None:
    namespace = record.get("namespace")
    if namespace is None or not bool(record["is_ctm"]):
        return
    parent = PurePosixPath(str(record["path"])).parent
    parent_variants = [parent]
    parent_text = parent.as_posix()
    if parent_text.startswith("textures/optifine/"):
        parent_variants.append(
            PurePosixPath(parent_text[len("textures/") :])
        )
    elif parent_text.startswith("optifine/"):
        parent_variants.append(PurePosixPath(f"textures/{parent_text}"))

    visited: set[str] = set()
    for variant in parent_variants:
        current = variant
        while "/optifine/ctm" in f"/{current.as_posix()}":
            current_text = current.as_posix()
            if current_text in visited:
                break
            visited.add(current_text)
            references.update(
                properties_by_parent.get((str(namespace), current_text), ())
            )
            if current.name == "ctm" or current == current.parent:
                break
            current = current.parent


def attach_known_references(
    source: SourceConfig, records: Sequence[dict[str, Any]]
) -> None:
    candidate_records: dict[str, set[int]] = {}
    for index, record in enumerate(records):
        for token in reference_tokens(record):
            candidate_records.setdefault(token, set()).add(index)

    references_by_record: list[set[str]] = [
        set(str(item) for item in record.get("known_references", []))
        for record in records
    ]
    properties_by_parent: dict[tuple[str, str], list[str]] = {}
    for reference_file in iter_reference_files(source):
        relative_reference = reference_file.relative_to(source.root).as_posix()
        parts = PurePosixPath(relative_reference).parts
        if (
            reference_file.suffix.lower() == ".properties"
            and len(parts) >= 4
            and parts[0] == "assets"
        ):
            namespace = parts[1]
            namespace_relative = PurePosixPath(*parts[2:])
            properties_by_parent.setdefault(
                (namespace, namespace_relative.parent.as_posix()), []
            ).append(relative_reference)

        text = read_reference_text(reference_file)
        matched_indices: set[int] = set()
        for raw_token in REFERENCE_TOKEN.findall(text):
            token = normalize_reference_token(raw_token)
            matched_indices.update(candidate_records.get(token, ()))
        for index in matched_indices:
            references_by_record[index].add(relative_reference)

    for index, record in enumerate(records):
        references = references_by_record[index]
        add_ctm_property_references(
            source, record, properties_by_parent, references
        )
        logical = record.get("logical_id")
        path = str(record["path"])
        if logical and path.endswith(("_n.png", "_s.png")):
            references.add(f"implicit:labpbr-sidecar-of:{str(logical)[:-2]}")
        if record["role"] == "albedo" and any(
            reference.lower().endswith(".mtl") for reference in references
        ):
            record["role"] = "obj-material"
        record["known_references"] = sorted(references)


def inventory_records(config: ToolConfig) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in sorted(
        config.sources, key=lambda item: (item.source_id, item.tier, item.root_label)
    ):
        source_records: list[dict[str, Any]] = []
        for namespace, relative_path, absolute_path in iter_source_pngs(source):
            source_records.append(
                inspect_png(source, namespace, relative_path, absolute_path)
            )
        attach_known_references(source, source_records)
        records.extend(source_records)
    records.sort(
        key=lambda record: (
            record["source"],
            record["tier"],
            str(record["namespace"] or ""),
            record["path"],
        )
    )
    return records


def inventory_document(
    config: ToolConfig, records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "texture_inventory",
        "tool_version": TOOL_VERSION,
        "config_sha256": config.sha256,
        "record_count": len(records),
        "records": list(records),
    }


def csv_value(field: str, value: Any) -> Any:
    if field == "dimensions" and isinstance(value, list) and len(value) == 2:
        return f"{value[0]}x{value[1]}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return value


def write_inventory_csv(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=CSV_FIELDS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for record in records:
        writer.writerow(
            {field: csv_value(field, record.get(field)) for field in CSV_FIELDS}
        )
    write_bytes_if_changed(path, buffer.getvalue().encode("utf-8"))


def selected_groups(
    config: ToolConfig, requested_ids: Sequence[str]
) -> tuple[PilotGroup, ...]:
    group_by_id = config.group_by_id
    if requested_ids:
        unknown = sorted(set(requested_ids) - set(group_by_id))
        if unknown:
            raise TextureToolError(f"Unknown pilot group(s): {', '.join(unknown)}")
        ids = sorted(set(requested_ids))
        return tuple(group_by_id[group_id] for group_id in ids)
    defaults = tuple(
        sorted(
            (
                group
                for group in config.pilot_groups
                if group.selected_by_default
            ),
            key=lambda group: group.group_id,
        )
    )
    if not defaults:
        raise TextureToolError(
            "No --group was supplied and no pilot group is selected by default"
        )
    return defaults


def selected_records(
    config: ToolConfig,
    groups: Sequence[PilotGroup],
    records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if records is not None:
        by_key = {
            (
                str(record["source"]),
                str(record["tier"]),
                str(record["namespace"]),
                str(record["path"]),
            ): dict(record)
            for record in records
        }
    else:
        by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        source_by_id = config.source_by_id
        for group in groups:
            source = source_by_id[group.source_id]
            for relative_path in group.paths:
                key = (
                    group.source_id,
                    group.tier,
                    group.namespace,
                    relative_path,
                )
                if key in by_key:
                    continue
                absolute_path = (
                    source.root
                    / "assets"
                    / group.namespace
                    / Path(relative_path)
                )
                if not absolute_path.is_file():
                    continue
                if source.include and not matches_any(relative_path, source.include):
                    raise TextureToolError(
                        f"{group.group_id}: configured PNG is excluded by source "
                        f"include rules: {group.namespace}/{relative_path}"
                    )
                if source.exclude and matches_any(relative_path, source.exclude):
                    raise TextureToolError(
                        f"{group.group_id}: configured PNG is excluded by source "
                        f"exclude rules: {group.namespace}/{relative_path}"
                    )
                by_key[key] = inspect_png(
                    source,
                    group.namespace,
                    relative_path,
                    absolute_path,
                )
    selected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for group in groups:
        group_hashes: set[str] = set()
        for relative_path in group.paths:
            key = (group.source_id, group.tier, group.namespace, relative_path)
            record = by_key.get(key)
            if record is None:
                raise TextureToolError(
                    f"{group.group_id}: configured PNG is absent from inventory: "
                    f"{group.namespace}/{relative_path}"
                )
            if record["role"] != group.role:
                raise TextureToolError(
                    f"{group.group_id}: {relative_path} role is {record['role']!r}, "
                    f"expected {group.role!r}"
                )
            group_hashes.add(str(record["sha256"]))
            selected[key] = record
        if len(group_hashes) != 1:
            detail = ", ".join(sorted(group_hashes))
            raise TextureToolError(
                f"{group.group_id} is not byte-identical; SHA-256 values: {detail}"
            )
    return selected


def manifest_path(stage_root: Path, namespace: str) -> Path:
    return (
        stage_root
        / "assets"
        / namespace
        / Path(MANIFEST_RELATIVE_PATH.as_posix())
    )


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TextureToolError(f"Invalid manifest JSON {path}: {exc}") from exc
    return dict(require_mapping(value, f"manifest {path}"))


def alias_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    namespace = str(record["namespace"])
    file_sha = str(record["sha256"])
    return {
        "file_sha256": file_sha,
        "logical_id": str(record["logical_id"]),
        "mcmeta_present": bool(record["mcmeta_present"]),
        "mcmeta_sha256": record["mcmeta_sha256"],
        "path": str(record["path"]),
        "role": str(record["role"]),
        "source": str(record["source"]),
        "target": f"assets/{namespace}/texture_blobs/{file_sha}.png",
        "tier": str(record["tier"]),
    }


def build_manifest(
    namespace: str,
    tier: str,
    aliases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    sorted_aliases = sorted(
        (dict(alias) for alias in aliases),
        key=lambda alias: (str(alias["path"]), str(alias["logical_id"])),
    )
    blob_by_target: dict[str, dict[str, Any]] = {}
    for alias in sorted_aliases:
        target = str(alias["target"])
        file_sha = str(alias["file_sha256"])
        blob_by_target[target] = {
            "file_sha256": file_sha,
            "target": target,
        }
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "format": MANIFEST_FORMAT,
        "namespace": namespace,
        "tier": tier,
        "aliases": sorted_aliases,
        "blobs": [blob_by_target[target] for target in sorted(blob_by_target)],
    }


def ensure_staged_root(config: ToolConfig, stage_root: Path) -> Path:
    resolved = stage_root.resolve()
    if not resolved.is_dir():
        raise TextureToolError(f"Staged resource root is missing: {resolved}")
    for source in config.sources:
        if (
            resolved == source.root
            or resolved.is_relative_to(source.root)
            or source.root.is_relative_to(resolved)
        ):
            raise TextureToolError(
                "Refusing a staged root that overlaps configured source root "
                f"{source.root}: {resolved}"
            )
    return resolved


def load_existing_aliases(
    path: Path, namespace: str, tier: str
) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    manifest = read_manifest(path)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise TextureToolError(f"Unsupported manifest schema in {path}")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise TextureToolError(f"Unsupported manifest format in {path}")
    if manifest.get("namespace") != namespace:
        raise TextureToolError(f"Manifest namespace mismatch in {path}")
    if manifest.get("tier") != tier:
        raise TextureToolError(
            f"Cross-tier apply is forbidden: {path} is {manifest.get('tier')!r}, "
            f"requested {tier!r}"
        )
    aliases = manifest.get("aliases")
    if not isinstance(aliases, list):
        raise TextureToolError(f"Manifest aliases must be an array in {path}")
    by_path: dict[str, dict[str, Any]] = {}
    for item in aliases:
        alias = dict(require_mapping(item, f"alias in {path}"))
        alias_path = normalize_relative_path(
            require_string(alias.get("path"), f"alias path in {path}"),
            f"alias path in {path}",
        )
        if alias_path in by_path:
            raise TextureToolError(f"Duplicate alias path in {path}: {alias_path}")
        by_path[alias_path] = alias
    return by_path


def command_inventory(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    records = inventory_records(config)
    document = inventory_document(config, records)
    write_json(args.json_out, document)
    write_inventory_csv(args.csv_out, records)
    print(
        json.dumps(
            {
                "status": "ok",
                "records": len(records),
                "json": str(args.json_out),
                "csv": str(args.csv_out),
            },
            sort_keys=True,
        )
    )
    return 0


def load_inventory_records(path: Path) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TextureToolError(f"Invalid inventory JSON {path}: {exc}") from exc
    root = require_mapping(document, "inventory")
    if root.get("schema_version") != 1 or root.get("kind") != "texture_inventory":
        raise TextureToolError(f"Unsupported texture inventory document: {path}")
    raw_records = root.get("records")
    if not isinstance(raw_records, list):
        raise TextureToolError(f"Inventory records must be an array: {path}")
    records = [
        dict(require_mapping(record, f"inventory.records[{index}]"))
        for index, record in enumerate(raw_records)
    ]
    required = {
        "source",
        "tier",
        "namespace",
        "path",
        "resource_id",
        "logical_id",
        "asset_location",
        "dimensions",
        "mode",
        "bit_depth",
        "file_size",
        "zip_deflate_size_estimate",
        "sha256",
        "rgba_sha256",
        "png_metadata_sha256",
        "mcmeta_present",
        "mcmeta_sha256",
        "role",
        "is_ctm",
        "animated",
        "intentionally_native_high_res",
        "alias_supported",
        "known_references",
    }
    for index, record in enumerate(records):
        missing = sorted(required - set(record))
        if missing:
            raise TextureToolError(
                f"Inventory record {index} is missing fields: {', '.join(missing)}"
            )
    return root, records


def duplicate_member(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": record["source"],
        "tier": record["tier"],
        "namespace": record["namespace"],
        "path": record["path"],
        "resource_id": record["resource_id"],
        "logical_id": record["logical_id"],
        "role": record["role"],
        "is_ctm": record["is_ctm"],
        "file_size": record["file_size"],
        "sha256": record["sha256"],
        "rgba_sha256": record["rgba_sha256"],
        "mcmeta_present": record["mcmeta_present"],
        "mcmeta_sha256": record["mcmeta_sha256"],
        "animated": record["animated"],
        "intentionally_native_high_res": record[
            "intentionally_native_high_res"
        ],
        "alias_supported": record["alias_supported"],
    }


def duplicate_exclusion_reasons(records: Sequence[Mapping[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if any(not bool(record["alias_supported"]) for record in records):
        reasons.append("unsupported-resource-location")
    if any(bool(record["animated"]) for record in records):
        reasons.append("animated")
    if any(bool(record["mcmeta_present"]) for record in records):
        reasons.append("mcmeta-backed")
    if any(bool(record["intentionally_native_high_res"]) for record in records):
        reasons.append("intentional-native-high-resolution")
    return reasons


def duplicate_method(records: Sequence[Mapping[str, Any]]) -> str:
    # A model-reference rewrite changes which third-party override path wins and
    # can also change implicit LabPBR sidecar discovery. Exact bytes alone do not
    # prove that rewrite safe, so the initial rollout keeps every logical path.
    return "virtual-alias"


def add_group_aggregate(
    aggregates: dict[str, dict[str, int]],
    key: str,
    redundant_files: int,
    removable_bytes: int,
    removable_deflate_bytes: int,
) -> None:
    aggregate = aggregates.setdefault(
        key,
        {
            "group_count": 0,
            "redundant_file_count": 0,
            "removable_file_bytes": 0,
            "removable_deflate_bytes_estimate": 0,
        },
    )
    aggregate["group_count"] += 1
    aggregate["redundant_file_count"] += redundant_files
    aggregate["removable_file_bytes"] += removable_bytes
    aggregate["removable_deflate_bytes_estimate"] += removable_deflate_bytes


def duplicate_analysis_document(
    inventory: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    exact_by_scope: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    exact_by_hash: dict[str, list[Mapping[str, Any]]] = {}
    decoded_by_scope: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for record in records:
        scope_key = (
            str(record["source"]),
            str(record["tier"]),
            str(record["namespace"] or "(pack-root)"),
            str(record["sha256"]),
        )
        exact_by_scope.setdefault(scope_key, []).append(record)
        exact_by_hash.setdefault(str(record["sha256"]), []).append(record)
        decoded_key = (
            str(record["source"]),
            str(record["tier"]),
            str(record["namespace"] or "(pack-root)"),
            tuple(record["dimensions"]),
            str(record["mode"]),
            int(record["bit_depth"]),
            str(record["rgba_sha256"]),
            str(record["png_metadata_sha256"]),
            bool(record["mcmeta_present"]),
            record["mcmeta_sha256"],
        )
        decoded_by_scope.setdefault(decoded_key, []).append(record)

    exact_groups: list[dict[str, Any]] = []
    by_source: dict[str, dict[str, int]] = {}
    by_tier: dict[str, dict[str, int]] = {}
    by_namespace: dict[str, dict[str, int]] = {}
    by_role: dict[str, dict[str, int]] = {}
    by_asset_class: dict[str, dict[str, int]] = {}
    eligible_by_source: dict[str, dict[str, int]] = {}
    eligible_by_tier: dict[str, dict[str, int]] = {}
    eligible_by_namespace: dict[str, dict[str, int]] = {}
    eligible_by_role: dict[str, dict[str, int]] = {}
    eligible_by_asset_class: dict[str, dict[str, int]] = {}
    excluded_by_reason: dict[str, dict[str, int]] = {}
    exact_redundant_files = 0
    exact_removable_bytes = 0
    exact_removable_deflate_bytes = 0
    eligible_redundant_files = 0
    eligible_removable_bytes = 0
    eligible_removable_deflate_bytes = 0

    for key, raw_group in exact_by_scope.items():
        if len(raw_group) < 2:
            continue
        group = sorted(
            raw_group,
            key=lambda record: (
                str(record["role"]),
                str(record["path"]),
            ),
        )
        source, tier, namespace, file_sha = key
        redundant_files = len(group) - 1
        removable_bytes = sum(int(record["file_size"]) for record in group[1:])
        removable_deflate_bytes = sum(
            int(record["zip_deflate_size_estimate"]) for record in group[1:]
        )
        reasons = duplicate_exclusion_reasons(group)
        roles = sorted({str(record["role"]) for record in group})
        role_key = roles[0] if len(roles) == 1 else "mixed:" + ",".join(roles)
        ctm_states = {bool(record["is_ctm"]) for record in group}
        asset_class = (
            ("ctm-" if ctm_states == {True} else "non-ctm-")
            + role_key
            if len(ctm_states) == 1
            else "mixed-ctm-location:" + role_key
        )
        eligible = not reasons
        exact_redundant_files += redundant_files
        exact_removable_bytes += removable_bytes
        exact_removable_deflate_bytes += removable_deflate_bytes
        add_group_aggregate(
            by_source,
            source,
            redundant_files,
            removable_bytes,
            removable_deflate_bytes,
        )
        add_group_aggregate(
            by_tier,
            tier,
            redundant_files,
            removable_bytes,
            removable_deflate_bytes,
        )
        add_group_aggregate(
            by_namespace,
            namespace,
            redundant_files,
            removable_bytes,
            removable_deflate_bytes,
        )
        add_group_aggregate(
            by_role,
            role_key,
            redundant_files,
            removable_bytes,
            removable_deflate_bytes,
        )
        add_group_aggregate(
            by_asset_class,
            asset_class,
            redundant_files,
            removable_bytes,
            removable_deflate_bytes,
        )
        if eligible:
            eligible_redundant_files += redundant_files
            eligible_removable_bytes += removable_bytes
            eligible_removable_deflate_bytes += removable_deflate_bytes
            add_group_aggregate(
                eligible_by_source,
                source,
                redundant_files,
                removable_bytes,
                removable_deflate_bytes,
            )
            add_group_aggregate(
                eligible_by_tier,
                tier,
                redundant_files,
                removable_bytes,
                removable_deflate_bytes,
            )
            add_group_aggregate(
                eligible_by_namespace,
                namespace,
                redundant_files,
                removable_bytes,
                removable_deflate_bytes,
            )
            add_group_aggregate(
                eligible_by_role,
                role_key,
                redundant_files,
                removable_bytes,
                removable_deflate_bytes,
            )
            add_group_aggregate(
                eligible_by_asset_class,
                asset_class,
                redundant_files,
                removable_bytes,
                removable_deflate_bytes,
            )
        else:
            for reason in reasons:
                add_group_aggregate(
                    excluded_by_reason,
                    reason,
                    redundant_files,
                    removable_bytes,
                    removable_deflate_bytes,
                )
        first = group[0]
        exact_groups.append(
            {
                "source": source,
                "tier": tier,
                "namespace": namespace,
                "sha256": file_sha,
                "rgba_sha256": first["rgba_sha256"],
                "dimensions": first["dimensions"],
                "mode": first["mode"],
                "bit_depth": first["bit_depth"],
                "roles": roles,
                "asset_class": asset_class,
                "file_count": len(group),
                "redundant_file_count": redundant_files,
                "removable_file_bytes": removable_bytes,
                "removable_deflate_bytes_estimate": removable_deflate_bytes,
                "eligible_for_byte_alias_rollout": eligible,
                "exclusion_reasons": reasons,
                "recommended_method": (
                    duplicate_method(group) if eligible else "exclude"
                ),
                "members": [duplicate_member(record) for record in group],
            }
        )

    exact_groups.sort(
        key=lambda group: (
            -int(group["removable_file_bytes"]),
            str(group["source"]),
            str(group["namespace"]),
            str(group["sha256"]),
        )
    )

    decoded_only_groups: list[dict[str, Any]] = []
    for key, raw_group in decoded_by_scope.items():
        file_hashes = {str(record["sha256"]) for record in raw_group}
        if len(raw_group) < 2 or len(file_hashes) < 2:
            continue
        group = sorted(raw_group, key=lambda record: str(record["path"]))
        smallest = min(int(record["file_size"]) for record in group)
        decoded_only_groups.append(
            {
                "source": key[0],
                "tier": key[1],
                "namespace": key[2],
                "dimensions": list(key[3]),
                "mode": key[4],
                "bit_depth": key[5],
                "rgba_sha256": key[6],
                "png_metadata_sha256": key[7],
                "mcmeta_present": key[8],
                "mcmeta_sha256": key[9],
                "file_count": len(group),
                "encoded_bytes_removable_estimate": (
                    sum(int(record["file_size"]) for record in group) - smallest
                ),
                "eligible_for_byte_alias_rollout": False,
                "exclusion_reasons": [
                    "byte-different-requires-separate-decoded-equivalence-pilot"
                ]
                + duplicate_exclusion_reasons(group),
                "members": [duplicate_member(record) for record in group],
            }
        )
    decoded_only_groups.sort(
        key=lambda group: (
            -int(group["encoded_bytes_removable_estimate"]),
            str(group["source"]),
            str(group["namespace"]),
            str(group["rgba_sha256"]),
        )
    )

    cross_scope_groups: list[dict[str, Any]] = []
    for file_sha, raw_group in exact_by_hash.items():
        scopes = {
            (
                str(record["source"]),
                str(record["tier"]),
                str(record["namespace"] or "(pack-root)"),
            )
            for record in raw_group
        }
        if len(scopes) < 2:
            continue
        group = sorted(
            raw_group,
            key=lambda record: (
                str(record["source"]),
                str(record["namespace"]),
                str(record["path"]),
            ),
        )
        scope_file_counts: dict[tuple[str, str, str], int] = {}
        for record in group:
            scope = (
                str(record["source"]),
                str(record["tier"]),
                str(record["namespace"] or "(pack-root)"),
            )
            scope_file_counts[scope] = scope_file_counts.get(scope, 0) + 1
        sample_limit = 12
        cross_scope_groups.append(
            {
                "sha256": file_sha,
                "file_count": len(group),
                "scope_count": len(scopes),
                "scopes": [
                    {
                        "source": source,
                        "tier": tier,
                        "namespace": namespace,
                        "file_count": scope_file_counts[
                            (source, tier, namespace)
                        ],
                    }
                    for source, tier, namespace in sorted(scopes)
                ],
                "report_only": True,
                "reason": "standalone mods and namespace/tier boundaries must be preserved",
                "member_samples": [
                    duplicate_member(record) for record in group[:sample_limit]
                ],
                "members_omitted": max(0, len(group) - sample_limit),
            }
        )
    cross_scope_groups.sort(
        key=lambda group: (
            -int(group["file_count"]),
            str(group["sha256"]),
        )
    )

    return {
        "schema_version": 1,
        "kind": "texture_duplicate_analysis",
        "tool_version": TOOL_VERSION,
        "inventory_config_sha256": inventory.get("config_sha256"),
        "summary": {
            "png_record_count": len(records),
            "physical_file_bytes": sum(
                int(record["file_size"]) for record in records
            ),
            "exact_scope_duplicate_group_count": len(exact_groups),
            "exact_scope_redundant_file_count": exact_redundant_files,
            "exact_scope_removable_file_bytes": exact_removable_bytes,
            "exact_scope_removable_deflate_bytes_estimate": (
                exact_removable_deflate_bytes
            ),
            "eligible_exact_group_count": sum(
                1
                for group in exact_groups
                if group["eligible_for_byte_alias_rollout"]
            ),
            "eligible_redundant_file_count": eligible_redundant_files,
            "eligible_removable_file_bytes": eligible_removable_bytes,
            "eligible_removable_deflate_bytes_estimate": (
                eligible_removable_deflate_bytes
            ),
            "decoded_equivalent_byte_different_group_count": len(
                decoded_only_groups
            ),
            "cross_scope_exact_group_count": len(cross_scope_groups),
        },
        "exact_totals": {
            "by_source": dict(sorted(by_source.items())),
            "by_tier": dict(sorted(by_tier.items())),
            "by_namespace": dict(sorted(by_namespace.items())),
            "by_role": dict(sorted(by_role.items())),
            "by_asset_class": dict(sorted(by_asset_class.items())),
        },
        "eligible_exact_totals": {
            "by_source": dict(sorted(eligible_by_source.items())),
            "by_tier": dict(sorted(eligible_by_tier.items())),
            "by_namespace": dict(sorted(eligible_by_namespace.items())),
            "by_role": dict(sorted(eligible_by_role.items())),
            "by_asset_class": dict(
                sorted(eligible_by_asset_class.items())
            ),
        },
        "excluded_exact_totals": {
            "by_reason": dict(sorted(excluded_by_reason.items()))
        },
        "exact_scope_groups": exact_groups,
        "decoded_equivalent_byte_different_groups": decoded_only_groups,
        "cross_scope_exact_groups": cross_scope_groups,
    }


def command_analyze(args: argparse.Namespace) -> int:
    inventory, records = load_inventory_records(args.inventory)
    report = duplicate_analysis_document(inventory, records)
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                **report["summary"],
            },
            sort_keys=True,
        )
    )
    return 0


def source_config_json(source: SourceConfig, output_parent: Path) -> dict[str, Any]:
    relative_root = os.path.relpath(source.root, output_parent).replace("\\", "/")
    return {
        "id": source.source_id,
        "tier": source.tier,
        "root": relative_root,
        "namespaces": list(source.namespaces),
        "include": list(source.include),
        "exclude": list(source.exclude),
        "role_rules": [
            {"glob": pattern, "role": role}
            for pattern, role in source.role_rules
        ],
        "metadata_overrides": {
            path: dict(value)
            for path, value in sorted(source.metadata_overrides.items())
        },
        "intentionally_native_high_res": list(
            source.intentionally_native_high_res
        ),
    }


def generated_plan_config(
    config: ToolConfig,
    inventory_path: Path,
    inventory: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    source_id: str,
    output_path: Path,
    included_roles: set[str],
) -> dict[str, Any]:
    if inventory.get("config_sha256") != config.sha256:
        raise TextureToolError(
            "Inventory was not generated from the supplied family config"
        )
    source = config.source_by_id.get(source_id)
    if source is None:
        raise TextureToolError(f"Unknown plan source: {source_id}")

    by_hash: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in records:
        if str(record["source"]) != source_id:
            continue
        if not bool(record["alias_supported"]):
            continue
        namespace = record.get("namespace")
        if namespace is None:
            continue
        by_hash.setdefault(
            (str(namespace), str(record["sha256"])), []
        ).append(record)

    groups: list[dict[str, Any]] = []
    excluded_by_reason: dict[str, int] = {}
    gross_removable_bytes = 0
    gross_removable_deflate_bytes = 0
    alias_count = 0
    for (namespace, file_sha), raw_group in sorted(by_hash.items()):
        if len(raw_group) < 2:
            continue
        group = sorted(raw_group, key=lambda record: str(record["path"]))
        reasons = duplicate_exclusion_reasons(group)
        roles = {str(record["role"]) for record in group}
        if len(roles) != 1:
            reasons.append("mixed-role")
        if not roles.issubset(included_roles):
            reasons.append("role-not-selected")
        if reasons:
            for reason in sorted(set(reasons)):
                excluded_by_reason[reason] = (
                    excluded_by_reason.get(reason, 0) + 1
                )
            continue
        role = next(iter(roles))
        groups.append(
            {
                "id": f"exact-{namespace}-{file_sha}",
                "selected_by_default": True,
                "source": source.source_id,
                "tier": source.tier,
                "namespace": namespace,
                "role": role,
                "paths": [str(record["path"]) for record in group],
            }
        )
        alias_count += len(group)
        gross_removable_bytes += sum(
            int(record["file_size"]) for record in group[1:]
        )
        gross_removable_deflate_bytes += sum(
            int(record["zip_deflate_size_estimate"]) for record in group[1:]
        )

    groups.sort(key=lambda group: str(group["id"]))
    return {
        "schema_version": 1,
        "plan_metadata": {
            "kind": "generated_texture_alias_plan",
            "tool_version": TOOL_VERSION,
            "inventory_sha256": sha256_file(inventory_path),
            "inventory_config_sha256": inventory.get("config_sha256"),
            "source": source.source_id,
            "tier": source.tier,
            "method": "virtual-alias",
            "exact_file_bytes_only": True,
            "included_roles": sorted(included_roles),
            "group_count": len(groups),
            "alias_count": alias_count,
            "canonical_blob_count": len(groups),
            "gross_removable_file_bytes": gross_removable_bytes,
            "gross_removable_deflate_bytes_estimate": (
                gross_removable_deflate_bytes
            ),
            "excluded_group_counts_by_reason": dict(
                sorted(excluded_by_reason.items())
            ),
        },
        "sources": [
            source_config_json(source, output_path.resolve().parent)
        ],
        "pilot_groups": groups,
    }


def command_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    inventory, records = load_inventory_records(args.inventory)
    included_roles = set(
        args.include_role
        or ["albedo", "ctm-albedo", "normal", "obj-material", "specular"]
    )
    plan = generated_plan_config(
        config,
        args.inventory,
        inventory,
        records,
        args.source,
        args.output,
        included_roles,
    )
    write_json(args.output, plan)
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                **plan["plan_metadata"],
            },
            sort_keys=True,
        )
    )
    return 0


def command_apply(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    groups = selected_groups(config, args.group)
    source_ids = {group.source_id for group in groups}
    if len(source_ids) != 1:
        raise TextureToolError(
            "A single staged resource root cannot mix source roots"
        )
    tiers = {group.tier for group in groups}
    if len(tiers) != 1:
        raise TextureToolError(
            "A single staged resource root cannot mix pilot groups from multiple tiers"
        )
    stage_root = ensure_staged_root(config, args.stage_root)
    records = selected_records(config, groups)
    unsupported_metadata = [
        f"{record['namespace']}:{record['path']}"
        for record in records.values()
        if record["mcmeta_present"] or record["animated"]
    ]
    if unsupported_metadata:
        raise TextureToolError(
            "Pilot manifest schema v1 deliberately excludes animated or "
            "mcmeta-backed resources: " + ", ".join(sorted(unsupported_metadata))
        )
    source_by_id = config.source_by_id
    aliases_by_namespace: dict[str, list[dict[str, Any]]] = {}
    merged_by_namespace: dict[str, dict[str, dict[str, Any]]] = {}
    tier_by_namespace: dict[str, str] = {}
    plans: list[tuple[Path, Path, Path]] = []

    for key in sorted(records):
        record = records[key]
        namespace = str(record["namespace"])
        tier = str(record["tier"])
        relative_path = str(record["path"])
        stage_png = stage_root / "assets" / namespace / Path(relative_path)
        source = source_by_id[str(record["source"])]
        baseline_png = source.root / "assets" / namespace / Path(relative_path)
        manifest_file = manifest_path(stage_root, namespace)
        existing_tier = tier_by_namespace.get(namespace)
        if existing_tier is not None and existing_tier != tier:
            raise TextureToolError(
                f"Cross-tier aliases are forbidden in namespace {namespace}"
            )
        tier_by_namespace[namespace] = tier
        if namespace not in merged_by_namespace:
            merged_by_namespace[namespace] = load_existing_aliases(
                manifest_file, namespace, tier
            )
        existing = merged_by_namespace[namespace]
        expected_alias = alias_entry(record)
        existing_alias = existing.get(relative_path)
        if existing_alias is not None and existing_alias != expected_alias:
            raise TextureToolError(
                f"Conflicting existing alias for {namespace}/{relative_path}"
            )

        if stage_png.exists():
            staged_sha = sha256_file(stage_png)
            if staged_sha != record["sha256"]:
                raise TextureToolError(
                    f"Staged PNG differs from baseline: {stage_png} "
                    f"({staged_sha} != {record['sha256']})"
                )
        elif existing_alias != expected_alias:
            raise TextureToolError(
                f"Staged PNG is missing and no matching alias exists: {stage_png}"
            )

        staged_mcmeta = stage_png.with_name(stage_png.name + ".mcmeta")
        if staged_mcmeta.is_file() != bool(record["mcmeta_present"]):
            raise TextureToolError(
                f"Staged mcmeta presence differs from baseline: {staged_mcmeta}"
            )
        if staged_mcmeta.is_file() and sha256_file(staged_mcmeta) != record[
            "mcmeta_sha256"
        ]:
            raise TextureToolError(
                f"Staged mcmeta differs from baseline: {staged_mcmeta}"
            )

        target_relative = PurePosixPath(str(expected_alias["target"]))
        target = stage_root / Path(target_relative.as_posix())
        if target.exists() and sha256_file(target) != record["sha256"]:
            raise TextureToolError(f"Existing blob hash mismatch: {target}")
        existing[relative_path] = expected_alias
        aliases_by_namespace.setdefault(namespace, []).append(expected_alias)
        plans.append((baseline_png, stage_png, target))

    # Nothing in the stage is mutated until every selected path and manifest has
    # passed the byte-for-byte preflight above.
    for baseline_png, stage_png, target in plans:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(baseline_png, target)
        if stage_png.exists():
            stage_png.unlink()

    manifest_summaries: list[dict[str, Any]] = []
    for namespace in sorted(aliases_by_namespace):
        tier = tier_by_namespace[namespace]
        path = manifest_path(stage_root, namespace)
        merged = merged_by_namespace[namespace]
        manifest = build_manifest(namespace, tier, list(merged.values()))
        write_json(path, manifest)
        manifest_summaries.append(
            {
                "aliases": len(manifest["aliases"]),
                "blobs": len(manifest["blobs"]),
                "namespace": namespace,
                "path": f"assets/{namespace}/{MANIFEST_RELATIVE_PATH.as_posix()}",
                "tier": tier,
            }
        )

    group_ids = [group.group_id for group in groups]
    report = {
        "schema_version": 1,
        "kind": "texture_alias_apply",
        "tool_version": TOOL_VERSION,
        "config_sha256": config.sha256,
        "group_count": len(group_ids),
        "groups_sha256": sha256_bytes(
            "\n".join(group_ids).encode("utf-8")
        ),
        "groups": group_ids if len(group_ids) <= 100 else None,
        "aliases_applied": len(records),
        "manifests": manifest_summaries,
        "status": "ok",
    }
    if args.report_out:
        write_json(args.report_out, report)
    print(json.dumps(report, sort_keys=True))
    return 0


def validate_manifest_structure(
    stage_root: Path, manifest_file: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = read_manifest(manifest_file)
    if canonical_json_bytes(manifest) != manifest_file.read_bytes():
        raise TextureToolError(f"Manifest is not canonical JSON: {manifest_file}")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise TextureToolError(f"Unsupported manifest schema: {manifest_file}")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise TextureToolError(f"Unsupported manifest format: {manifest_file}")
    namespace = validate_namespace(
        manifest.get("namespace"), f"{manifest_file}.namespace"
    )
    expected_parent = stage_root / "assets" / namespace / Path(
        MANIFEST_RELATIVE_PATH.as_posix()
    )
    if expected_parent.resolve() != manifest_file.resolve():
        raise TextureToolError(
            f"Manifest is outside its declared namespace: {manifest_file}"
        )
    tier = require_string(manifest.get("tier"), f"{manifest_file}.tier")
    aliases_raw = manifest.get("aliases")
    blobs_raw = manifest.get("blobs")
    if not isinstance(aliases_raw, list) or not isinstance(blobs_raw, list):
        raise TextureToolError(f"Manifest aliases/blobs must be arrays: {manifest_file}")

    aliases: list[dict[str, Any]] = []
    alias_nodes: set[str] = set()
    targets: set[str] = set()
    alias_paths: set[str] = set()
    for index, item in enumerate(aliases_raw):
        alias = dict(require_mapping(item, f"{manifest_file}.aliases[{index}]"))
        relative_path = normalize_relative_path(
            require_string(alias.get("path"), "alias.path"), "alias.path"
        )
        if (
            not relative_path.endswith(".png")
            or not relative_path.startswith(("textures/", "optifine/"))
        ):
            raise TextureToolError(
                "Alias path is not a namespace-relative PNG under textures/ "
                f"or optifine/: {relative_path}"
            )
        if relative_path in alias_paths:
            raise TextureToolError(
                f"Duplicate alias path in {manifest_file}: {relative_path}"
            )
        alias_paths.add(relative_path)
        if alias.get("tier") != tier:
            raise TextureToolError(
                f"Cross-tier alias in {manifest_file}: {relative_path}"
            )
        logical = require_string(alias.get("logical_id"), "alias.logical_id")
        if logical != logical_id(namespace, relative_path):
            raise TextureToolError(
                f"Logical ID does not match alias path in {manifest_file}: {logical}"
            )
        target = normalize_relative_path(
            require_string(alias.get("target"), "alias.target"), "alias.target"
        )
        expected_prefix = f"assets/{namespace}/texture_blobs/"
        if not target.startswith(expected_prefix):
            raise TextureToolError(
                f"Cross-namespace or non-blob target in {manifest_file}: {target}"
            )
        file_sha = require_sha256(alias.get("file_sha256"), "alias.file_sha256")
        expected_target = f"{expected_prefix}{file_sha}.png"
        if target != expected_target:
            raise TextureToolError(
                f"Blob target does not match file SHA in {manifest_file}: {target}"
            )
        alias_node = f"assets/{namespace}/{relative_path}"
        alias_nodes.add(alias_node)
        targets.add(target)
        target_path = stage_root / Path(PurePosixPath(target).as_posix())
        if not target_path.is_file():
            raise TextureToolError(f"Dangling blob target: {target_path}")
        if sha256_file(target_path) != file_sha:
            raise TextureToolError(f"Blob SHA mismatch: {target_path}")
        aliases.append(alias)

    if alias_nodes & targets:
        overlap = ", ".join(sorted(alias_nodes & targets))
        raise TextureToolError(f"Alias cycle or chain is forbidden: {overlap}")

    declared_blobs: dict[str, str] = {}
    for index, item in enumerate(blobs_raw):
        blob = require_mapping(item, f"{manifest_file}.blobs[{index}]")
        target = normalize_relative_path(
            require_string(blob.get("target"), "blob.target"), "blob.target"
        )
        file_sha = require_sha256(blob.get("file_sha256"), "blob.file_sha256")
        if target in declared_blobs:
            raise TextureToolError(
                f"Duplicate declared blob target in {manifest_file}: {target}"
            )
        declared_blobs[target] = file_sha
    if set(declared_blobs) != targets:
        raise TextureToolError(
            f"Declared blobs do not exactly match alias targets in {manifest_file}"
        )
    for target, file_sha in declared_blobs.items():
        if not target.endswith(f"/{file_sha}.png"):
            raise TextureToolError(
                f"Declared blob filename does not match SHA: {target}"
            )
    return manifest, aliases


def command_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    groups = selected_groups(config, args.group)
    stage_root = ensure_staged_root(config, args.stage_root)
    expected = selected_records(config, groups)
    expected_by_alias = {
        f"assets/{record['namespace']}/{record['path']}": record
        for record in expected.values()
    }
    manifest_files = sorted(
        stage_root.glob("assets/*/texture_aliases/v1.json"),
        key=lambda path: path.as_posix(),
    )
    if not manifest_files:
        raise TextureToolError(f"No v1 texture alias manifests found in {stage_root}")

    seen_aliases: set[str] = set()
    manifest_reports: list[dict[str, Any]] = []
    source_by_id = config.source_by_id
    all_targets: set[str] = set()
    all_alias_nodes: set[str] = set()
    decoded_rgba_validated = 0
    alpha_validated = 0
    for manifest_file in manifest_files:
        manifest, aliases = validate_manifest_structure(stage_root, manifest_file)
        namespace = str(manifest["namespace"])
        tier = str(manifest["tier"])
        for alias in aliases:
            alias_node = f"assets/{namespace}/{alias['path']}"
            if alias_node in seen_aliases:
                raise TextureToolError(f"Duplicate alias across manifests: {alias_node}")
            seen_aliases.add(alias_node)
            all_alias_nodes.add(alias_node)
            all_targets.add(str(alias["target"]))
            record = expected_by_alias.get(alias_node)
            if record is None:
                raise TextureToolError(f"Unexpected alias in stage: {alias_node}")
            expected_entry = alias_entry(record)
            if alias != expected_entry:
                raise TextureToolError(f"Alias differs from baseline: {alias_node}")
            source = source_by_id[str(record["source"])]
            baseline_path = (
                source.root
                / "assets"
                / namespace
                / Path(str(record["path"]))
            )
            if sha256_file(baseline_path) != alias["file_sha256"]:
                raise TextureToolError(f"Baseline changed during validation: {baseline_path}")
            target_path = stage_root / Path(
                PurePosixPath(str(alias["target"])).as_posix()
            )
            decoded_target = inspect_png(
                source,
                namespace,
                str(record["path"]),
                target_path,
            )
            decoded_fields = (
                "width",
                "height",
                "mode",
                "bit_depth",
                "rgba_sha256",
                "alpha_min",
                "alpha_max",
                "alpha_varies",
                "png_metadata_sha256",
            )
            for field in decoded_fields:
                if decoded_target[field] != record[field]:
                    raise TextureToolError(
                        f"Decoded {field} differs from baseline for alias: {alias_node}"
                    )
            decoded_rgba_validated += 1
            alpha_validated += 1
            staged_original = stage_root / Path(PurePosixPath(alias_node).as_posix())
            if staged_original.exists():
                raise TextureToolError(
                    f"Aliased staged PNG was not removed: {staged_original}"
                )
            baseline_mcmeta = baseline_path.with_name(baseline_path.name + ".mcmeta")
            staged_mcmeta = staged_original.with_name(staged_original.name + ".mcmeta")
            if baseline_mcmeta.is_file() != staged_mcmeta.is_file():
                raise TextureToolError(
                    f"mcmeta presence differs for alias: {alias_node}"
                )
            if baseline_mcmeta.is_file() and sha256_file(
                baseline_mcmeta
            ) != sha256_file(staged_mcmeta):
                raise TextureToolError(f"mcmeta differs for alias: {alias_node}")
        manifest_reports.append(
            {
                "aliases": len(aliases),
                "blobs": len(manifest["blobs"]),
                "namespace": namespace,
                "tier": tier,
            }
        )
    if all_alias_nodes & all_targets:
        overlap = ", ".join(sorted(all_alias_nodes & all_targets))
        raise TextureToolError(f"Cross-manifest alias cycle detected: {overlap}")
    missing_aliases = sorted(set(expected_by_alias) - seen_aliases)
    if missing_aliases:
        raise TextureToolError(
            "Expected aliases are missing: " + ", ".join(missing_aliases)
        )

    actual_blob_paths = {
        path.relative_to(stage_root).as_posix()
        for path in stage_root.glob("assets/*/texture_blobs/*.png")
        if path.is_file()
    }
    orphaned = sorted(actual_blob_paths - all_targets)
    if orphaned:
        raise TextureToolError("Orphaned texture blobs: " + ", ".join(orphaned))

    group_ids = [group.group_id for group in groups]
    report = {
        "schema_version": 1,
        "kind": "texture_alias_validation",
        "tool_version": TOOL_VERSION,
        "config_sha256": config.sha256,
        "group_count": len(group_ids),
        "groups_sha256": sha256_bytes(
            "\n".join(group_ids).encode("utf-8")
        ),
        "groups": group_ids if len(group_ids) <= 100 else None,
        "aliases_validated": len(seen_aliases),
        "blobs_validated": len(all_targets),
        "decoded_rgba_aliases_validated": decoded_rgba_validated,
        "alpha_aliases_validated": alpha_validated,
        "manifests": manifest_reports,
        "status": "ok",
    }
    if args.report_out:
        write_json(args.report_out, report)
    print(json.dumps(report, sort_keys=True))
    return 0


def normalize_container_member(value: str, label: str) -> str:
    normalized = value.replace("\\", "/").lstrip("/")
    pure = PurePosixPath(normalized)
    if not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise TextureToolError(f"Unsafe resource member in {label}: {value!r}")
    return pure.as_posix()


def read_container(path: Path) -> tuple[str, dict[str, bytes]]:
    resolved = path.resolve()
    if resolved.is_dir():
        entries: dict[str, bytes] = {}
        for file_path in sorted(
            (candidate for candidate in resolved.rglob("*") if candidate.is_file()),
            key=lambda item: item.relative_to(resolved).as_posix(),
        ):
            member = normalize_container_member(
                file_path.relative_to(resolved).as_posix(), str(resolved)
            )
            entries[member] = file_path.read_bytes()
        return "directory", entries
    if resolved.is_file() and zipfile.is_zipfile(resolved):
        entries = {}
        with zipfile.ZipFile(resolved, "r") as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                member = normalize_container_member(info.filename, str(resolved))
                if member in entries:
                    raise TextureToolError(
                        f"Duplicate archive member in {resolved}: {member}"
                    )
                entries[member] = archive.read(info)
        return "archive", entries
    raise TextureToolError(f"Expected a directory, ZIP, or JAR: {resolved}")


def logical_container_view(
    entries: Mapping[str, bytes], label: str
) -> dict[str, bytes]:
    view = dict(entries)
    manifest_paths = sorted(
        path
        for path in entries
        if fnmatch.fnmatchcase(path, "assets/*/texture_aliases/v1.json")
    )
    declared_targets: set[str] = set()
    alias_paths: set[str] = set()
    for manifest_member in manifest_paths:
        try:
            manifest = json.loads(entries[manifest_member].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TextureToolError(
                f"Invalid alias manifest in {label}: {manifest_member}: {exc}"
            ) from exc
        manifest_obj = require_mapping(manifest, manifest_member)
        if manifest_obj.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise TextureToolError(
                f"Unsupported alias schema in {label}: {manifest_member}"
            )
        if manifest_obj.get("format") != MANIFEST_FORMAT:
            raise TextureToolError(
                f"Unsupported alias format in {label}: {manifest_member}"
            )
        namespace = validate_namespace(
            manifest_obj.get("namespace"), f"{manifest_member}.namespace"
        )
        expected_manifest = f"assets/{namespace}/{MANIFEST_RELATIVE_PATH.as_posix()}"
        if manifest_member != expected_manifest:
            raise TextureToolError(
                f"Manifest namespace/path mismatch in {label}: {manifest_member}"
            )
        aliases = manifest_obj.get("aliases")
        if not isinstance(aliases, list):
            raise TextureToolError(f"Manifest aliases must be an array: {manifest_member}")
        tier = require_string(manifest_obj.get("tier"), f"{manifest_member}.tier")
        for index, item in enumerate(aliases):
            alias = require_mapping(item, f"{manifest_member}.aliases[{index}]")
            relative_path = normalize_relative_path(
                require_string(alias.get("path"), "alias.path"), "alias.path"
            )
            if (
                not relative_path.endswith(".png")
                or not relative_path.startswith(("textures/", "optifine/"))
            ):
                raise TextureToolError(
                    f"Invalid alias texture path in {label}: {relative_path}"
                )
            if alias.get("tier") != tier:
                raise TextureToolError(
                    f"Cross-tier alias in {label}: {relative_path}"
                )
            logical = require_string(alias.get("logical_id"), "alias.logical_id")
            if logical != logical_id(namespace, relative_path):
                raise TextureToolError(
                    f"Logical ID does not match alias path in {label}: {logical}"
                )
            alias_member = f"assets/{namespace}/{relative_path}"
            if alias_member in alias_paths:
                raise TextureToolError(
                    f"Duplicate alias member in {label}: {alias_member}"
                )
            alias_paths.add(alias_member)
            if alias_member in entries:
                raise TextureToolError(
                    f"Alias and physical resource both exist in {label}: {alias_member}"
                )
            target = normalize_container_member(
                require_string(alias.get("target"), "alias.target"), label
            )
            expected_prefix = f"assets/{namespace}/texture_blobs/"
            if not target.startswith(expected_prefix):
                raise TextureToolError(
                    f"Cross-namespace alias target in {label}: {target}"
                )
            if target in alias_paths:
                raise TextureToolError(f"Alias cycle in {label}: {target}")
            target_data = entries.get(target)
            if target_data is None:
                raise TextureToolError(f"Dangling alias target in {label}: {target}")
            expected_sha = require_sha256(
                alias.get("file_sha256"), "alias.file_sha256"
            )
            if sha256_bytes(target_data) != expected_sha:
                raise TextureToolError(f"Alias target SHA mismatch in {label}: {target}")
            declared_targets.add(target)
            view[alias_member] = target_data

    if alias_paths & declared_targets:
        overlap = ", ".join(sorted(alias_paths & declared_targets))
        raise TextureToolError(f"Alias cycle or chain in {label}: {overlap}")

    physical_blobs = {
        path
        for path in entries
        if fnmatch.fnmatchcase(path, "assets/*/texture_blobs/*.png")
    }
    orphaned = sorted(physical_blobs - declared_targets)
    if orphaned:
        raise TextureToolError(
            f"Orphaned texture blobs in {label}: {', '.join(orphaned)}"
        )
    for internal in manifest_paths:
        view.pop(internal, None)
    for internal in physical_blobs:
        view.pop(internal, None)
    return view


def comparison_document(
    left_path: Path,
    right_path: Path,
    raw: bool,
) -> dict[str, Any]:
    left_kind, left_entries = read_container(left_path)
    right_kind, right_entries = read_container(right_path)
    if not raw:
        left_entries = logical_container_view(left_entries, str(left_path))
        right_entries = logical_container_view(right_entries, str(right_path))
    left_paths = set(left_entries)
    right_paths = set(right_entries)
    missing_from_left = sorted(right_paths - left_paths)
    missing_from_right = sorted(left_paths - right_paths)
    changed: list[dict[str, str]] = []
    for path in sorted(left_paths & right_paths):
        left_sha = sha256_bytes(left_entries[path])
        right_sha = sha256_bytes(right_entries[path])
        if left_sha != right_sha:
            changed.append(
                {
                    "path": path,
                    "left_sha256": left_sha,
                    "right_sha256": right_sha,
                }
            )
    equal = not missing_from_left and not missing_from_right and not changed
    return {
        "schema_version": 1,
        "kind": "texture_container_comparison",
        "tool_version": TOOL_VERSION,
        "mode": "raw" if raw else "alias-aware",
        "left_kind": left_kind,
        "right_kind": right_kind,
        "left_file_count": len(left_entries),
        "right_file_count": len(right_entries),
        "missing_from_left": missing_from_left,
        "missing_from_right": missing_from_right,
        "changed": changed,
        "equal": equal,
        "status": "ok" if equal else "different",
    }


def command_compare(args: argparse.Namespace) -> int:
    report = comparison_document(args.left, args.right, args.raw)
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["equal"] else 1


def manifest_counts_from_entries(
    entries: Mapping[str, bytes], label: str
) -> tuple[int, int, int]:
    manifest_count = 0
    alias_count = 0
    blob_count = 0
    for path, data in entries.items():
        if not fnmatch.fnmatchcase(path, "assets/*/texture_aliases/v1.json"):
            continue
        manifest_count += 1
        try:
            manifest = require_mapping(
                json.loads(data.decode("utf-8")), f"manifest in {label}"
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TextureToolError(
                f"Invalid alias manifest in {label}: {path}: {exc}"
            ) from exc
        aliases = manifest.get("aliases")
        blobs = manifest.get("blobs")
        if not isinstance(aliases, list) or not isinstance(blobs, list):
            raise TextureToolError(
                f"Manifest aliases/blobs must be arrays in {label}: {path}"
            )
        alias_count += len(aliases)
        blob_count += len(blobs)
    return manifest_count, alias_count, blob_count


def properties_digest(entries: Mapping[str, bytes]) -> tuple[int, str]:
    digest = hashlib.sha256()
    properties = [
        (path, data)
        for path, data in entries.items()
        if path.endswith(".properties")
    ]
    for path, data in sorted(properties):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return len(properties), digest.hexdigest()


def container_metrics(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    kind, entries = read_container(resolved)
    manifest_count, alias_count, blob_count = manifest_counts_from_entries(
        entries, str(resolved)
    )
    property_count, property_digest = properties_digest(entries)
    png_entries = {
        name: data for name, data in entries.items() if name.endswith(".png")
    }
    metrics: dict[str, Any] = {
        "path": str(resolved),
        "kind": kind,
        "sha256": sha256_file(resolved) if resolved.is_file() else None,
        "archive_bytes": resolved.stat().st_size if resolved.is_file() else None,
        "entry_count": len(entries),
        "physical_png_count": len(png_entries),
        "physical_png_bytes": sum(len(data) for data in png_entries.values()),
        "uncompressed_file_bytes": sum(len(data) for data in entries.values()),
        "uncompressed_asset_bytes": sum(
            len(data)
            for name, data in entries.items()
            if name.startswith("assets/")
        ),
        "manifest_count": manifest_count,
        "alias_count": alias_count,
        "canonical_blob_count": blob_count,
        "properties_count": property_count,
        "properties_set_sha256": property_digest,
    }
    if kind == "archive":
        with zipfile.ZipFile(resolved, "r") as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            metrics["compressed_entry_bytes"] = sum(
                info.compress_size for info in infos
            )
            metrics["zip_entry_overhead_bytes"] = (
                int(metrics["archive_bytes"])
                - int(metrics["compressed_entry_bytes"])
            )
    return metrics


def command_metrics(args: argparse.Namespace) -> int:
    metrics = [container_metrics(path) for path in args.container]
    report = {
        "schema_version": 1,
        "kind": "texture_container_metrics",
        "tool_version": TOOL_VERSION,
        "status": "ok",
        "containers": metrics,
    }
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                "container_count": len(metrics),
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory PNGs and pilot deterministic, namespace-scoped, "
            "byte-identical texture aliases."
        )
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {TOOL_VERSION}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory",
        help="Inventory every configured PNG and emit deterministic JSON and CSV.",
    )
    inventory.add_argument(
        "--config",
        type=Path,
        default=Path("config/family-texture-sources.json"),
    )
    inventory.add_argument("--json-out", type=Path, required=True)
    inventory.add_argument("--csv-out", type=Path, required=True)
    inventory.set_defaults(func=command_inventory)

    analyze = subparsers.add_parser(
        "analyze",
        help=(
            "Analyze an inventory for same-scope exact duplicates, "
            "decoded-only matches and report-only cross-scope matches."
        ),
    )
    analyze.add_argument("--inventory", type=Path, required=True)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.set_defaults(func=command_analyze)

    plan = subparsers.add_parser(
        "plan",
        help=(
            "Generate a deterministic, source-scoped virtual-alias config "
            "from eligible byte-identical inventory groups."
        ),
    )
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument("--inventory", type=Path, required=True)
    plan.add_argument("--source", required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument(
        "--include-role",
        action="append",
        default=[],
        help=(
            "Asset role to include; repeatable. Defaults to the roles proven "
            "by pilots A-C, excluding overlays and animation."
        ),
    )
    plan.set_defaults(func=command_plan)

    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply selected byte-identical groups to a copied resource stage.",
    )
    apply_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/family-texture-sources.json"),
    )
    apply_parser.add_argument("--stage-root", type=Path, required=True)
    apply_parser.add_argument(
        "--group",
        action="append",
        default=[],
        help="Pilot group id; repeatable. Defaults to selected_by_default groups.",
    )
    apply_parser.add_argument("--report-out", type=Path)
    apply_parser.set_defaults(func=command_apply)

    validate = subparsers.add_parser(
        "validate",
        help="Validate generated manifests/blobs against configured baselines.",
    )
    validate.add_argument(
        "--config",
        type=Path,
        default=Path("config/family-texture-sources.json"),
    )
    validate.add_argument("--stage-root", type=Path, required=True)
    validate.add_argument(
        "--group",
        action="append",
        default=[],
        help="Pilot group id; repeatable. Defaults to selected_by_default groups.",
    )
    validate.add_argument("--report-out", type=Path)
    validate.set_defaults(func=command_validate)

    compare = subparsers.add_parser(
        "compare",
        help="Compare directories/ZIPs/JARs, expanding texture aliases by default.",
    )
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument(
        "--raw",
        action="store_true",
        help="Compare physical entries instead of expanding aliases.",
    )
    compare.add_argument("--output", type=Path)
    compare.set_defaults(func=command_compare)

    metrics = subparsers.add_parser(
        "metrics",
        help="Measure physical archive, PNG, alias, blob and properties totals.",
    )
    metrics.add_argument("container", type=Path, nargs="+")
    metrics.add_argument("--output", type=Path, required=True)
    metrics.set_defaults(func=command_metrics)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except TextureToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
