from app.search import build_jackett_params, dedupe, llm_payload, magnet_hash, RawItem, score_relevance


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
