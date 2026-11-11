from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NETLAB_")

    database_url: str = "postgresql+psycopg://netlab:netlab@localhost:55432/netlab"
    dns_zone: str = "netlab.internal"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
