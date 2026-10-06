from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CIBUD_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://cibud:cibud@localhost:5432/cibud"
    grobid_url: str = "http://localhost:8070"
    # Sent to Crossref/OpenAlex so requests go to their polite pools.
    contact_email: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
