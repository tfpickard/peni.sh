from functools import lru_cache
from pathlib import Path

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://penish:penish@localhost:5432/penish"
    data_dir: Path = Path("/srv/penish/data")
    anthropic_api_key: str | None = None
    # Unset turns the whole admin surface off (404), so a fresh deploy exposes nothing.
    admin_password: str | None = None

    # Model ids are env vars so they can be swapped without a code change.
    describe_model: str = "claude-sonnet-5-5"
    haiku_model: str = "claude-haiku-5-5"
    embed_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embed_dim: int = 1024

    settle_seconds: int = 10
    watermark: bool = False
    # Below this the corpus mean is too noisy for centered similarity to help.
    centered_min_images: int = 30
    web_build_dir: Path = Path("web/build")

    # All derived from data_dir so the inbox and processing dirs share a
    # filesystem, which keeps the move into processing/ an atomic rename.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def processing(self) -> Path:
        return self.data_dir / "processing"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed(self) -> Path:
        return self.data_dir / "failed"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_orig(self) -> Path:
        return self.data_dir / "media" / "orig"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_pub(self) -> Path:
        return self.data_dir / "media" / "pub"


@lru_cache
def get_settings() -> Settings:
    return Settings()
