from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "BoBSearch"
    app_version: str = "1.0.25"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_data_dir: str = "/app/data"

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
    software_library_path: str = "/software"

    llm_base_url: str = "http://ccnode.briconbric.com:49530/v1"
    llm_api_key: str
    llm_model: str = "gpt-6-astra"
    llm_api_type: str = "responses"
    llm_fallback_base_url: str | None = "http://ccnode.briconbric.com:49530/v1"
    llm_fallback_api_key: str | None = None
    llm_fallback_model: str | None = "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ3_S"
    llm_fallback_api_type: str = "chat_completions"
    ai_config_path: str = "/app/data/ai-config.json"

    search_concurrency: int = 8
    indexer_timeout_seconds: float = 12
    total_timeout_seconds: float = 45
    search_history_path: str = "/app/data/search-history.json"
    search_history_limit: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_base_settings() -> Settings:
    return Settings()


def _runtime_ai_overrides(settings: Settings) -> dict[str, str | None]:
    path = Path(settings.ai_config_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    allowed = {
        "llm_base_url",
        "llm_api_key",
        "llm_model",
        "llm_api_type",
        "llm_fallback_base_url",
        "llm_fallback_api_key",
        "llm_fallback_model",
        "llm_fallback_api_type",
    }
    return {key: value for key, value in data.items() if key in allowed and isinstance(value, (str, type(None)))}


def get_settings() -> Settings:
    base = get_base_settings()
    overrides = _runtime_ai_overrides(base)
    return base.model_copy(update=overrides) if overrides else base


def save_ai_settings(overrides: dict[str, str | None]) -> None:
    settings = get_base_settings()
    path = Path(settings.ai_config_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
