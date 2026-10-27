"""Central configuration. The only module that reads environment variables (.env or the process env)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"  # dev | prod
    log_level: str = "INFO"
    timezone: str = "Europe/Lisbon"

    database_url: str = "postgresql+psycopg://imoveis:imoveis@localhost:5432/imoveis"

    # Panel
    session_secret: str = "change-me-in-.env"
    admin_username: str = "andreia"
    admin_password_hash: str = ""  # python -m app.cli hash-password "<password>"
    panel_host: str = "localhost"
    panel_base_url: str = "https://localhost"

    # Crawling
    http_user_agent: str = "ImoveisBot/0.1 (+mailto:contact@example.com)"
    http_default_delay_s: float = 2.0
    http_timeout_s: float = 30.0

    # Telegram (alerts go to exactly one chat: hers)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_min_spacing_s: float = 3.0

    # AI
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5"
    llm_model_profiler: str = ""  # empty -> llm_model
    ai_cap_cents_month: int = 500

    # Idealista alert mailbox (IMAP)
    imap_host: str = "imap.gmail.com"
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"

    # Backups (worker job)
    backup_dir: str = "/backups"
    backup_keep: int = 14

    @property
    def profiler_model(self) -> str:
        return self.llm_model_profiler or self.llm_model


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
