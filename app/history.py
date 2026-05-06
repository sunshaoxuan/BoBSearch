from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .models import SearchResponse, SearchResult


class SearchHistoryStore:
    def __init__(self, settings: Settings):
        self.path = Path(settings.search_history_path or Path(settings.app_data_dir) / "search-history.json")
        self.limit = max(1, settings.search_history_limit)

    def history_id(self, query: str, category: str, sort: str) -> str:
        payload = "\0".join([query.strip(), category.strip() or "all", sort.strip() or "seeders"])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def list_items(self) -> list[dict[str, Any]]:
        data = self._read()
        return [self._metadata(item) for item in data["items"]]

    def latest(self) -> dict[str, Any] | None:
        data = self._read()
        return data["items"][0] if data["items"] else None

    def get(self, history_id: str) -> dict[str, Any] | None:
        data = self._read()
        return next((item for item in data["items"] if item.get("id") == history_id), None)

    def get_response(self, history_id: str) -> SearchResponse | None:
        item = self.get(history_id)
        if not item:
            return None
        response = SearchResponse.model_validate(item["response"])
        response.history_id = history_id
        return response

    def find_result(self, history_id: str, token: str) -> SearchResult | None:
        response = self.get_response(history_id)
        if not response:
            return None
        return next((result for result in response.results if result.token == token), None)

    def save(self, response: SearchResponse, category: str, sort: str) -> dict[str, Any]:
        data = self._read()
        now = self._now()
        history_id = self.history_id(response.query, category, sort)
        response.history_id = history_id
        existing = next((item for item in data["items"] if item.get("id") == history_id), None)
        created_at = existing.get("created_at") if existing else now
        item = {
            "id": history_id,
            "query": response.query,
            "category": category,
            "sort": sort,
            "created_at": created_at,
            "updated_at": now,
            "total_raw": response.total_raw,
            "total_deduped": response.total_deduped,
            "result_count": len(response.results),
            "response": response.model_dump(mode="json"),
        }
        data["items"] = [item] + [old for old in data["items"] if old.get("id") != history_id]
        data["items"] = data["items"][: self.limit]
        self._write(data)
        return self._metadata(item)

    def _metadata(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "query": item.get("query", ""),
            "category": item.get("category", "all"),
            "sort": item.get("sort", "seeders"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "total_raw": item.get("total_raw", 0),
            "total_deduped": item.get("total_deduped", 0),
            "result_count": item.get("result_count", 0),
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "items": []}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            self._quarantine_corrupt_file()
            return {"version": 1, "items": []}
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            self._quarantine_corrupt_file()
            return {"version": 1, "items": []}
        return {"version": 1, "items": data["items"]}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod_quietly(self.path.parent, 0o700)
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.path)
        self._chmod_quietly(self.path, 0o600)

    def _quarantine_corrupt_file(self) -> None:
        if not self.path.exists():
            return
        target = self.path.with_name(f"{self.path.name}.corrupt.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
        try:
            os.replace(self.path, target)
        except OSError:
            pass

    def _chmod_quietly(self, path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            pass

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
