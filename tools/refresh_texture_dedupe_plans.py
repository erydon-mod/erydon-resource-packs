from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


SOURCE_IDS = ("unified-32x", "unified-64x")


def run(*arguments: object) -> None:
    result = subprocess.run(
        [sys.executable, *(str(argument) for argument in arguments)],
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate exact-byte texture alias plans from the canonical "
            "physical 32x and 64x pack sources."
        )
    )
    parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    tool = repo_root / "tools" / "texture_dedupe.py"
    config = repo_root / "config" / "texture-dedupe-packs.json"
    plans_root = repo_root / "config" / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="erydon-pack-texture-inventory-"
    ) as temporary:
        temporary_root = Path(temporary)
        inventory = temporary_root / "inventory.json"
        inventory_csv = temporary_root / "inventory.csv"
        run(
            tool,
            "inventory",
            "--config",
            config,
            "--json-out",
            inventory,
            "--csv-out",
            inventory_csv,
        )
        for source_id in SOURCE_IDS:
            run(
                tool,
                "plan",
                "--config",
                config,
                "--inventory",
                inventory,
                "--source",
                source_id,
                "--output",
                plans_root / f"{source_id}.json",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
