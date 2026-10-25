from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", VERSION):
    raise RuntimeError(
        f"VERSION must be a full semantic version (for example 1.6.0): {VERSION!r}"
    )
FAMILY_ASSET_GENERATION = 2
FAMILY_COMPATIBILITY_GENERATION = 2
COMPATIBILITY_MARKER = Path("assets/erydon/erydon_collection_pack.json")
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ARCHIVE_COMPRESSION = zipfile.ZIP_DEFLATED
ARCHIVE_COMPRESSION_LEVEL = 6
ROOT_FILES = {"LICENSE.txt", "README.txt", "pack.mcmeta", "pack.png"}
RAW_CTM_ROOT = Path("assets/minecraft/optifine/ctm")
TEXTURE_CTM_ROOT = Path("assets/minecraft/textures/optifine/ctm")
SHARED_NATIVE_OVERLAY_ROOT = Path(
    "assets/minecraft/textures/optifine/ctm/overlay"
)
TILES_PROPERTY = re.compile(r"^\s*tiles\s*(?:[=:]|\s)\s*(.*)$")
RAW_CTM_TOKEN = re.compile(r"(?<!textures/)optifine/ctm/")
TIERS = {
    "32x": (
        "ERYDON-32x-Resource-Pack-"
        f"compat{FAMILY_COMPATIBILITY_GENERATION}-v{VERSION}.zip"
    ),
    "64x": (
        "ERYDON-64x-Resource-Pack-"
        f"compat{FAMILY_COMPATIBILITY_GENERATION}-v{VERSION}.zip"
    ),
}
PLAN_NAMES = {
    "32x": "unified-32x.json",
    "64x": "unified-64x.json",
}
PACK_DESCRIPTIONS = {
    "32x": (
        "ERYDON 32x Resource Pack: Detailed connected textures and PBR materials "
        "for the ERYDON mod family."
    ),
    "64x": (
        "ERYDON 64x Resource Pack: High-detail connected textures and PBR materials "
        "for the ERYDON mod family."
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def files_are_identical(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as first_handle, second.open("rb") as second_handle:
        while True:
            first_chunk = first_handle.read(1024 * 1024)
            second_chunk = second_handle.read(1024 * 1024)
            if first_chunk != second_chunk:
                return False
            if not first_chunk:
                return True


def collect_files(pack_root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(pack_root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            if (current_path / directory).is_symlink():
                raise RuntimeError(f"Symlinked directory: {current_path / directory}")
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Non-regular file: {path}")
            files.append(path)
    files.sort(key=lambda path: path.relative_to(pack_root).as_posix())
    root_files = {
        path.name for path in files if path.parent.resolve() == pack_root.resolve()
    }
    if root_files != ROOT_FILES:
        raise RuntimeError(
            f"Unexpected ZIP-root files in {pack_root}: {sorted(root_files)}"
        )
    return files


def packaged_files(pack_root: Path, files: list[Path]) -> list[Path]:
    """Omit the shared overlay set supplied by the ERYDON family mods."""
    return [
        path
        for path in files
        if not path.relative_to(pack_root).is_relative_to(
            SHARED_NATIVE_OVERLAY_ROOT
        )
    ]


def source_snapshot(pack_root: Path, files: list[Path]) -> dict[str, str]:
    return {
        path.relative_to(pack_root).as_posix(): sha256_file(path)
        for path in files
    }


def properties_proof(pack_root: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    paths = sorted(
        (
            path
            for path in pack_root.rglob("*.properties")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.relative_to(pack_root).as_posix(),
    )
    for path in paths:
        relative = path.relative_to(pack_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return {
        "count": len(paths),
        "set_and_content_sha256": digest.hexdigest().upper(),
    }


def validate_ctm_layout(pack_root: Path) -> dict[str, int]:
    raw_root = pack_root / RAW_CTM_ROOT
    texture_root = pack_root / TEXTURE_CTM_ROOT
    raw_pngs = sorted(raw_root.rglob("*.png"))
    if raw_pngs:
        sample = raw_pngs[0].relative_to(pack_root).as_posix()
        raise RuntimeError(
            f"Mirrored raw CTM PNGs are forbidden in {pack_root.name}: "
            f"{len(raw_pngs)} files, first {sample}"
        )

    properties = sorted(raw_root.rglob("*.properties"))
    if not properties:
        raise RuntimeError(f"No CTM properties found in {raw_root}")
    texture_pngs = sorted(texture_root.rglob("*.png"))
    if not texture_pngs:
        raise RuntimeError(f"No texture-rooted CTM PNGs found in {texture_root}")

    tile_references = 0
    missing: list[str] = []
    raw_references: list[str] = []
    unsupported: list[str] = []
    for property_path in properties:
        text = property_path.read_bytes().decode("iso-8859-1")
        for line in text.splitlines():
            match = TILES_PROPERTY.match(line)
            if match is None:
                continue
            for token in re.split(r"[ ,]+", match.group(1).strip()):
                if not token or token in {"<skip>", "<default>"}:
                    continue
                tile_references += 1
                namespace = "minecraft"
                path = token.removesuffix(".png")
                if ":" in path:
                    namespace, path = path.split(":", 1)
                if RAW_CTM_TOKEN.search(path):
                    raw_references.append(
                        f"{property_path.relative_to(pack_root).as_posix()}: {token}"
                    )
                    continue
                if (
                    namespace != "minecraft"
                    or not path.startswith("textures/optifine/ctm/")
                ):
                    unsupported.append(
                        f"{property_path.relative_to(pack_root).as_posix()}: {token}"
                    )
                    continue
                target = pack_root / "assets" / namespace / f"{path}.png"
                if not target.is_file():
                    missing.append(
                        f"{property_path.relative_to(pack_root).as_posix()}: {token}"
                    )

    if raw_references:
        raise RuntimeError(
            "Raw CTM tile references would recreate the mirrored tree: "
            + "; ".join(raw_references[:3])
        )
    if unsupported:
        raise RuntimeError(
            "Unsupported CTM tile references: " + "; ".join(unsupported[:3])
        )
    if missing:
        raise RuntimeError(
            "Missing texture-rooted CTM tiles: " + "; ".join(missing[:3])
        )

    atlas_path = pack_root / "assets/minecraft/atlases/blocks.json"
    atlas = json.loads(atlas_path.read_text(encoding="utf-8"))
    atlas_directories = 0
    missing_atlas_directories: list[str] = []
    for source in atlas.get("sources", []):
        if source.get("type") != "directory":
            continue
        source_path = str(source.get("source", ""))
        if not source_path.startswith("optifine/ctm/"):
            continue
        atlas_directories += 1
        if not (pack_root / "assets/minecraft/textures" / source_path).is_dir():
            missing_atlas_directories.append(source_path)
    if missing_atlas_directories:
        raise RuntimeError(
            "Missing CTM atlas directories: "
            + ", ".join(missing_atlas_directories[:3])
        )

    return {
        "property_files": len(properties),
        "tile_references": tile_references,
        "texture_pngs": len(texture_pngs),
        "raw_pngs": 0,
        "raw_tile_references": 0,
        "atlas_directories": atlas_directories,
    }


def load_release_baselines(path: Path) -> dict[str, dict[str, object]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    packs = document.get("packs")
    if document.get("schema_version") != 1 or not isinstance(packs, dict):
        raise RuntimeError(f"Invalid release artifact baseline: {path}")
    return packs


def release_baseline_comparison(
    tier: str,
    baseline: dict[str, object],
    archive_bytes: int,
) -> dict[str, object]:
    baseline_bytes = int(baseline["bytes"])
    comparison = {
        **baseline,
        "saved_bytes": baseline_bytes - archive_bytes,
        "saved_percent": round(
            (baseline_bytes - archive_bytes) * 100.0 / baseline_bytes,
            4,
        ),
    }
    if archive_bytes > baseline_bytes:
        raise RuntimeError(
            f"{tier} deduplicated ZIP exceeds release artifact baseline "
            f"{baseline['release']}: {archive_bytes} > {baseline_bytes}"
        )
    return comparison


def run_texture_tool(tool: Path, *arguments: object) -> None:
    command = [sys.executable, str(tool), *(str(value) for value in arguments)]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part.strip()
        )
        raise RuntimeError(
            f"Texture deduplication command failed ({result.returncode}): "
            f"{' '.join(command)}\n{detail}"
        )


def prepare_deduplicated_stage(
    pack_root: Path,
    stage_root: Path,
    plan: Path,
    tool: Path,
    report_root: Path,
) -> dict[str, object]:
    shutil.copytree(pack_root, stage_root)
    report_root.mkdir(parents=True, exist_ok=True)
    apply_report = report_root / "apply.json"
    validate_report = report_root / "validate.json"
    compare_report = report_root / "logical-compare.json"
    run_texture_tool(
        tool,
        "apply",
        "--config",
        plan,
        "--stage-root",
        stage_root,
        "--report-out",
        apply_report,
    )
    run_texture_tool(
        tool,
        "validate",
        "--config",
        plan,
        "--stage-root",
        stage_root,
        "--report-out",
        validate_report,
    )
    run_texture_tool(
        tool,
        "compare",
        pack_root,
        stage_root,
        "--output",
        compare_report,
    )

    physical_properties = properties_proof(pack_root)
    staged_properties = properties_proof(stage_root)
    if physical_properties != staged_properties:
        raise RuntimeError(
            f"CTM .properties changed while staging {pack_root.name}"
        )

    apply_data = json.loads(apply_report.read_text(encoding="utf-8"))
    validate_data = json.loads(validate_report.read_text(encoding="utf-8"))
    comparison_data = json.loads(compare_report.read_text(encoding="utf-8"))
    if (
        apply_data.get("status") != "ok"
        or validate_data.get("status") != "ok"
        or comparison_data.get("status") != "ok"
        or comparison_data.get("equal") is not True
    ):
        raise RuntimeError(f"Texture alias proof did not pass for {pack_root}")
    return {
        "aliases": int(apply_data["aliases_applied"]),
        "manifests": apply_data["manifests"],
        "properties": physical_properties,
        "logical_comparison": "equal",
    }


def paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve()
    second = second.resolve()
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def validate_plan_binding(
    plan_path: Path,
    config_path: Path,
    pack_root: Path,
    tier: str,
) -> dict[str, object]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    source_id = f"unified-{tier}"
    metadata = plan.get("plan_metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError(f"Missing plan metadata: {plan_path}")
    if (
        plan.get("schema_version") != 1
        or metadata.get("kind") != "generated_texture_alias_plan"
        or metadata.get("method") != "virtual-alias"
        or metadata.get("exact_file_bytes_only") is not True
        or metadata.get("source") != source_id
        or metadata.get("tier") != source_id
    ):
        raise RuntimeError(f"Plan metadata does not match {tier}: {plan_path}")
    if metadata.get("inventory_config_sha256") != sha256_file(config_path).lower():
        raise RuntimeError(
            f"Plan was not generated from the current pack config: {plan_path}"
        )

    plan_sources = plan.get("sources")
    config_sources = config.get("sources")
    if not isinstance(plan_sources, list) or len(plan_sources) != 1:
        raise RuntimeError(f"Plan must bind exactly one source: {plan_path}")
    if not isinstance(config_sources, list):
        raise RuntimeError(f"Invalid pack config sources: {config_path}")
    matching_config_sources = [
        source for source in config_sources if source.get("id") == source_id
    ]
    if len(matching_config_sources) != 1:
        raise RuntimeError(
            f"Pack config must define exactly one {source_id} source"
        )
    plan_source = plan_sources[0]
    config_source = matching_config_sources[0]
    comparable_keys = {
        "id",
        "tier",
        "namespaces",
        "include",
        "exclude",
        "role_rules",
        "metadata_overrides",
        "intentionally_native_high_res",
    }
    if any(
        plan_source.get(key) != config_source.get(key)
        for key in comparable_keys
    ):
        raise RuntimeError(
            f"Plan source policy differs from current config: {plan_path}"
        )

    plan_root = (plan_path.parent / plan_source["root"]).resolve()
    config_root = (config_path.parent / config_source["root"]).resolve()
    expected_root = pack_root.resolve()
    if plan_root != expected_root or config_root != expected_root:
        raise RuntimeError(
            f"Plan/config source root does not match {tier} pack: "
            f"{plan_root}, {config_root}, expected {expected_root}"
        )

    groups = plan.get("pilot_groups")
    if not isinstance(groups, list) or not groups:
        raise RuntimeError(f"Plan has no selected exact groups: {plan_path}")
    configured_namespaces = set(config_source["namespaces"])
    for group in groups:
        if (
            not isinstance(group, dict)
            or group.get("source") != source_id
            or group.get("tier") != source_id
            or group.get("namespace") not in configured_namespaces
            or group.get("selected_by_default") is not True
        ):
            raise RuntimeError(
                f"Plan group escaped the configured source/tier: {plan_path}"
            )
    return plan


def validate_metadata(pack_root: Path, tier: str, archive_name: str) -> None:
    metadata = json.loads((pack_root / "pack.mcmeta").read_text(encoding="utf-8"))
    description = metadata["pack"]["description"]
    if description != PACK_DESCRIPTIONS[tier]:
        raise RuntimeError(f"Unexpected {tier} description: {description!r}")
    readme = (pack_root / "README.txt").read_text(encoding="utf-8")
    if f"Release file: {archive_name}" not in readme:
        raise RuntimeError(f"{tier} README does not name {archive_name}")
    marker_path = pack_root / COMPATIBILITY_MARKER
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    expected_marker = {
        "schema": 1,
        "family_asset_generation": FAMILY_ASSET_GENERATION,
        "family_compatibility_generation": FAMILY_COMPATIBILITY_GENERATION,
        "release": VERSION,
        "resolution": int(tier.removesuffix("x")),
    }
    if marker != expected_marker:
        raise RuntimeError(
            f"Unexpected {tier} compatibility marker: {marker!r}"
        )


def build_archive(pack_root: Path, files: list[Path], output: Path) -> None:
    if output.exists():
        raise RuntimeError(f"Output already exists: {output}")
    with zipfile.ZipFile(
        output,
        mode="x",
        compression=ARCHIVE_COMPRESSION,
        compresslevel=ARCHIVE_COMPRESSION_LEVEL,
        allowZip64=True,
        strict_timestamps=True,
    ) as archive:
        archive.comment = b""
        for path in files:
            relative = path.relative_to(pack_root).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_TIMESTAMP)
            info.compress_type = ARCHIVE_COMPRESSION
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(
                info,
                path.read_bytes(),
                compress_type=ARCHIVE_COMPRESSION,
                compresslevel=ARCHIVE_COMPRESSION_LEVEL,
            )


def inspect_archive(pack_root: Path, files: list[Path], archive_path: Path) -> dict:
    expected_names = [path.relative_to(pack_root).as_posix() for path in files]
    source_hashes = {
        path.relative_to(pack_root).as_posix(): sha256_file(path) for path in files
    }
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if names != expected_names:
            raise RuntimeError(f"Archive path set/order differs: {archive_path}")
        if archive.comment:
            raise RuntimeError(f"Archive comment is not empty: {archive_path}")
        uncompressed = 0
        for info in infos:
            if info.compress_type != ARCHIVE_COMPRESSION:
                raise RuntimeError(
                    f"Unexpected compression for {info.filename}: "
                    f"{info.compress_type}"
                )
            if info.date_time != FIXED_TIMESTAMP:
                raise RuntimeError(f"Variable timestamp: {info.filename}")
            if stat.S_IMODE((info.external_attr >> 16) & 0xFFFF) != 0o644:
                raise RuntimeError(f"Unexpected permissions: {info.filename}")
            data = archive.read(info)
            digest = hashlib.sha256(data).hexdigest().upper()
            if digest != source_hashes[info.filename]:
                raise RuntimeError(f"Content mismatch: {info.filename}")
            uncompressed += len(data)
    return {
        "bytes": archive_path.stat().st_size,
        "entries": len(files),
        "sha256": sha256_file(archive_path),
        "uncompressed_bytes": uncompressed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--texture-dedupe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Build virtual texture aliases (default), or use "
            "--no-texture-dedupe for the fully physical fallback packs."
        ),
    )
    args = parser.parse_args()

    repo_root = REPO_ROOT
    texture_tool = repo_root / "tools" / "texture_dedupe.py"
    texture_config = repo_root / "config" / "texture-dedupe-packs.json"
    release_baselines = load_release_baselines(
        repo_root / "config" / "release-artifact-baselines.json"
    )
    plans_root = repo_root / "config" / "plans"
    output_dir = args.output_dir.resolve()
    for tier in TIERS:
        pack_root = (repo_root / "packs" / tier).resolve()
        if paths_overlap(output_dir, pack_root):
            raise RuntimeError(
                f"Output directory must not overlap authored {tier} sources: "
                f"{output_dir}"
            )
    output_dir.mkdir(parents=True, exist_ok=True)
    deduplicated_tiers = set(PLAN_NAMES) if args.texture_dedupe else set()
    if not deduplicated_tiers:
        build_mode = "physical-fallback"
    elif deduplicated_tiers == set(TIERS):
        build_mode = "deduplicated"
    else:
        build_mode = "mixed-rollout"
    report: dict[str, object] = {
        "status": "passed",
        "version": VERSION,
        "mode": build_mode,
        "compression": "ZIP_DEFLATED",
        "compression_level": ARCHIVE_COMPRESSION_LEVEL,
        "packs": {},
    }

    for tier, archive_name in TIERS.items():
        pack_root = repo_root / "packs" / tier
        validate_metadata(pack_root, tier, archive_name)
        ctm_layout = validate_ctm_layout(pack_root)
        source_files = collect_files(pack_root)
        physical_files = packaged_files(pack_root, source_files)
        before = source_snapshot(pack_root, source_files)
        output = output_dir / archive_name
        repro = output_dir / f".{archive_name}.repro"
        if tier in deduplicated_tiers:
            plan = plans_root / PLAN_NAMES[tier]
            if not plan.is_file():
                raise RuntimeError(
                    f"Missing committed texture deduplication plan: {plan}"
                )
            plan_data = validate_plan_binding(
                plan,
                texture_config,
                pack_root,
                tier,
            )
            expected_aliases = int(plan_data["plan_metadata"]["alias_count"])
            with tempfile.TemporaryDirectory(
                prefix=f"erydon-{tier}-texture-dedupe-"
            ) as temporary:
                temporary_root = Path(temporary)
                stage_root = temporary_root / "stage"
                proof = prepare_deduplicated_stage(
                    pack_root,
                    stage_root,
                    plan,
                    texture_tool,
                    output_dir / "texture-dedupe" / tier,
                )
                if proof["aliases"] != expected_aliases:
                    raise RuntimeError(
                        f"{tier} staged {proof['aliases']} aliases; "
                        f"plan declares {expected_aliases}"
                    )

                staged_files = packaged_files(
                    stage_root,
                    collect_files(stage_root),
                )
                build_archive(stage_root, staged_files, output)
                build_archive(stage_root, staged_files, repro)
                first = inspect_archive(stage_root, staged_files, output)
                second = inspect_archive(stage_root, staged_files, repro)
                if first != second or not files_are_identical(output, repro):
                    raise RuntimeError(
                        f"{tier} builds are not byte-for-byte reproducible"
                    )
                repro.unlink()

                baseline_control = temporary_root / "physical-control.zip"
                build_archive(pack_root, physical_files, baseline_control)
                baseline_bytes = baseline_control.stat().st_size
                if first["bytes"] >= baseline_bytes:
                    raise RuntimeError(
                        f"{tier} deduplicated ZIP is not smaller: "
                        f"{first['bytes']} >= {baseline_bytes}"
                    )
                metrics_report = (
                    output_dir / "texture-dedupe" / tier / "archive-metrics.json"
                )
                run_texture_tool(
                    texture_tool,
                    "metrics",
                    "--output",
                    metrics_report,
                    baseline_control,
                    output,
                )
                saved_bytes = baseline_bytes - int(first["bytes"])
                historical_comparison = None
                historical = release_baselines.get(tier)
                if historical is not None:
                    historical_comparison = release_baseline_comparison(
                        tier,
                        historical,
                        int(first["bytes"]),
                    )
                dedupe_details = {
                    "enabled": True,
                    **proof,
                    "plan": plan.relative_to(repo_root).as_posix(),
                    "plan_sha256": sha256_file(plan),
                    "physical_control_bytes": baseline_bytes,
                    "saved_bytes": saved_bytes,
                    "saved_percent": round(
                        saved_bytes * 100.0 / baseline_bytes,
                        4,
                    ),
                    "historical_comparison": historical_comparison,
                }
        else:
            build_archive(pack_root, physical_files, output)
            build_archive(pack_root, physical_files, repro)
            first = inspect_archive(pack_root, physical_files, output)
            second = inspect_archive(pack_root, physical_files, repro)
            if first != second or not files_are_identical(output, repro):
                raise RuntimeError(
                    f"{tier} builds are not byte-for-byte reproducible"
                )
            repro.unlink()
            dedupe_details = {
                "enabled": False,
                "reason": (
                    "physical-fallback-requested"
                    if not args.texture_dedupe
                    else "not-yet-enabled-in-rollout"
                ),
                "properties": properties_proof(pack_root),
            }

        after_files = collect_files(pack_root)
        after = source_snapshot(pack_root, after_files)
        if before != after:
            raise RuntimeError(
                f"Physical source tree changed while building {tier}"
            )
        report["packs"][tier] = {
            "filename": archive_name,
            "ctm_layout": ctm_layout,
            "shared_native_overlay": {
                "owner": "erydon-family-mods",
                "path": SHARED_NATIVE_OVERLAY_ROOT.as_posix(),
                "excluded_entries": len(source_files) - len(physical_files),
            },
            **first,
            "texture_dedupe": dedupe_details,
        }

    report_path = output_dir / "resource-pack-build-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checksum_lines = [
        f"{details['sha256']}  {details['filename']}"
        for details in report["packs"].values()
    ]
    (output_dir / "RESOURCE_PACK_SHA256SUMS.txt").write_text(
        "\n".join(checksum_lines) + "\n", encoding="ascii"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
