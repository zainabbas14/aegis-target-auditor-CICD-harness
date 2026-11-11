"""Runtime configuration. Environment only; nothing secret has a default.

Every variable is prefixed ``AUDITOR_``. See ``.env.example`` for the full list.
Invalid configuration fails at startup, not on the first request.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUDITOR_", env_file=".env", extra="ignore", frozen=True)

    env: Literal["dev", "test", "prod"] = "dev"
    ledger_backend: Literal["sqlite", "postgres"] = "sqlite"
    sqlite_path: Path = Path("tests/mock_data/generated/enterprise_erp.db")
    # SecretStr: the URL embeds a password; it must never render in logs or reprs.
    database_url: SecretStr | None = None
    db_pool_min: int = Field(default=1, ge=1)
    db_pool_max: int = Field(default=5, ge=1)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    near_duplicate_window_days: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.ledger_backend == "postgres" and self.database_url is None:
            raise ValueError("AUDITOR_DATABASE_URL is required when AUDITOR_LEDGER_BACKEND=postgres")
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("AUDITOR_DB_POOL_MIN must be <= AUDITOR_DB_POOL_MAX")
        if self.env == "prod" and self.ledger_backend == "sqlite":
            raise ValueError("SQLite ledger is not permitted when AUDITOR_ENV=prod")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
