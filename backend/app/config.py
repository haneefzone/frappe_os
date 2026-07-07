from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings. Names map to env vars case-insensitively
    (database_url <- DATABASE_URL, fdm_secret_key <- FDM_SECRET_KEY, ...)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FDM Platform"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://fdm:fdm@localhost:5432/fdm"
    redis_url: str = "redis://localhost:6379/0"

    # Master key for Fernet secrets-at-rest; never stored in DB.
    fdm_secret_key: str = "change-me-generate-a-fernet-key"
    jwt_secret: str = "change-me-long-random-string"

    # Comma-separated list of allowed browser origins.
    cors_origins: str = "http://localhost:5173"

    default_tz: str = "Asia/Dubai"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
