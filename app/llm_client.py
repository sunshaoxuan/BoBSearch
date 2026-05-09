from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


@dataclass(frozen=True)
class LlmEndpoint:
    name: str
    base_url: str
    model: str
    api_key: str | None = None


def auth_headers(api_key: str | None) -> dict[str, str]:
    token = (api_key or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def primary_endpoint(settings: Settings) -> LlmEndpoint:
    return LlmEndpoint(
        name="primary",
        base_url=settings.llm_base_url.rstrip("/"),
        model=settings.llm_model,
        api_key=settings.llm_api_key,
    )


def fallback_endpoint(settings: Settings) -> LlmEndpoint | None:
    model = (settings.llm_fallback_model or "").strip()
    if not model:
        return None
    base_url = (settings.llm_fallback_base_url or settings.llm_base_url).rstrip("/")
    api_key = settings.llm_fallback_api_key
    if api_key is not None and not api_key.strip():
        api_key = None
    return LlmEndpoint(
        name="fallback",
        base_url=base_url,
        model=model,
        api_key=api_key,
    )


async def health_check(endpoint: LlmEndpoint, timeout: float = 8) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{endpoint.base_url}/models", headers=auth_headers(endpoint.api_key))
        response.raise_for_status()
    return {"configured": True, "ok": True, "model": endpoint.model}


async def chat_completion(
    settings: Settings,
    *,
    system_content: str,
    user_content: str,
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None = None,
    timeout: float = 60,
) -> tuple[dict[str, Any], str]:
    endpoints = [primary_endpoint(settings)]
    fallback = fallback_endpoint(settings)
    if fallback:
        endpoints.append(fallback)

    last_error: Exception | None = None
    for endpoint in endpoints:
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{endpoint.base_url}/chat/completions",
                    headers=auth_headers(endpoint.api_key),
                    json=payload,
                )
                response.raise_for_status()
                return response.json(), endpoint.name
        except Exception as exc:
            last_error = exc
            continue
    assert last_error is not None
    raise last_error
