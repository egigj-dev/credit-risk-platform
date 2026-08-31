"""Unified access to the production pipeline sources vendored under legacy/."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from credit_risk.config import ROOT, Settings
from credit_risk.core.contracts import validate_data_dir

PIPELINES = ROOT / "legacy" / "pipelines"


def _db_params(url: str | None) -> dict:
    if not url:
        raise RuntimeError("DATABASE_URL is required for a production pipeline command")
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/"),
        "user": parsed.username,
        "password": parsed.password,
    }


@contextmanager
def _working_dir(path: Path):
    prior = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prior)


def _load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, PIPELINES / script)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _artifact_root(settings: Settings) -> Path:
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    return settings.artifact_dir


def run_pd_etl(run_date: date, settings: Settings) -> None:
    root = _artifact_root(settings)
    with _working_dir(root):
        module = _load("credit_risk_etl_pg.py", "legacy_pd_etl")
        module.PG_CONN_PARAMS = _db_params(settings.database_url) | {
            "options": "-c application_name=credit_risk_etl"
        }
        try:
            module.run_pipeline(run_date)
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise


def run_npl(mode: str, settings: Settings) -> None:
    root = _artifact_root(settings)
    with _working_dir(root):
        module = _load("npl_train.py", "legacy_npl")
        module.PG = _db_params(settings.database_url)
        module.ARTIFACTS = root / "npl_artifacts"
        module.ARTIFACTS.mkdir(exist_ok=True)
        if mode == "train":
            module.train()
        elif mode == "score":
            module.run_score(module.load_artifacts())
        else:
            arts = module.load_artifacts()
            conn = module.get_conn()
            df = module.load_training_data(conn)
            conn.close()
            _, test = module.out_of_time_split(module.prepare_features(df))
            module.evaluate_model(arts["model"], *module.get_X_y(test), "OOT test")


def run_nbo(mode: str, settings: Settings) -> None:
    root = _artifact_root(settings)
    with _working_dir(root):
        module = _load("nbo_train.py", "legacy_nbo")
        module.PG = _db_params(settings.database_url)
        module.ARTIFACTS = root / "nbo_artifacts"
        module.ARTIFACTS.mkdir(exist_ok=True)
        if mode == "ownership":
            module.refresh_ownership()
        elif mode == "train":
            module.train()
        else:
            arts = module.load_artifacts()
            conn = module.get_conn()
            df = module.load_clients(conn)
            conn.close()
            expected = int(arts["config"].get("n_clients", -1))
            if expected != len(df):
                raise RuntimeError(
                    f"NBO artifacts were trained for {expected:,} clients, but the database "
                    f"contains {len(df):,}. Retrain NBO against this database before scoring."
                )
            module.run_daily(module.score_rfm(df), arts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Full credit-risk platform commands")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    commands = parser.add_subparsers(dest="command", required=True)
    pd = commands.add_parser("pd-etl")
    pd.add_argument("--run-date", default=str(date.today()))
    npl = commands.add_parser("npl")
    npl.add_argument("--mode", choices=["train", "score", "eval"], default="score")
    nbo = commands.add_parser("nbo")
    nbo.add_argument("--mode", choices=["ownership", "train", "daily"], default="daily")
    data = commands.add_parser("check-data")
    data.add_argument("--data-dir", type=Path, default=Settings.from_env().data_dir)
    args = parser.parse_args()
    if args.command == "check-data":
        missing = validate_data_dir(args.data_dir)
        print("Input contract OK" if not missing else f"Missing: {', '.join(missing)}")
        raise SystemExit(bool(missing))
    settings = Settings.from_env()
    settings = Settings(
        "database",
        args.database_url,
        settings.artifact_dir,
        settings.data_dir,
        settings.cors_origins,
    )
    if args.command == "pd-etl":
        run_pd_etl(date.fromisoformat(args.run_date), settings)
    elif args.command == "npl":
        run_npl(args.mode, settings)
    else:
        run_nbo(args.mode, settings)


if __name__ == "__main__":
    main()
