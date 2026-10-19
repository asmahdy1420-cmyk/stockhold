"""Runtime configuration read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./stockhold.db"
    default_ttl_seconds: int = 900
    max_ttl_seconds: int = 3600

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("STOCKHOLD_DATABASE_URL", cls.database_url),
            default_ttl_seconds=int(
                os.environ.get("STOCKHOLD_DEFAULT_TTL_SECONDS", cls.default_ttl_seconds)
            ),
            max_ttl_seconds=int(os.environ.get("STOCKHOLD_MAX_TTL_SECONDS", cls.max_ttl_seconds)),
        )
