"""Application settings. Every limit can be overridden with a GEO_-prefixed env var."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GEO_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    database_url: str = ""  # defaults to SQLite inside data_dir

    # Upload limits
    max_upload_bytes: int = 50 * 1024 * 1024
    max_uncompressed_bytes: int = 250 * 1024 * 1024
    max_compression_ratio: int = 100
    max_zip_entries: int = 100

    # Content limits
    max_features: int = 200_000
    max_vertices: int = 10_000_000
    max_property_chars: int = 65_536

    # Files at or under both thresholds are processed inside the POST request (201);
    # larger ones are queued and processed in the background (202).
    sync_max_bytes: int = 5 * 1024 * 1024
    sync_max_features: int = 20_000

    # Pagination
    default_page_size: int = 100
    max_page_size: int = 1000

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'geo.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
