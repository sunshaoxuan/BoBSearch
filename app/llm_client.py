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
    api_type: str = "chat_completions"


def auth_headers(api_key: str | None) -> dict[str, str]:
    token = (api_key or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def primary_endpoint(settings: Settings) -> LlmEndpoint:
    return LlmEndpoint(
        name="primary",
        base_url=settings.llm_base_url.rstrip("/"),
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        api_type=settings.llm_api_type,
    )


def fallback_endpoint(settings: Settings) -> LlmEndpoint | None:
    model = (settings.llm_fallback_model or "").strip()
    if not model:
        return None
    base_url = (settings.llm_fallback_base_url or settings.llm_base_url).rstrip("/")
    api_key = settings.llm_api_key
    if api_key is not None and not api_key.strip():
        api_key = None
    return LlmEndpoint(
        name="fallback",
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_type=settings.llm_fallback_api_type,
    )


async def health_check(endpoint: LlmEndpoint, timeout: float = 8) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{endpoint.base_url}/models", headers=auth_headers(endpoint.api_key))
        response.raise_for_status()
    return {"configured": True, "ok": True, "model": endpoint.model, "api_type": endpoint.api_type}


def response_text(data: dict[str, Any]) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text:
                return text
    raise ValueError("Responses API response did not contain output text")


async def endpoint_completion(
    endpoint: LlmEndpoint,
    *,
    system_content: str,
    user_content: str,
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    headers = auth_headers(endpoint.api_key)
    if endpoint.api_type == "responses":
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "instructions": system_content,
            "input": user_content,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{endpoint.base_url}/responses", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        return {"choices": [{"message": {"content": response_text(data)}}], "response": data}

    payload = {
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
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{endpoint.base_url}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


async def test_endpoint(endpoint: LlmEndpoint, timeout: float = 30) -> dict[str, Any]:
    await endpoint_completion(
        endpoint,
        system_content="Reply with JSON only.",
        user_content='Return exactly {"ok":true}.',
        temperature=0,
        max_tokens=32,
        response_format={"type": "json_object"},
        timeout=timeout,
    )
    return {"ok": True, "model": endpoint.model, "api_type": endpoint.api_type}


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
        try:
            response = await endpoint_completion(
                endpoint,
                system_content=system_content,
                user_content=user_content,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                timeout=timeout,
            )
            return response, endpoint.name
        except Exception as exc:
            last_error = exc
            continue
    assert last_error is not None
    raise last_error
