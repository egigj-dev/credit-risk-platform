from __future__ import annotations

import json
from datetime import date

import pytest

from credit_risk.config import Settings
from credit_risk.production import (
    PipelineResult,
    run_sql_preprocessing,
    validate_nbo_artifacts,
)


def test_pipeline_result_contract() -> None:
    result = PipelineResult(date(2026, 8, 30), ("source_staging", "pd_scoring"))
    assert result.as_dict() == {
        "status": "success",
        "run_date": "2026-08-30",
        "steps": ["source_staging", "pd_scoring"],
    }


def test_nbo_contract_requires_config(tmp_path) -> None:
    settings = Settings("database", "postgresql://unused", tmp_path, tmp_path, [])
    with pytest.raises(RuntimeError, match="contract is missing"):
        validate_nbo_artifacts(settings)


def test_nbo_contract_message_is_actionable(tmp_path, monkeypatch) -> None:
    artifact_dir = tmp_path / "nbo_artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "nbo_config.json").write_text(json.dumps({"n_clients": 42}))

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, _sql):
            return None

        def fetchone(self):
            return (7,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def cursor(self):
            return Cursor()

    monkeypatch.setattr("credit_risk.production.psycopg2.connect", lambda _url: Connection())
    settings = Settings("database", "postgresql://unused", tmp_path, tmp_path, [])
    with pytest.raises(RuntimeError, match="trained for 42 clients.*contains 7"):
        validate_nbo_artifacts(settings)


def test_preprocessing_reuses_complete_checkpoint(tmp_path, monkeypatch) -> None:
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, sql, params=()):
            statements.append((sql, params))

        def fetchone(self):
            return (True, True, True)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def cursor(self):
            return Cursor()

    monkeypatch.setattr("credit_risk.production.psycopg2.connect", lambda _url: Connection())
    settings = Settings("database", "postgresql://unused", tmp_path, tmp_path, [])
    assert run_sql_preprocessing(date(2026, 8, 30), settings) is False
    assert len(statements) == 1
