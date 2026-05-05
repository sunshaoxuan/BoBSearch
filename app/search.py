from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .config import Settings
from .models import IndexerStatus, RelevanceSummary, SearchResponse, SearchResult, SourceItem


VIDEO_CATEGORIES = {
    "all": None,
    "movies": ["2000"],
    "tv": ["5000"],
    "anime": ["5070"],
}
MAX_LLM_ITEMS = 15
CJK_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass
class RawItem:
    data: dict[str, Any]
    indexer_id: str


def load_indexers(settings: Settings) -> list[str]:
    try:
        names = []
        for name in os.listdir(settings.jackett_indexers_dir):
            if name.endswith(".json"):
                names.append(name.removesuffix(".json"))
        return sorted(set(names))
    except FileNotFoundError:
        return []


async def search_jackett(settings: Settings, query: str, category: str) -> tuple[list[RawItem], list[IndexerStatus]]:
    indexers = load_indexers(settings)
    sem = asyncio.Semaphore(settings.search_concurrency)
    timeout = httpx.Timeout(settings.indexer_timeout_seconds)
    raw: list[RawItem] = []
    statuses: list[IndexerStatus] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        async def one(indexer_id: str) -> None:
            started = time.monotonic()
            async with sem:
                params = build_jackett_params(settings.jackett_api_key, query, category)
                url = f"{settings.jackett_url.rstrip('/')}/api/v2.0/indexers/{indexer_id}/results"
                try:
                    r = await client.get(url, params=params)
                    r.raise_for_status()
                    payload = r.json()
                    results = payload.get("Results") or []
                    raw.extend(RawItem(item, indexer_id) for item in results)
                    statuses.append(
                        IndexerStatus(
                            id=indexer_id,
                            name=indexer_id,
                            status="ok" if results else "empty",
                            count=len(results),
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )
                    )
                except Exception as exc:
                    statuses.append(
                        IndexerStatus(
                            id=indexer_id,
                            name=indexer_id,
                            status="error",
                            error=f"{type(exc).__name__}: {str(exc)[:180]}",
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )
                    )

        try:
            await asyncio.wait_for(asyncio.gather(*(one(i) for i in indexers)), timeout=settings.total_timeout_seconds)
        except asyncio.TimeoutError:
            statuses.append(IndexerStatus(id="_search", status="timeout", error="Total search timeout reached"))

    return raw, sorted(statuses, key=lambda s: (s.status != "ok", s.id))


def build_jackett_params(api_key: str, query: str, category: str) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("apikey", api_key), ("Query", query)]
    for cat in VIDEO_CATEGORIES.get(category) or []:
        params.append(("Category[]", cat))
    return params


def magnet_hash(magnet: str | None) -> str | None:
    if not magnet:
        return None
    parsed = urlparse(magnet)
    qs = parse_qs(parsed.query)
    for xt in qs.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            return xt.split(":")[-1].upper()
    return None


def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"[\W_]+", " ", title, flags=re.UNICODE)
    return re.sub(r"\s+", " ", title).strip()


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def cjk_chunks(value: str) -> list[str]:
    chunks = re.findall(r"[\u3400-\u9fff]+", value)
    return [chunk for chunk in chunks if chunk]


def cjk_bigrams(value: str) -> set[str]:
    chars = CJK_RE.findall(value)
    if len(chars) < 2:
        return set(chars)
    return {"".join(chars[i : i + 2]) for i in range(len(chars) - 1)}


def latin_tokens(value: str) -> list[str]:
    tokens = [token.casefold() for token in LATIN_TOKEN_RE.findall(value)]
    return [token for token in tokens if len(token) >= 2]


def relevance_text(result: SearchResult) -> str:
    parts = [
        result.title,
        result.normalized_name or "",
        result.category or "",
        result.info_hash or "",
        " ".join(result.trackers),
        " ".join(result.tags),
        " ".join(result.quality_flags),
    ]
    for source in result.sources:
        parts.extend([source.title, source.tracker or "", source.category or "", urlparse(source.details or "").netloc])
    return " ".join(part for part in parts if part)


def score_relevance(query: str, result: SearchResult) -> tuple[float, str, list[str]]:
    haystack = compact_text(relevance_text(result))
    normalized_query = compact_text(query)
    score = 0.0
    reasons: list[str] = []

    if normalized_query and normalized_query in haystack:
        score += 1.0
        reasons.append("完整关键词命中")

    query_cjk_chunks = cjk_chunks(query)
    for chunk in query_cjk_chunks:
        folded = compact_text(chunk)
        if folded and folded in haystack:
            score += min(0.9, 0.25 + len(folded) * 0.08)
            reasons.append(f"中文片段命中: {chunk}")

    query_bigrams = cjk_bigrams(query)
    if query_bigrams:
        haystack_bigrams = cjk_bigrams(haystack)
        matched = query_bigrams & haystack_bigrams
        if matched:
            ratio = len(matched) / len(query_bigrams)
            score += min(0.85, ratio * 0.85)
            reasons.append(f"中文字符组命中 {len(matched)}/{len(query_bigrams)}")

    tokens = latin_tokens(query)
    if tokens:
        matched_tokens = [token for token in tokens if token in haystack]
        if matched_tokens:
            score += min(0.9, len(matched_tokens) / len(tokens) * 0.9)
            reasons.append("英文/数字 token 命中: " + ", ".join(matched_tokens[:6]))

    score = min(score, 1.0)
    if score >= 0.72:
        level = "high"
    elif score >= 0.32:
        level = "medium"
    else:
        level = "low"
        if not reasons:
            reasons.append("未命中关键词")
    return round(score, 3), level, reasons[:6]


