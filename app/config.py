"""Application configuration, loaded from environment / .env.

Secrets never live in the repo. Locally they come from .env (git-ignored);
on Render they come from the dashboard's environment variables.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://localhost:5432/nextgen",
        alias="DATABASE_URL",
    )

    # HTTP Basic Auth — guards the entire app (two internal users).
    basic_auth_user: str = Field(default="nextgen", alias="BASIC_AUTH_USER")
    basic_auth_pass: str = Field(default="change-me", alias="BASIC_AUTH_PASS")

    # External data sources (unused by the scaffold; wired in later phases).
    alphavantage_api_key: str | None = Field(default=None, alias="ALPHAVANTAGE_API_KEY")
    fmp_api_key: str | None = Field(default=None, alias="FMP_API_KEY")
    fred_api_key: str | None = Field(default=None, alias="FRED_API_KEY")

    app_env: str = Field(default="development", alias="APP_ENV")

    @field_validator("database_url")
    @classmethod
    def normalize_db_url(cls, v: str) -> str:
        """Render hands out `postgres://...`; SQLAlchemy 2 + psycopg3 wants
        `postgresql+psycopg://...`. Normalize so the same value works in both places."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+psycopg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
