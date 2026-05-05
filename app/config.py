from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "BoBSearch"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    web_username: str = "admin"
    web_password: str
    session_secret: str

    jackett_url: str = "http://172.17.0.1:9117"
    jackett_api_key: str
    jackett_indexers_dir: str = "/jackett-indexers"

    qbit_url: str = "http://172.17.0.1:8080"
    qbit_username: str = "admin"
    qbit_password: str
    qbit_category: str = "movies-staging"
    qbit_save_path: str = "/app/qBittorrent/downloads/movies-staging"
    qbit_downloads_path: str = "/app/qBittorrent/downloads"
    qbit_local_downloads_path: str = "/downloads"
    qbit_extra_downloads_path: str = "/downloads"
    qbit_extra_local_downloads_path: str = "/downloads"
    jellyfin_library_path: str = "/jellyfin/library"

    llm_base_url: str
    llm_api_key: str
    llm_model: str = "gpt-5.5"

    search_concurrency: int = 8
    indexer_timeout_seconds: float = 12
    total_timeout_seconds: float = 45

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