def apply_relevance(query: str, results: list[SearchResult]) -> RelevanceSummary:
    summary = RelevanceSummary()
    for result in results:
        score, level, reasons = score_relevance(query, result)
        result.relevance_score = score
        result.relevance_level = level
        result.relevance_reasons = reasons
        if level == "high":
            summary.high += 1
        elif level == "medium":
            summary.medium += 1
        else:
            summary.low += 1
    return summary


def dedup_key(item: dict[str, Any]) -> str:
    info_hash = (item.get("InfoHash") or "").upper() or magnet_hash(item.get("MagnetUri"))
    if info_hash:
        return f"hash:{info_hash}"
    guid = item.get("Guid") or item.get("Link") or item.get("Details")
    if guid:
        return f"guid:{guid}"
    return f"title-size:{normalize_title(item.get('Title') or '')}:{item.get('Size') or 0}"


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def make_token(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def dedupe(raw: list[RawItem]) -> list[SearchResult]:
    grouped: dict[str, list[RawItem]] = {}
    for item in raw:
        grouped.setdefault(dedup_key(item.data), []).append(item)

    results: list[SearchResult] = []
    for key, items in grouped.items():
        best = max(items, key=lambda x: as_int(x.data.get("Seeders")) or -1).data
        trackers = sorted({(i.data.get("Tracker") or i.indexer_id) for i in items if i.data.get("Tracker") or i.indexer_id})
        sources = [
            SourceItem(
                tracker=i.data.get("Tracker"),
                tracker_id=i.data.get("TrackerId") or i.indexer_id,
                title=i.data.get("Title") or "",
                size=as_int(i.data.get("Size")),
                seeders=as_int(i.data.get("Seeders")),
                peers=as_int(i.data.get("Peers")),
                publish_date=i.data.get("PublishDate"),
                category=i.data.get("CategoryDesc"),
                details=i.data.get("Details"),
            )
            for i in items
        ]
        info_hash = (best.get("InfoHash") or "").upper() or magnet_hash(best.get("MagnetUri"))
        results.append(
            SearchResult(
                token=make_token(key),
                title=best.get("Title") or "",
                info_hash=info_hash,
                size=as_int(best.get("Size")),
                seeders=max([as_int(i.data.get("Seeders")) or -1 for i in items] or [-1]),
                peers=max([as_int(i.data.get("Peers")) or -1 for i in items] or [-1]),
                publish_date=best.get("PublishDate"),
                category=best.get("CategoryDesc"),
                magnet_uri=best.get("MagnetUri"),
                link=best.get("Link"),
                details=best.get("Details"),
                trackers=trackers,
                sources=sources,
            )
        )
    return sorted(results, key=lambda r: ((r.seeders or -1), len(r.sources), r.publish_date or ""), reverse=True)


def llm_payload(results: list[SearchResult]) -> list[dict[str, Any]]:
    payload = []
    for r in results[:MAX_LLM_ITEMS]:
        payload.append(
            {
                "token": r.token,
                "title": r.title,
                "size": r.size,
                "seeders": r.seeders,
                "peers": r.peers,
                "publish_date": r.publish_date,
                "category": r.category,
                "info_hash": r.info_hash,
                "trackers": r.trackers,
                "source_count": len(r.sources),
                "details_domains": sorted({urlparse(s.details or "").netloc for s in r.sources if s.details}),
            }
        )
    return payload


async def enrich_with_llm(settings: Settings, results: list[SearchResult]) -> str | None:
    if not results:
        return None
    prompt = {
        "task": "Summarize torrent/search results for deduped media resources. Return strict JSON only.",
        "schema": {
            "items": [
                {
                    "token": "string",
                    "normalized_name": "string",
                    "tags": ["resolution/source/codec/audio/subtitle/HDR/etc"],
                    "quality_flags": ["string"],
                    "recommendation": "short Chinese note",
                    "group_note": "short Chinese note",
                }
            ]
        },
        "rules": [
            "Do not invent unavailable fields.",
            "Use Chinese for notes.",
            "Never include or request magnet links.",
            "quality_flags is only for suspicious or low-quality signals; do not repeat size, seeders, peers, or source_count there.",
        ],
        "results": llm_payload(results),
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": [
                        {"role": "system", "content": "You return valid compact JSON only."},
                        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 2048,
                    "response_format": {"type": "json_object"},
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return "LLM response did not contain items"
            by_token = {r.token: r for r in results}
            for item in items:
                if not isinstance(item, dict) or item.get("token") not in by_token:
                    continue
                target = by_token[item["token"]]
                target.normalized_name = item.get("normalized_name")
                target.tags = [str(x) for x in item.get("tags") or []][:12]
                target.quality_flags = [str(x) for x in item.get("quality_flags") or []][:8]
                target.recommendation = item.get("recommendation")
                target.group_note = item.get("group_note")
            return None
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:220]}"


async def search_and_enrich(settings: Settings, query: str, category: str) -> SearchResponse:
    raw, statuses = await search_jackett(settings, query, category)
    results = dedupe(raw)
    relevance_summary = apply_relevance(query, results)
    llm_candidates = sorted(results, key=lambda r: (r.relevance_score, r.seeders or -1, len(r.sources)), reverse=True)
    llm_error = await enrich_with_llm(settings, llm_candidates)
    relevance_summary = apply_relevance(query, results)
    return SearchResponse(
        query=query,
        total_raw=len(raw),
        total_deduped=len(results),
        results=results,
        indexers=statuses,
        relevance_summary=relevance_summary,
        llm_error=llm_error,
    )
