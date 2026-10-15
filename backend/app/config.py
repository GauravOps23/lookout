from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://panel:panel@localhost:5432/panel"
    master_key: str | None = None  # base64, 32 bytes
    master_key_file: str | None = None
    backup_dir: str = "./backups"
    allowed_origins: list[str] = []

    cookie_secure: bool = True
    session_absolute_hours: int = 8
    session_idle_minutes: int = 30
    login_rate_per_minute: int = 10
    max_failed_attempts: int = 5
    lockout_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
