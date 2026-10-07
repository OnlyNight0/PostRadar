"""Application configuration loaded from environment variables or .env."""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = ""
    admin_user_id: int | None = None
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_session: str = ""
    database_url: str = "sqlite+aiosqlite:///./postradar.db"
    media_dir: str = "./data/media"
    gemini_api_key: str = ""
    gemini_primary_model: str = "gemini-3.5-flash-lite"
    gemini_fallback_model: str = "gemini-3.8-flash"
    ai_edit_enabled: bool = True
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("admin_user_id", "telegram_api_id", mode="before")
    @classmethod
    def empty_optional_integer(cls, value: object) -> object:
        """Treat blank values in a copied .env.example as unset."""
        return None if value == "" else value
