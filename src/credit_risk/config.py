from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    mode: str
    database_url: str | None
    artifact_dir: Path
    data_dir: Path
    cors_origins: list[str]

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.getenv("APP_MODE", "demo").lower()
        if mode not in {"demo", "database"}:
            raise ValueError("APP_MODE must be 'demo' or 'database'")
        database_url = os.getenv("DATABASE_URL")
        if mode == "database" and not database_url:
            raise ValueError("DATABASE_URL is required when APP_MODE=database")
        return cls(
            mode=mode,
            database_url=database_url,
            artifact_dir=Path(os.getenv("ARTIFACT_DIR", ROOT / "artifacts")).resolve(),
            data_dir=Path(os.getenv("DATA_DIR", ROOT / "data")).resolve(),
            cors_origins=[
                x.strip()
                for x in os.getenv("CORS_ORIGINS", "http://localhost:8002").split(",")
                if x.strip()
            ],
        )
