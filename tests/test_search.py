import asyncio
import json

import app.search as search_module
from app.models import IndexerStatus
from app.search import apply_relevance_queries, build_jackett_params, dedupe, llm_payload, magnet_hash, RawItem, score_relevance, search_and_enrich_queries, suggest_search_keywords


def test_magnet_hash_extracts_btih():
    magnet = "magnet:?xt=urn:btih:abc123&dn=test"
    assert magnet_hash(magnet) == "ABC123"


def test_dedupe_by_info_hash_merges_sources():
    raw = [
        RawItem(
            {
                "Title": "Ubuntu ISO",
                "InfoHash": "abc",
                "Tracker": "A",
                "TrackerId": "a",
                "Size": 10,
                "Seeders": 5,
                "Peers": 7,
                "MagnetUri": "magnet:?xt=urn:btih:abc",
            },
            "a",
        ),
        RawItem(
            {
                "Title": "Ubuntu ISO duplicate",
                "InfoHash": "ABC",
                "Tracker": "B",
                "TrackerId": "b",
                "Size": 10,
                "Seeders": 9,
                "Peers": 11,
                "MagnetUri": "magnet:?xt=urn:btih:abc",
            },
            "b",
        ),
    ]
    results = dedupe(raw)
    assert len(results) == 1
    assert results[0].seeders == 9
    assert results[0].trackers == ["A", "B"]
    assert len(results[0].sources) == 2


def test_llm_payload_does_not_include_magnet():
    raw = [
        RawItem(
            {
                "Title": "Ubuntu ISO",
                "InfoHash": "abc",
                "Tracker": "A",
                "TrackerId": "a",
                "Size": 10,
                "Seeders": 5,
                "Peers": 7,
                "MagnetUri": "magnet:?xt=urn:btih:abc&dn=secret",
                "Details": "https://example.com/item",
            },
            "a",
        )
    ]
    payload = llm_payload(dedupe(raw))
    text = str(payload)
    assert "magnet:" not in text
    assert "example.com" in text


def test_jackett_params_include_original_chinese_query():
    params = build_jackett_params("secret", "飞驰人生3", "movies")
    assert ("Query", "飞驰人生3") in params
    assert ("Category[]", "2000") in params


def test_chinese_unrelated_title_is_low_relevance():
    result = dedupe([RawItem({"Title": "Project Hail Mary 2026 1080p WEB", "Guid": "a"}, "a")])[0]
    score, level, reasons = score_relevance("不存在中文测试甲乙丙丁", result)
    assert score == 0
    assert level == "low"
    assert reasons == ["未命中关键词"]


def test_chinese_matching_title_is_high_relevance():
    result = dedupe([RawItem({"Title": "飞驰人生3 2026 2160p WEB-DL", "Guid": "a"}, "a")])[0]
    score, level, reasons = score_relevance("飞驰人生3", result)
    assert score >= 0.72
    assert level == "high"
    assert any("完整关键词命中" in reason for reason in reasons)


def test_mixed_cjk_latin_and_number_relevance():
    result = dedupe([RawItem({"Title": "飞驰人生 3 Pegasus 2026 1080p WEB-DL", "Guid": "a"}, "a")])[0]
    score, level, reasons = score_relevance("飞驰人生3 1080p", result)
    assert score >= 0.72
    assert level == "high"
    assert any("英文/数字 token" in reason for reason in reasons)


def test_multi_query_relevance_uses_best_alias():
    result = dedupe([RawItem({"Title": "Once Upon A Time in the Middle East 2026 1080p", "Guid": "a"}, "a")])[0]
    summary = apply_relevance_queries(["欢迎来龙餐馆", "Once Upon A Time in the Middle East"], [result])

    assert summary.high == 1
    assert result.relevance_level == "high"
    assert result.relevance_reasons[0] == "匹配搜索词: Once Upon A Time in the Middle East"


def test_keyword_suggestions_are_deduplicated_and_bounded(monkeypatch):
    async def fake_evidence(description, category):
        return ["欢迎来龙餐馆 (豆瓣)", "Once Upon a Time in the Middle East"]

    async def fake_completion(*args, **kwargs):
        payload = {
            "summary": "识别为欢迎来龙餐馆",
            "candidates": [
                {"keyword": "欢迎来龙餐馆", "label": "正式中文名", "kind": "official_cn", "reason": "演员与剧情匹配", "confidence": 0.98},
                {"keyword": "欢迎来龙餐馆", "label": "重复项", "confidence": 0.5},
                {"keyword": "Once Upon A Time in the Middle East", "label": "英文名", "kind": "english", "confidence": 0.94},
            ],
        }
        return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}, "primary"

    monkeypatch.setattr(search_module, "web_title_evidence", fake_evidence)
    monkeypatch.setattr(search_module, "chat_completion", fake_completion)
    result = asyncio.run(suggest_search_keywords(None, "沈腾和蒋奇明演的，发生在阿拉伯地区", "movies"))

    assert result.summary == "识别为欢迎来龙餐馆"
    assert [item.keyword for item in result.candidates] == ["欢迎来龙餐馆", "Once Upon A Time in the Middle East"]


def test_multi_query_search_merges_and_deduplicates(monkeypatch):
    async def fake_search(settings, query, category):
        common = RawItem({"Title": f"{query} 2026", "InfoHash": "same", "Seeders": 3}, query)
        unique = RawItem({"Title": f"{query} unique", "Guid": query, "Seeders": 1}, query)
        return [common, unique], [IndexerStatus(id="source", status="ok", count=2)]

    async def fake_enrich(settings, results):
        return None

    monkeypatch.setattr(search_module, "search_jackett", fake_search)
    monkeypatch.setattr(search_module, "enrich_with_llm", fake_enrich)
    response = asyncio.run(search_and_enrich_queries(None, ["中文片名", "English Title"], "movies"))

    assert response.query == "中文片名 | English Title"
    assert response.total_raw == 4
    assert response.total_deduped == 3
    assert response.indexers[0].count == 4
