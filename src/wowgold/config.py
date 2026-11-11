"""Runtime configuration, read from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Region = Literal["us", "eu", "kr", "tw"]
Locale = Literal[
    "en_US",
    "es_MX",
    "pt_BR",
    "en_GB",
    "es_ES",
    "fr_FR",
    "ru_RU",
    "de_DE",
    "pt_PT",
    "it_IT",
    "ko_KR",
    "zh_TW",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    blizzard_client_id: str = ""
    blizzard_client_secret: SecretStr = SecretStr("")

    wowgold_region: Region = "eu"
    # An unsupported locale (e.g. "en_EU") makes Blizzard return null for every name.
    wowgold_locale: Locale = "en_GB"
    wowgold_data_dir: Path = Path("data")
    # Auction houses you trade on, e.g. WOWGOLD_HOUSES=[110,113]. Empty = all non-empty houses.
    wowgold_houses: list[int] = []
    wowgold_methods_dir: Path = Path("data/methods")
    # Versioned static namespace observed at launch; Blizzard's plain `static-forever-*`
    # has been reported to 403 at times, so we keep a fallback.
    wowgold_static_version: str = "1.60.1_70399"

    @property
    def has_credentials(self) -> bool:
        return bool(self.blizzard_client_id and self.blizzard_client_secret.get_secret_value())

    @property
    def db_path(self) -> Path:
        return self.wowgold_data_dir / "wowgold.sqlite3"

    @property
    def profile_path(self) -> Path:
        return self.wowgold_data_dir / "profile.json"

    @property
    def timer_path(self) -> Path:
        return self.wowgold_data_dir / "active_session.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
