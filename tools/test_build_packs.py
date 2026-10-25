from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("build_packs.py")
SPEC = importlib.util.spec_from_file_location("erydon_build_packs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BUILD_PACKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD_PACKS)


class BuildPacksTest(unittest.TestCase):
    def test_release_names_and_version_match_both_authored_packs(self) -> None:
        self.assertEqual(
            (BUILD_PACKS.REPO_ROOT / "VERSION").read_text(
                encoding="utf-8"
            ).strip(),
            BUILD_PACKS.VERSION,
        )
        self.assertRegex(BUILD_PACKS.VERSION, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertEqual({"32x", "64x"}, set(BUILD_PACKS.TIERS))
        for tier, filename in BUILD_PACKS.TIERS.items():
            with self.subTest(tier=tier):
                self.assertEqual(
                    f"ERYDON-{tier}-Resource-Pack-"
                    f"compat{BUILD_PACKS.FAMILY_COMPATIBILITY_GENERATION}-"
                    f"v{BUILD_PACKS.VERSION}.zip",
                    filename,
                )
                BUILD_PACKS.validate_metadata(
                    BUILD_PACKS.REPO_ROOT / "packs" / tier,
                    tier,
                    filename,
                )

    def test_authored_descriptions_preserve_official_pack_recognition(self) -> None:
        # Existing family mods inspect the profile filename plus description
        # before opening the protocol marker. In particular, their 64x branch
        # requires the literal "64x resource pack" after the ZIP rename.
        for tier, filename in BUILD_PACKS.TIERS.items():
            with self.subTest(tier=tier):
                metadata = json.loads(
                    (BUILD_PACKS.REPO_ROOT / "packs" / tier / "pack.mcmeta")
                    .read_text(encoding="utf-8")
                )
                description = metadata["pack"]["description"]
                self.assertTrue(
                    description.startswith(f"ERYDON {tier} Resource Pack: "),
                    description,
                )
                signal = f"file/{filename} {description}".lower()
                self.assertIn("erydon", signal)
                self.assertIn(f"{tier} resource pack", signal)

    def test_archive_is_deflated_deterministic_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = root / "pack"
            pack.mkdir()
            (pack / "LICENSE.txt").write_bytes(b"license")
            (pack / "README.txt").write_bytes(b"readme")
            (pack / "pack.mcmeta").write_bytes(b"{}")
            (pack / "pack.png").write_bytes(b"png")
            nested = pack / "assets" / "erydon"
            nested.mkdir(parents=True)
            (nested / "proof.txt").write_bytes(b"proof")

            files = BUILD_PACKS.collect_files(pack)
            first = root / "first.zip"
            second = root / "second.zip"
            BUILD_PACKS.build_archive(pack, files, first)
            BUILD_PACKS.build_archive(pack, files, second)
            self.assertTrue(BUILD_PACKS.files_are_identical(first, second))
            self.assertEqual(
                BUILD_PACKS.inspect_archive(pack, files, first),
                BUILD_PACKS.inspect_archive(pack, files, second),
            )
            with zipfile.ZipFile(first) as archive:
                self.assertTrue(
                    all(
                        info.compress_type == zipfile.ZIP_DEFLATED
                        for info in archive.infolist()
                    )
                )

    def test_shared_native_overlay_textures_are_not_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pack = Path(temporary)
            ordinary = (
                pack
                / "assets"
                / "minecraft"
                / "textures"
                / "block"
                / "a.png"
            )
            overlay = pack / BUILD_PACKS.SHARED_NATIVE_OVERLAY_ROOT
            ordinary.parent.mkdir(parents=True)
            overlay.mkdir(parents=True)
            ordinary.write_bytes(b"ordinary")
            albedo = overlay / "0.png"
            normal = overlay / "0_n.png"
            specular = overlay / "0_s.png"
            albedo.write_bytes(b"albedo")
            normal.write_bytes(b"normal")
            specular.write_bytes(b"specular")

            files = BUILD_PACKS.packaged_files(
                pack,
                [ordinary, albedo, normal, specular],
            )
            self.assertEqual([ordinary], files)

    def test_ctm_layout_requires_one_texture_rooted_png_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pack = Path(temporary)
            raw = (
                pack
                / "assets"
                / "minecraft"
                / "optifine"
                / "ctm"
                / "proof"
            )
            textures = (
                pack
                / "assets"
                / "minecraft"
                / "textures"
                / "optifine"
                / "ctm"
                / "proof"
            )
            raw.mkdir(parents=True)
            textures.mkdir(parents=True)
            properties = raw / "proof.properties"
            properties.write_text(
                "method=fixed\n"
                "tiles=textures/optifine/ctm/proof/0\n",
                encoding="ascii",
            )
            (textures / "0.png").write_bytes(b"png")
            atlas = pack / "assets" / "minecraft" / "atlases" / "blocks.json"
            atlas.parent.mkdir(parents=True)
            atlas.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "type": "directory",
                                "source": "optifine/ctm/proof",
                                "prefix": "optifine/ctm/proof/",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = BUILD_PACKS.validate_ctm_layout(pack)
            self.assertEqual(1, result["property_files"])
            self.assertEqual(1, result["tile_references"])
            self.assertEqual(1, result["texture_pngs"])

            raw_png = raw / "0.png"
            raw_png.write_bytes(b"png")
            with self.assertRaises(RuntimeError):
                BUILD_PACKS.validate_ctm_layout(pack)
            raw_png.unlink()

            properties.write_text(
                "method=fixed\ntiles=optifine/ctm/proof/0\n",
                encoding="ascii",
            )
            with self.assertRaises(RuntimeError):
                BUILD_PACKS.validate_ctm_layout(pack)

    def test_properties_proof_includes_paths_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "assets" / "minecraft" / "optifine" / "a.properties"
            second = root / "assets" / "erydon" / "optifine" / "a.properties"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"method=repeat\n")
            second.write_bytes(b"method=repeat\n")

            proof = BUILD_PACKS.properties_proof(root)
            self.assertEqual(2, proof["count"])
            first.write_bytes(b"method=fixed\n")
            self.assertNotEqual(proof, BUILD_PACKS.properties_proof(root))

    def test_source_snapshot_hashes_bytes_not_only_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "same-size.bin"
            path.write_bytes(b"first")
            before_stat = path.stat()
            before = BUILD_PACKS.source_snapshot(root, [path])
            path.write_bytes(b"other")
            os.utime(
                path,
                ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns),
            )
            after = BUILD_PACKS.source_snapshot(root, [path])
            self.assertNotEqual(before, after)

    def test_validate_metadata_requires_current_compatibility_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pack = Path(temporary)
            archive_name = (
                "ERYDON-32x-Resource-Pack-"
                f"compat{BUILD_PACKS.FAMILY_COMPATIBILITY_GENERATION}-"
                f"v{BUILD_PACKS.VERSION}.zip"
            )
            (pack / "pack.mcmeta").write_text(
                json.dumps(
                    {
                        "pack": {
                            "description": BUILD_PACKS.PACK_DESCRIPTIONS["32x"]
                        }
                    }
                ),
                encoding="utf-8",
            )
            (pack / "README.txt").write_text(
                f"Release file: {archive_name}\n",
                encoding="utf-8",
            )
            marker = pack / BUILD_PACKS.COMPATIBILITY_MARKER
            marker.parent.mkdir(parents=True)
            marker.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "family_asset_generation": (
                            BUILD_PACKS.FAMILY_ASSET_GENERATION
                        ),
                        "family_compatibility_generation": (
                            BUILD_PACKS.FAMILY_COMPATIBILITY_GENERATION
                        ),
                        "release": BUILD_PACKS.VERSION,
                        "resolution": 32,
                    }
                ),
                encoding="utf-8",
            )
            BUILD_PACKS.validate_metadata(pack, "32x", archive_name)
            marker.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "family_asset_generation": 1,
                        "family_compatibility_generation": (
                            BUILD_PACKS.FAMILY_COMPATIBILITY_GENERATION
                        ),
                        "release": BUILD_PACKS.VERSION,
                        "resolution": 32,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                BUILD_PACKS.validate_metadata(pack, "32x", archive_name)

            marker.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "family_asset_generation": (
                            BUILD_PACKS.FAMILY_ASSET_GENERATION
                        ),
                        "family_compatibility_generation": 1,
                        "release": BUILD_PACKS.VERSION,
                        "resolution": 32,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                BUILD_PACKS.validate_metadata(pack, "32x", archive_name)

    def test_release_artifact_baseline_allows_exact_rebuild_only(self) -> None:
        baseline = {
            "bytes": 100,
            "filename": "proof.zip",
            "release": "1.0.0",
            "sha256": "0" * 64,
        }
        comparison = BUILD_PACKS.release_baseline_comparison(
            "64x", baseline, 100
        )
        self.assertEqual(0, comparison["saved_bytes"])
        self.assertEqual(0.0, comparison["saved_percent"])
        with self.assertRaises(RuntimeError):
            BUILD_PACKS.release_baseline_comparison("64x", baseline, 101)

    def test_output_overlap_and_plan_binding_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = root / "packs" / "32x"
            pack.mkdir(parents=True)
            config_dir = root / "config"
            plans_dir = config_dir / "plans"
            plans_dir.mkdir(parents=True)
            source = {
                "id": "unified-32x",
                "tier": "unified-32x",
                "root": "../packs/32x",
                "namespaces": ["erydon"],
                "include": ["**/*.png"],
                "exclude": [],
                "role_rules": [],
                "metadata_overrides": {},
                "intentionally_native_high_res": [],
            }
            config = {
                "schema_version": 1,
                "sources": [source],
                "pilot_groups": [],
            }
            config_path = config_dir / "texture-dedupe-packs.json"
            config_path.write_text(
                json.dumps(config, sort_keys=True),
                encoding="utf-8",
            )
            plan_source = dict(source)
            plan_source["root"] = "../../packs/32x"
            config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
            plan = {
                "schema_version": 1,
                "plan_metadata": {
                    "kind": "generated_texture_alias_plan",
                    "method": "virtual-alias",
                    "exact_file_bytes_only": True,
                    "source": "unified-32x",
                    "tier": "unified-32x",
                    "inventory_config_sha256": config_sha,
                    "alias_count": 2,
                },
                "sources": [plan_source],
                "pilot_groups": [
                    {
                        "id": "exact-erydon-proof",
                        "source": "unified-32x",
                        "tier": "unified-32x",
                        "namespace": "erydon",
                        "selected_by_default": True,
                        "role": "albedo",
                        "paths": [
                            "textures/block/a.png",
                            "textures/block/b.png",
                        ],
                    }
                ],
            }
            plan_path = plans_dir / "unified-32x.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            self.assertTrue(BUILD_PACKS.paths_overlap(root / "packs", pack))
            self.assertFalse(BUILD_PACKS.paths_overlap(root / "dist", pack))
            BUILD_PACKS.validate_plan_binding(
                plan_path,
                config_path,
                pack,
                "32x",
            )
            plan["plan_metadata"]["tier"] = "unified-64x"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                BUILD_PACKS.validate_plan_binding(
                    plan_path,
                    config_path,
                    pack,
                    "32x",
                )


if __name__ == "__main__":
    unittest.main()
