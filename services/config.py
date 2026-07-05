from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FURNACE_", env_file=".env", extra="ignore")

    database_url: str = Field(default="sqlite+aiosqlite:///./furnace.db")
    provisioner: str = Field(default="fake")
    artifacts_dir: str = Field(default="./artifacts")
    session_step_delay_seconds: float = Field(default=0.05, ge=0.0)
    fake_queue_acquire: bool = Field(default=False)
    auto_create_schema: bool = Field(default=True)
    master_encryption_key: str | None = Field(default=None)
    github_app_id: str | None = Field(default=None)
    github_app_slug: str | None = Field(default=None)
    github_app_private_key: str | None = Field(default=None)
    github_webhook_secret: str | None = Field(default=None)
    github_api_base: str = Field(default="https://api.github.com")
