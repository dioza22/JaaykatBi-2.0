from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://jaaykatbi:jaaykatbi@localhost:5434/jaaykatbi"

    # WhatsApp Cloud API (Meta)
    whatsapp_api_version: str = "v21.0"
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_webhook_verify_token: str = "jaaykatbi_webhook_2026"

    # LLM (Gemini 2.5 Flash-Lite, free tier for MVP validation)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"

    # App
    app_base_url: str = "http://localhost:8000"
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
