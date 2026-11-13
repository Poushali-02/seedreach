"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    llm_provider: Literal["gemini", "anthropic"] = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-lite-latest"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    apollo_api_key: str = ""
    # USD per million tokens, from your provider's pricing page; 0 shows tokens only (e.g. on a free tier)
    llm_price_input_per_m: float = 0.0
    llm_price_output_per_m: float = 0.0

    google_credentials_file: Path = Path("credentials.json")
    google_token_file: Path = Path("token.json")
    db_path: Path = Path("outreach.db")

    http_timeout: float = 15.0
    user_agent: str = DEFAULT_USER_AGENT
    hn_lookback_days: int = 62
    launchhn_lookback_days: int = 120
    yc_max_team_size: int = 25  # YC directory: skip companies larger than this
    # YC directory: only companies listing one of these regions, comma-separated (e.g. "India,Remote").
    # Names must match YC's own region names exactly (case doesn't matter). Empty means every region.
    yc_regions: str = ""
    log_level: str = "INFO"
    headless: bool = False  # set on servers/CI: never open a browser for Gmail sign-in
    allow_guessed_emails: bool = False  # draft to {first}@domain guesses with no evidence (most bounce or go unread)
    # Read the company's public Greenhouse/Lever/Ashby board for open internships and name one in the draft
    check_job_boards: bool = True

    @field_validator("google_credentials_file", "google_token_file", "db_path")
    @classmethod
    def _anchor_to_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else ROOT / value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
