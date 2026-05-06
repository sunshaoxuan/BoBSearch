from __future__ import annotations

from app.config import Settings
from app.history import SearchHistoryStore
from app.models import SearchResponse, SearchResult


def settings(tmp_path, limit=30):
    return Settings(
        web_password="web",
        session_secret="session",
        jackett_api_key="jackett",
        qbit_password="qbit",
        llm_base_url="http://llm.test/v1",
        llm_api_key="llm",
        app_data_dir=str(tmp_path),
        search_history_path=str(tmp_path / "search-history.json"),
        search_history_limit=limit,
    )


def response(query: str, token: str = "token") -> SearchResponse:
    return SearchResponse(
        query=query,
        total_raw=1,
        total_deduped=1,
        results=[SearchResult(token=token, title=f"{query} result", magnet_uri=f"magnet:?xt=urn:btih:{token}")],
        indexers=[],
    )


def test_history_save_latest_and_restore_response(tmp_path):
    store = SearchHistoryStore(settings(tmp_path))
    item = store.save(response("ubuntu", "abc"), "all", "seeders")

    assert store.list_items()[0]["id"] == item["id"]
    assert store.latest()["id"] == item["id"]
    restored = store.get_response(item["id"])
    assert restored.query == "ubuntu"
    assert restored.history_id == item["id"]
    assert restored.results[0].magnet_uri == "magnet:?xt=urn:btih:abc"


def test_history_updates_same_query_category_sort(tmp_path):
    store = SearchHistoryStore(settings(tmp_path))
    first = store.save(response("ubuntu", "one"), "all", "seeders")
    second = store.save(response("ubuntu", "two"), "all", "seeders")

    assert first["id"] == second["id"]
    assert len(store.list_items()) == 1
    assert store.find_result(second["id"], "two").title == "ubuntu result"
    assert store.find_result(second["id"], "one") is None


def test_history_trims_to_limit(tmp_path):
    store = SearchHistoryStore(settings(tmp_path, limit=2))
    first = store.save(response("one"), "all", "seeders")
    second = store.save(response("two"), "all", "seeders")
    third = store.save(response("three"), "all", "seeders")

    ids = [item["id"] for item in store.list_items()]
    assert ids == [third["id"], second["id"]]
    assert first["id"] not in ids


def test_history_quarantines_corrupt_json(tmp_path):
    cfg = settings(tmp_path)
    path = tmp_path / "search-history.json"
    path.write_text("{bad json", encoding="utf-8")

    store = SearchHistoryStore(cfg)
    assert store.list_items() == []
    assert not path.exists()
    assert list(tmp_path.glob("search-history.json.corrupt.*"))
