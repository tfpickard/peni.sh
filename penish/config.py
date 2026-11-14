import logging
from functools import lru_cache
from pathlib import Path

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

DEV_SECRET_KEY = "dev-secret-not-for-production"


class Settings(BaseSettings):
    # compose passes unset keys as ${VAR:-}, an empty string; that must mean "default",
    # or an unset SECRET_KEY would quietly become the empty key.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    database_url: str = "postgresql://penish:penish@localhost:5432/penish"
    data_dir: Path = Path("/srv/penish/data")
    anthropic_api_key: str | None = None
    # Other providers, chosen per purpose by a `provider:` prefix on the model id. An unset
    # key only matters once a purpose is pointed at that provider.
    openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    xai_api_key: str | None = None
    # Unset turns the whole admin surface off (404), so a fresh deploy exposes nothing.
    admin_password: str | None = None
    # Keys the hmac of voter ids and ip hashes. Rotating it orphans existing votes.
    secret_key: str = DEV_SECRET_KEY
    # Baked in at image build time so /system can say what is deployed.
    git_sha: str = "dev"
    # Both unset means comments fall back to the rate limit alone.
    turnstile_site_key: str | None = None
    turnstile_secret: str | None = None
    # The worker's internal embed server; compose publishes no port for it.
    embed_server_url: str = "http://worker:8001"
    # Where the worker listens for it. Only tests move it, to dodge a busy 8001.
    embed_server_port: int = 8001

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
    # The cloud is capped so it cannot grow with the corpus; raise the floor as it fills.
    tag_cloud_limit: int = 60
    # A tag on one picture leads nowhere but back to that picture.
    tag_cloud_min: int = 2

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
    s = Settings()
    # /data is the container layout, so a default key there means a prod box without one.
    if s.secret_key == DEV_SECRET_KEY and s.data_dir == Path("/data"):
        log.warning("SECRET_KEY is the dev default; set it in .env")
    return s
