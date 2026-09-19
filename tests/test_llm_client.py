from __future__ import annotations

from app.config import Settings, _runtime_ai_overrides, save_ai_settings
from app.llm_client import fallback_endpoint, primary_endpoint, response_text


def settings(tmp_path) -> Settings:
    return Settings(
        web_password="web",
        session_secret="session",
        jackett_api_key="jackett",
        qbit_password="qbit",
        llm_api_key="shared-key",
        llm_base_url="http://llm.test/v1",
        llm_model="primary-model",
        llm_api_type="responses",
        llm_fallback_base_url="http://llm.test/v1",
        llm_fallback_api_key="ignored-fallback-key",
        llm_fallback_model="fallback-model",
        llm_fallback_api_type="chat_completions",
        ai_config_path=str(tmp_path / "ai-config.json"),
    )


def test_endpoints_use_separate_protocols_and_shared_key(tmp_path):
    cfg = settings(tmp_path)

    primary = primary_endpoint(cfg)
    fallback = fallback_endpoint(cfg)

    assert primary.api_type == "responses"
    assert fallback is not None
    assert fallback.api_type == "chat_completions"
    assert primary.api_key == "shared-key"
    assert fallback.api_key == "shared-key"


def test_response_text_reads_direct_and_nested_output():
    assert response_text({"output_text": "direct"}) == "direct"
    assert response_text({"output": [{"content": [{"type": "output_text", "text": "nested"}]}]}) == "nested"


def test_runtime_ai_config_is_private_and_loadable(monkeypatch, tmp_path):
    cfg = settings(tmp_path)
    monkeypatch.setattr("app.config.get_base_settings", lambda: cfg)
    values = {
        "llm_base_url": "http://new.test/v1",
        "llm_api_key": "new-key",
        "llm_model": "new-model",
        "llm_api_type": "responses",
        "llm_fallback_base_url": "http://new.test/v1",
        "llm_fallback_api_key": "new-key",
        "llm_fallback_model": "new-fallback",
        "llm_fallback_api_type": "chat_completions",
    }

    save_ai_settings(values)

    path = tmp_path / "ai-config.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert _runtime_ai_overrides(cfg) == values
