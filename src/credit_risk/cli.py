from __future__ import annotations

import argparse
import json
from pathlib import Path

from credit_risk.config import Settings
from credit_risk.core.contracts import validate_data_dir
from credit_risk.core.legacy import import_legacy_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="credit-risk", description="Credit Risk Platform operations"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-data", help="Validate required Home Credit CSVs")
    check.add_argument("--data-dir", type=Path, default=Settings.from_env().data_dir)
    artifact = commands.add_parser(
        "import-legacy-artifacts", help="Copy v2 artifacts with checksums"
    )
    artifact.add_argument("--source", type=Path, required=True)
    artifact.add_argument("--destination", type=Path, default=Settings.from_env().artifact_dir)
    args = parser.parse_args()
    if args.command == "check-data":
        missing = validate_data_dir(args.data_dir)
        print(json.dumps({"data_dir": str(args.data_dir), "missing": missing}, indent=2))
        raise SystemExit(1 if missing else 0)
    if args.command == "import-legacy-artifacts":
        print(json.dumps(import_legacy_artifacts(args.source, args.destination), indent=2))


if __name__ == "__main__":
    main()
