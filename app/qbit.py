from __future__ import annotations

import shutil
import re
import json
import html
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote_plus

import httpx

from .config import Settings
from .models import QbitFileNode, QbitTorrent, SearchResult


ALLOWED_JELLYFIN_CATEGORIES = {"movies", "series", "other"}
TARGET_CATEGORY_ORDER = ("movies", "series", "other")
NAMING_EXAMPLE_LIMIT = 80
TMDB_CANDIDATE_LIMIT = 6


def safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.strip().replace("\\", "/"))
    if not str(path) or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe relative path: {value}")
    return path


def safe_target_folder(value: str) -> PurePosixPath:
    return safe_relative_path(value)


def ensure_inside(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise ValueError(f"Path escapes allowed root: {path}")
    return resolved


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(1, 1000):
        candidate = parent / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not create unique destination for {path}")


def compress_selected_paths(paths: list[str]) -> list[PurePosixPath]:
    selected = sorted({safe_relative_path(path) for path in paths}, key=lambda p: (len(p.parts), str(p)))
    compressed: list[PurePosixPath] = []
    for path in selected:
        if any(path == parent or path.is_relative_to(parent) for parent in compressed):
            continue
        compressed.append(path)
    return compressed


def build_file_tree(files: list[dict[str, Any]]) -> list[QbitFileNode]:
    root: dict[str, Any] = {"children": {}, "node": None}
    for item in files:
        rel = safe_relative_path(str(item.get("name") or ""))
        current = root
        built: list[str] = []
        for part in rel.parts[:-1]:
            built.append(part)
            current = current["children"].setdefault(
                part,
                {
                    "children": {},
                    "node": QbitFileNode(path="/".join(built), name=part, type="directory", movable=True),
                },
            )
        file_name = rel.parts[-1]
        file_node = QbitFileNode(
            path=str(rel),
            name=file_name,
            type="file",
            size=int(item.get("size") or 0),
            progress=float(item.get("progress") or 0),
            priority=int(item.get("priority")) if item.get("priority") is not None else None,
            movable=True,
        )
        current["children"][file_name] = {"children": {}, "node": file_node}

    def finish(entry: dict[str, Any]) -> list[QbitFileNode]:
        nodes: list[QbitFileNode] = []
        for child in sorted(entry["children"].values(), key=lambda x: (x["node"].type != "directory", x["node"].name.casefold())):
            node: QbitFileNode = child["node"]
            node.children = finish(child)
            if node.type == "directory":
                node.size = sum(descendant_size(grandchild) for grandchild in node.children)
                leaf_progress = [leaf.progress for leaf in flatten_files(node)]
                node.progress = min(leaf_progress) if leaf_progress else 0
            nodes.append(node)
        return nodes

    return finish(root)


def descendant_size(node: QbitFileNode) -> int:
    if node.type == "file":
        return node.size
    return sum(descendant_size(child) for child in node.children)


def flatten_files(node: QbitFileNode) -> list[QbitFileNode]:
    if node.type == "file":
        return [node]
    result: list[QbitFileNode] = []
    for child in node.children:
        result.extend(flatten_files(child))
    return result


def normalize_media_text(value: str) -> str:
    return "".join(re.findall(r"[\u3400-\u9fffA-Za-z0-9]+", value.casefold()))


def display_name_without_ids(folder: str) -> str:
    without_id = re.sub(r"\s*\[(?:tmdbid-)?\d+\]\s*$", "", folder).strip()
    return re.sub(r"\s*\(\d{4}\)\s*$", "", without_id).strip()


def folder_year(folder: str) -> str | None:
    match = re.search(r"\((19\d{2}|20\d{2})\)", folder)
    return match.group(1) if match else None


def release_years(query: str) -> set[str]:
    return set(re.findall(r"(?<!\d)(?:19\d{2}|20\d{2})(?!\d)", query))


def has_sequel_suffix_after_title(query_norm: str, folder_norm: str) -> bool:
    if not folder_norm or folder_norm[-1:].isdigit():
        return False
    start = query_norm.find(folder_norm)
    while start >= 0:
        end = start + len(folder_norm)
        if end < len(query_norm) and query_norm[end].isdigit():
            return True
        start = query_norm.find(folder_norm, start + 1)
    return False


def generated_folder_name(query: str) -> str:
    stem = Path(query).stem
    chunks = re.findall(r"[\u3400-\u9fff][\u3400-\u9fffA-Za-z0-9：·・]+", stem)
    digit_chunks = [chunk for chunk in chunks if re.search(r"\d", chunk)]
    if digit_chunks:
        return max(digit_chunks, key=len)
    if chunks:
        return max(chunks, key=len)
    cleaned = re.sub(r"[\[\](){}【】].*?[\[\](){}【】]?", " ", stem)
    cleaned = re.sub(r"(?i)\b(2160p|1080p|720p|web-?dl|bluray|hdr|x26[45]|aac|mkv|mp4)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")
    return cleaned[:80] or stem[:80] or "未命名媒体"


def folder_name_from_llm_target(target: dict[str, Any]) -> dict[str, Any] | None:
    category = str(target.get("category") or "movies").strip()
    title = str(target.get("title") or "").strip()
    year = str(target.get("year") or "").strip()
    tmdb_id = str(target.get("tmdb_id") or target.get("tmdbid") or "").strip()
    folder = str(target.get("folder") or "").strip()
    reason = str(target.get("reason") or "LLM 根据 TMDb 信息生成").strip()
    if category not in ALLOWED_JELLYFIN_CATEGORIES or not title or not re.fullmatch(r"\d{4}", year) or not re.fullmatch(r"\d+", tmdb_id):
        return None
    expected = f"{title} ({year}) [tmdbid-{tmdb_id}]"
    if not folder:
        folder = expected
    if not re.fullmatch(r".+ \(\d{4}\) \[tmdbid-\d+\]", folder):
        folder = expected
    try:
        safe_target_folder(folder)
    except ValueError:
        return None
    return {
        "category": category,
        "folder": folder,
        "score": float(target.get("score") or 0.86),
        "reason": reason[:120],
        "existing": False,
    }


def naming_examples(settings: Settings, limit: int = NAMING_EXAMPLE_LIMIT) -> list[dict[str, str]]:
    examples: list[dict[str, str]] = []
    library = Path(settings.jellyfin_library_path)
    pattern = re.compile(r".+ \(\d{4}\) \[tmdbid-\d+\]$")
    for category in TARGET_CATEGORY_ORDER:
        root = library / category
        if not root.exists():
            continue
        for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if child.is_dir() and pattern.fullmatch(child.name):
                examples.append({"category": category, "folder": child.name})
                if len(examples) >= limit:
                    return examples
    return examples


def release_search_text(query: str) -> str:
    stem = Path(query).stem
    text = re.sub(r"[\[\]【】（）(){}]", " ", stem)
    text = re.sub(r"(?i)\b(2160p|1080p|720p|480p|web-?dl|itunes|bluray|bdrip|hdr|dv|x26[45]|h\.?26[45]|aac|ddp?5\.1|atmos|mkv|mp4|dreamhd)\b", " ", text)
    text = re.sub(r"(?i)\b(www\.[a-z0-9.-]+|com|org|net)\b", " ", text)
    text = re.sub(r"[-_.★]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    english = re.search(r"([A-Za-z][A-Za-z0-9' ]{2,}?)\s+(19\d{2}|20\d{2})", text)
    if english:
        return f"{english.group(1).strip()} {english.group(2)}"
    chinese = re.search(r"([\u3400-\u9fff][\u3400-\u9fffA-Za-z0-9：·・ ]{1,}?)\s*(19\d{2}|20\d{2})?", text)
    if chinese:
        return " ".join(part for part in chinese.groups() if part).strip()
    return text[:120]


def parse_tmdb_page(kind: str, tmdb_id: str, url: str, body: str) -> dict[str, Any] | None:
    title_match = re.search(r"<title>\s*(.*?)\s*(?:—|-|&#8212;)\s*The Movie Database", body, flags=re.S)
    if not title_match:
        return None
    title_text = html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
    match = re.search(r"(.+?)\s*\((\d{4})\)", title_text)
    if not match:
        return None
    title = match.group(1).strip().strip('"“”')
    year = match.group(2)
    if not title:
        return None
    return {
        "kind": kind,
        "category": "series" if kind == "tv" else "movies",
        "title": title,
        "year": year,
        "tmdb_id": tmdb_id,
        "url": url,
    }


async def tmdb_web_candidates(query: str) -> list[dict[str, Any]]:
    search_text = release_search_text(query)
    if len(search_text) < 2:
        return []
    search_url = f"https://lite.duckduckgo.com/lite/?q={quote_plus(search_text + ' TMDb')}"
    headers = {"User-Agent": "Mozilla/5.0"}
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers=headers) as client:
            r = await client.get(search_url)
            r.raise_for_status()
            matches = re.findall(r"themoviedb\.org/(movie|tv)/(\d+)(?:-[A-Za-z0-9-]+)?", r.text)
            for kind, tmdb_id in matches:
                key = (kind, tmdb_id)
                if key in seen:
                    continue
                seen.add(key)
                url = f"https://www.themoviedb.org/{kind}/{tmdb_id}?language=en-US"
                page = await client.get(url)
                if page.status_code >= 400:
                    continue
                parsed = parse_tmdb_page(kind, tmdb_id, url, page.text)
                if parsed:
                    candidates.append(parsed)
                if len(candidates) >= TMDB_CANDIDATE_LIMIT:
                    break
    except Exception:
        return candidates
    return candidates


async def llm_generated_target_suggestion(query: str, settings: Settings) -> dict[str, Any] | None:
    tmdb_candidates = await tmdb_web_candidates(query)
    prompt = {
        "task": "Identify the media in a qBittorrent release name and generate one Jellyfin folder target.",
        "release_name": query,
        "cleaned_search_text": release_search_text(query),
        "tmdb_candidates_from_web_search": tmdb_candidates,
        "existing_folder_style_examples": naming_examples(settings),
        "required_folder_format": "Title (YYYY) [tmdbid-NUMBER]",
        "rules": [
            "Find the actual movie/series title from the release name; ignore release groups, websites, codecs, quality, audio, subtitles and container text.",
            "Use TMDb as the ID source. Prefer the provided TMDb web-search candidates when they match the release name.",
            "For Chinese releases, prefer the title style already used by Jellyfin/TMDb Chinese metadata if clear; otherwise use the official English title.",
            "Do not return a folder based on release group names or website names.",
            "If the TMDb item cannot be identified confidently, return {\"target\": null}.",
            "Return strict JSON only.",
        ],
        "schema": {
            "target": {
                "category": "movies|series|other",
                "title": "folder title without year/id",
                "year": "YYYY",
                "tmdb_id": "digits only",
                "folder": "Title (YYYY) [tmdbid-NUMBER]",
                "score": 0.0,
                "reason": "short Chinese explanation",
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": [
                        {"role": "system", "content": "You return valid compact JSON only. Use TMDb IDs for folder naming."},
                        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                    ],
                    "temperature": 0.05,
                    "max_tokens": 900,
                    "response_format": {"type": "json_object"},
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            target = data.get("target") if isinstance(data, dict) else None
            if isinstance(target, dict):
                parsed = folder_name_from_llm_target(target)
                if parsed:
                    return parsed
            if len(tmdb_candidates) == 1:
                candidate = tmdb_candidates[0]
                return folder_name_from_llm_target(
                    {
                        "category": candidate["category"],
                        "title": candidate["title"],
                        "year": candidate["year"],
                        "tmdb_id": candidate["tmdb_id"],
                        "score": 0.82,
                        "reason": "TMDb 网站候选唯一匹配",
                    }
                )
            return None
    except Exception:
        if len(tmdb_candidates) == 1:
            candidate = tmdb_candidates[0]
            return folder_name_from_llm_target(
                {
                    "category": candidate["category"],
                    "title": candidate["title"],
                    "year": candidate["year"],
                    "tmdb_id": candidate["tmdb_id"],
                    "score": 0.82,
                    "reason": "TMDb 网站候选唯一匹配",
                }
            )
        return None


def target_score(query: str, folder: str) -> tuple[float, str]:
    query_norm = normalize_media_text(query)
    folder_title = display_name_without_ids(folder)
    folder_norm = normalize_media_text(folder_title)
    if not query_norm or not folder_norm:
        return 0.0, "无可匹配文本"
    years = release_years(query)
    year = folder_year(folder)
    year_mismatch = bool(year and years and year not in years)
    sequel_mismatch = has_sequel_suffix_after_title(query_norm, folder_norm)
    if sequel_mismatch:
        return 0.25, "任务名疑似续集，已有目录缺少续集编号"
    if folder_norm in query_norm:
        if year_mismatch:
            return 0.45, f"目录名命中但年份不一致：目录 {year}，任务 {', '.join(sorted(years))}"
        return 1.0, "目录名完整命中任务名"
    if query_norm in folder_norm:
        if year_mismatch:
            return 0.42, f"任务名命中但年份不一致：目录 {year}，任务 {', '.join(sorted(years))}"
        return 0.92, "任务名完整命中目录名"
    folder_bigrams = {folder_norm[i : i + 2] for i in range(max(0, len(folder_norm) - 1))}
    query_bigrams = {query_norm[i : i + 2] for i in range(max(0, len(query_norm) - 1))}
    if not folder_bigrams:
        return 0.0, "无匹配"
    matched = folder_bigrams & query_bigrams
    ratio = len(matched) / len(folder_bigrams)
    if ratio:
        score = min(0.88, ratio)
        if year_mismatch:
            score = min(0.42, score)
            return round(score, 3), f"片名字符组部分匹配但年份不一致：目录 {year}，任务 {', '.join(sorted(years))}"
        return round(score, 3), f"片名字符组匹配 {len(matched)}/{len(folder_bigrams)}"
    return 0.0, "无匹配"


def jellyfin_target_suggestions(query: str, settings: Settings, limit: int = 24, include_fallback: bool = True) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    library = Path(settings.jellyfin_library_path)
    for category in TARGET_CATEGORY_ORDER:
        root = library / category
        if not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            score, reason = target_score(query, child.name)
            if score <= 0:
                continue
            suggestions.append(
                {
                    "category": category,
                    "folder": child.name,
                    "score": score,
                    "reason": reason,
                    "existing": True,
                }
            )
    suggestions.sort(key=lambda item: (item["score"], item["category"] == "movies"), reverse=True)
    generated = generated_folder_name(query)
    if include_fallback and not any(item["category"] == "movies" and item["folder"] == generated for item in suggestions):
        suggestions.append(
            {
                "category": "movies",
                "folder": generated,
                "score": 0.2,
                "reason": "自动生成的新目录名",
                "existing": False,
            }
        )
    return suggestions[:limit]


async def jellyfin_target_suggestions_with_llm(query: str, settings: Settings, limit: int = 24) -> list[dict[str, Any]]:
    suggestions = jellyfin_target_suggestions(query, settings, limit=limit, include_fallback=False)
    llm_target = await llm_generated_target_suggestion(query, settings)
    if llm_target and not any(item["category"] == llm_target["category"] and item["folder"] == llm_target["folder"] for item in suggestions):
        suggestions.append(llm_target)
    suggestions.sort(key=lambda item: (item["score"], item["existing"], item["category"] == "movies"), reverse=True)
    return suggestions[:limit]


def qbit_path_to_local(path: str, settings: Settings) -> Path:
    qbit_path = PurePosixPath(path)
    mappings = [
        (PurePosixPath(settings.qbit_downloads_path), Path(settings.qbit_local_downloads_path)),
        (PurePosixPath(settings.qbit_extra_downloads_path), Path(settings.qbit_extra_local_downloads_path)),
    ]
    mappings.sort(key=lambda pair: len(pair[0].parts), reverse=True)
    for qbit_root, local_root in mappings:
        if qbit_path == qbit_root or qbit_root in qbit_path.parents:
            rel = qbit_path.relative_to(qbit_root)
            return ensure_inside(local_root.joinpath(*rel.parts), local_root)
    raise ValueError(f"qB path is outside downloads roots: {path}")


def jellyfin_target_path(category: str, folder: str, settings: Settings) -> Path:
    if category not in ALLOWED_JELLYFIN_CATEGORIES:
        raise ValueError("Unsupported Jellyfin category")
    rel = safe_target_folder(folder)
    root = Path(settings.jellyfin_library_path) / category
    return ensure_inside(root.joinpath(*rel.parts), Path(settings.jellyfin_library_path))


def move_selected_files(selected_paths: list[str], torrent: QbitTorrent, target_category: str, target_folder: str, settings: Settings) -> list[dict[str, str]]:
    base = qbit_path_to_local(torrent.save_path or settings.qbit_downloads_path, settings)
    target_root = jellyfin_target_path(target_category, target_folder, settings)
    target_root.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, str]] = []
    for rel in compress_selected_paths(selected_paths):
        source = ensure_inside(base.joinpath(*rel.parts), base)
        if not source.exists():
            raise FileNotFoundError(f"Selected path does not exist: {rel}")
        destination = unique_destination(target_root / rel.name)
        shutil.move(str(source), str(destination))
        moved.append({"source": str(source), "destination": str(destination)})
    return moved


class QbitClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=settings.qbit_url.rstrip("/"), timeout=15)
        self.logged_in = False

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self) -> None:
        r = await self.client.post(
            "/api/v2/auth/login",
            data={"username": self.settings.qbit_username, "password": self.settings.qbit_password},
        )
        r.raise_for_status()
        if r.text.strip() != "Ok.":
            raise RuntimeError("qBittorrent login failed")
        self.logged_in = True

    async def ensure_login(self) -> None:
        if not self.logged_in:
            await self.login()

    async def ensure_category(self) -> None:
        await self.ensure_login()
        r = await self.client.get("/api/v2/torrents/categories")
        r.raise_for_status()
        categories = r.json()
        if self.settings.qbit_category in categories:
            return
        r = await self.client.post(
            "/api/v2/torrents/createCategory",
            data={"category": self.settings.qbit_category, "savePath": self.settings.qbit_save_path},
        )
        r.raise_for_status()

    async def add_result(self, result: SearchResult) -> None:
        await self.ensure_category()
        url = result.magnet_uri or result.link
        if not url:
            raise ValueError("Result has no magnet or link")
        r = await self.client.post(
            "/api/v2/torrents/add",
            data={
                "urls": url,
                "category": self.settings.qbit_category,
                "paused": "false",
            },
        )
        r.raise_for_status()
        if r.text.strip().lower() not in {"ok.", "ok"}:
            raise RuntimeError(f"qBittorrent add failed: {r.text[:200]}")

    async def torrents(self) -> list[QbitTorrent]:
        await self.ensure_login()
        r = await self.client.get("/api/v2/torrents/info")
        r.raise_for_status()
        return [self._torrent_from_dict(item) for item in r.json()]

    async def torrent(self, torrent_hash: str) -> QbitTorrent:
        await self.ensure_login()
        r = await self.client.get("/api/v2/torrents/info", params={"hashes": torrent_hash})
        r.raise_for_status()
        for item in r.json():
            torrent = self._torrent_from_dict(item)
            if torrent.hash.lower() == torrent_hash.lower():
                return torrent
        raise ValueError("Torrent not found")

    async def files(self, torrent_hash: str) -> list[dict[str, Any]]:
        await self.ensure_login()
        await self.torrent(torrent_hash)
        r = await self.client.get("/api/v2/torrents/files", params={"hash": torrent_hash})
        r.raise_for_status()
        return r.json()

    async def file_tree(self, torrent_hash: str) -> list[QbitFileNode]:
        return build_file_tree(await self.files(torrent_hash))

    async def stop_torrent(self, torrent_hash: str) -> None:
        await self.ensure_login()
        for endpoint in ("/api/v2/torrents/stop", "/api/v2/torrents/pause"):
            r = await self.client.post(endpoint, data={"hashes": torrent_hash})
            if r.status_code == 404:
                continue
            r.raise_for_status()
            return
        raise RuntimeError("qBittorrent stop/pause endpoint unavailable")

    async def start_torrent(self, torrent_hash: str) -> None:
        await self.ensure_login()
        for endpoint in ("/api/v2/torrents/start", "/api/v2/torrents/resume"):
            r = await self.client.post(endpoint, data={"hashes": torrent_hash})
            if r.status_code == 404:
                continue
            r.raise_for_status()
            return
        raise RuntimeError("qBittorrent start/resume endpoint unavailable")

    async def delete_torrent(self, torrent_hash: str, delete_files: bool) -> None:
        await self.ensure_login()
        r = await self.client.post(
            "/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
        )
        r.raise_for_status()

    async def move_selected(self, torrent_hash: str, selected_paths: list[str], target_category: str, target_folder: str) -> list[dict[str, str]]:
        torrent = await self.torrent(torrent_hash)
        if not torrent.is_complete:
            raise ValueError("Torrent is not complete")
        if not selected_paths:
            raise ValueError("No files or folders selected")
        await self.stop_torrent(torrent_hash)
        moved = move_selected_files(selected_paths, torrent, target_category, target_folder, self.settings)
        await self.delete_torrent(torrent_hash, delete_files=True)
        return moved

    @staticmethod
    def _torrent_from_dict(item: dict[str, Any]) -> QbitTorrent:
        progress = float(item.get("progress") or 0)
        return QbitTorrent(
            hash=str(item.get("hash") or ""),
            name=str(item.get("name") or ""),
            state=item.get("state"),
            progress=progress,
            size=int(item.get("size") or 0),
            completed=int(item.get("completed") or 0),
            save_path=item.get("save_path"),
            content_path=item.get("content_path"),
            download_speed=int(item.get("dlspeed") or 0),
            upload_speed=int(item.get("upspeed") or 0),
            eta=int(item.get("eta") or 0),
            added_on=item.get("added_on"),
            completion_on=item.get("completion_on"),
            category=item.get("category"),
            is_complete=progress >= 1 or str(item.get("state") or "").lower() in {"uploading", "stalled_up", "pausedup", "checkingup", "forcedup", "queuedup"},
        )


async def qbit_health(settings: Settings) -> dict:
    client = QbitClient(settings)
    try:
        await client.ensure_login()
        r = await client.client.get("/api/v2/app/version")
        r.raise_for_status()
        return {"ok": True, "version": r.text.strip()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        await client.close()
