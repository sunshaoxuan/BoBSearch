from __future__ import annotations

from pydantic import BaseModel, Field


class IndexerStatus(BaseModel):
    id: str
    name: str | None = None
    status: str
    count: int = 0
    error: str | None = None
    elapsed_ms: int | None = None


class SourceItem(BaseModel):
    tracker: str | None = None
    tracker_id: str | None = None
    title: str
    size: int | None = None
    seeders: int | None = None
    peers: int | None = None
    publish_date: str | None = None
    category: str | None = None
    details: str | None = None


class SearchResult(BaseModel):
    token: str
    title: str
    normalized_name: str | None = None
    info_hash: str | None = None
    size: int | None = None
    seeders: int | None = None
    peers: int | None = None
    publish_date: str | None = None
    category: str | None = None
    magnet_uri: str | None = None
    link: str | None = None
    details: str | None = None
    trackers: list[str] = Field(default_factory=list)
    sources: list[SourceItem] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    group_note: str | None = None
    relevance_score: float = 0.0
    relevance_level: str = "low"
    relevance_reasons: list[str] = Field(default_factory=list)


class RelevanceSummary(BaseModel):
    high: int = 0
    medium: int = 0
    low: int = 0


class SearchResponse(BaseModel):
    query: str
    total_raw: int
    total_deduped: int
    results: list[SearchResult]
    indexers: list[IndexerStatus]
    relevance_summary: RelevanceSummary = Field(default_factory=RelevanceSummary)
    llm_error: str | None = None
    history_id: str | None = None


class QbitTorrent(BaseModel):
    hash: str
    name: str
    state: str | None = None
    progress: float = 0.0
    size: int = 0
    completed: int = 0
    save_path: str | None = None
    content_path: str | None = None
    download_speed: int = 0
    upload_speed: int = 0
    eta: int = 0
    added_on: int | None = None
    completion_on: int | None = None
    category: str | None = None
    is_complete: bool = False


class QbitFileNode(BaseModel):
    path: str
    name: str
    type: str
    size: int = 0
    progress: float = 0.0
    priority: int | None = None
    movable: bool = True
    children: list["QbitFileNode"] = Field(default_factory=list)


class MoveSelectedRequest(BaseModel):
    selected_paths: list[str]
    target_category: str
    target_folder: str
    rename_plan: dict | None = None


class AddMagnetRequest(BaseModel):
    magnet_uri: str


class RefreshTargetsRequest(BaseModel):
    targets: list[dict]


class TargetSuggestionsRequest(BaseModel):
    query: str
    file_names: list[str] = Field(default_factory=list)


class MoveSelectedResponse(BaseModel):
    ok: bool
    moved: list[dict[str, str]] = Field(default_factory=list)
    message: str
