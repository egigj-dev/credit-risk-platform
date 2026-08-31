from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

ARTIFACT_GROUPS = ("model_artifacts", "npl_artifacts", "nbo_artifacts")


def import_legacy_artifacts(source_root: Path, target_root: Path) -> dict:
    source_root, target_root = source_root.resolve(), target_root.resolve()
    copied: list[dict] = []
    target_root.mkdir(parents=True, exist_ok=True)
    for group in ARTIFACT_GROUPS:
        source = source_root / group
        if not source.is_dir():
            continue
        destination = target_root / group
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite {destination}")
        shutil.copytree(source, destination)
        for file in destination.rglob("*"):
            if file.is_file():
                copied.append(
                    {
                        "file": str(file.relative_to(target_root)),
                        "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                    }
                )
    manifest = {"source": str(source_root), "files": copied}
    (target_root / "legacy-import-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest
