from __future__ import annotations

import asyncio
import shutil
import re
import json
import html
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote_plus

import httpx

from .config import Settings
from .llm_client import chat_completion
from .models import QbitFileNode, QbitTorrent, SearchResult


ALLOWED_JELLYFIN_CATEGORIES = {"movies", "series", "other"}
TARGET_CATEGORY_ORDER = ("movies", "series", "other")
NAMING_EXAMPLE_LIMIT = 80
TMDB_CANDIDATE_LIMIT = 6
MIN_EXISTING_TARGET_SCORE = 0.35
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup"}
FILE_COMPLETE_THRESHOLD = 1.0


class TorrentAlreadyExistsError(RuntimeError):
    pass


def safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.strip().replace("\\", "/"))
    if not str(path) or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe relative path: {value}")
    return path


def safe_target_folder(value: str) -> PurePosixPath:
    return safe_relative_path(value)


def result_info_hash(result: SearchResult) -> str | None:
    value = str(result.info_hash or "").strip()
    if value:
        return value.upper()
    url = str(result.magnet_uri or result.link or "")
    match = re.search(r"(?i)btih:([a-f0-9]{32,40})", url)
    return match.group(1).upper() if match else None


def season_folder_name(season_number: int) -> str:
    return f"Season {season_number:02d}"


def parse_episode_info(value: str) -> dict[str, Any] | None:
    text = str(value or "")
    patterns = [
        r"(?i)(?:^|[^a-z0-9])s(?:eason)?\s*0*(\d{1,2})\s*e(?:p(?:isode)?)?\s*0*(\d{1,3})(?:\s*(?:-|~|–|—|to)\s*e?\s*0*(\d{1,3}))?",
        r"(?i)(?:^|[^a-z0-9])0*(\d{1,2})x0*(\d{1,3})(?:\s*(?:-|~|–|—|to)\s*0*(\d{1,3}))?",
        r"第\s*0*(\d{1,2})\s*季\s*第?\s*0*(\d{1,3})\s*(?:集|话|話)(?:\s*(?:-|~|–|—|至)\s*第?\s*0*(\d{1,3})\s*(?:集|话|話)?)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        season = int(match.group(1))
        first = int(match.group(2))
        last = int(match.group(3)) if match.group(3) else first
        if season <= 0 or first <= 0 or last < first:
            return None
        return {"season_number": season, "episode_numbers": list(range(first, last + 1))}
    return None


def episode_code(season_number: int, episode_numbers: list[int]) -> str:
    if len(episode_numbers) > 1:
        return f"S{season_number:02d}E{episode_numbers[0]:02d}-E{episode_numbers[-1]:02d}"
    return f"S{season_number:02d}E{episode_numbers[0]:02d}"


def series_rename_base(series_folder: str, season_number: int, episode_numbers: list[int], episode_title: str | None = None) -> str:
    base = f"{series_folder} - {episode_code(season_number, episode_numbers)}"
    if episode_title:
        title = re.sub(r"[\\/:*?\"<>|]+", " ", episode_title).strip()
        if title:
            base = f"{base} - {title[:80]}"
    return base


def clean_series_folder_name(value: str) -> str:
    without_id = re.sub(r"\s*\[(?:tmdbid-)?\d+\]\s*$", "", str(value or "").strip()).strip()
    return re.sub(r"\s*\(\d{4}\)\s*$", "", without_id).strip()


def target_with_defaults(target: dict[str, Any]) -> dict[str, Any]:
    folder = str(target.get("folder") or target.get("target_folder") or "").strip()
    category = str(target.get("category") or "movies").strip()
    target["category"] = category
    target["folder"] = folder
    target["target_folder"] = folder
    target.setdefault("media_type", "series" if category == "series" else "movie" if category == "movies" else "other")
    return target


def refresh_target_existing(target: dict[str, Any], settings: Settings) -> dict[str, Any]:
    target = target_with_defaults(dict(target))
    category = target["category"]
    folder = target["folder"]
    try:
        target["existing"] = jellyfin_target_path(category, folder, settings).exists()
    except ValueError:
        target["existing"] = False
    return target


def refresh_targets_existing(targets: list[dict[str, Any]], settings: Settings) -> list[dict[str, Any]]:
    refreshed = [refresh_target_existing(target, settings) for target in targets]
    refreshed.sort(key=lambda item: (item["score"], item["existing"], item["category"] == "series"), reverse=True)
    return refreshed


def series_target(
    series_folder: str,
    season_number: int,
    episode_numbers: list[int],
    score: float,
    reason: str,
    existing: bool,
    episode_title: str | None = None,
    tmdb_id: str | None = None,
) -> dict[str, Any]:
    series_folder = clean_series_folder_name(series_folder)
    season_folder = season_folder_name(season_number)
    folder = f"{series_folder}/{season_folder}"
    preview = series_rename_base(series_folder, season_number, episode_numbers, episode_title)
    return {
        "category": "series",
        "folder": folder,
        "target_folder": folder,
        "media_type": "series",
        "series_folder": series_folder,
        "season_number": season_number,
        "season_folder": season_folder,
        "episode_numbers": episode_numbers,
        "episode_title": episode_title,
        "tmdb_id": tmdb_id,
        "rename_plan": {
            "enabled": True,
            "preview": preview,
            "season_number": season_number,
            "episode_numbers": episode_numbers,
        },
        "score": score,
        "reason": reason,
        "existing": existing,
        "disabled": False,
    }


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


def same_file_size(source: Path, destination: Path) -> bool:
    return source.is_file() and destination.is_file() and source.stat().st_size == destination.stat().st_size


def same_directory_tree(source: Path, destination: Path) -> bool:
    if not source.is_dir() or not destination.is_dir():
        return False
    source_files = sorted(path for path in source.rglob("*") if path.is_file())
    destination_files = sorted(path for path in destination.rglob("*") if path.is_file())
    source_rel = [path.relative_to(source).as_posix() for path in source_files]
    destination_rel = [path.relative_to(destination).as_posix() for path in destination_files]
    if source_rel != destination_rel:
        return False
    return all(source_file.stat().st_size == (destination / rel).stat().st_size for source_file, rel in zip(source_files, source_rel))


def move_or_skip_existing(source: Path, destination: Path) -> dict[str, str]:
    if destination.exists():
        if same_file_size(source, destination) or same_directory_tree(source, destination):
            return {"source": str(source), "destination": str(destination), "skipped": "true"}
        if source.is_file() and destination.is_file():
            replace_file(source, destination)
            return {"source": str(source), "destination": str(destination), "replaced": "true", "skipped": "false"}
        raise FileExistsError(f"Target directory already exists with different content: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return {"source": str(source), "destination": str(destination), "skipped": "false"}


def replace_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(destination)
        return
    except OSError:
        pass
    temp = destination.with_name(f".{destination.name}.bobsearch-tmp")
    if temp.exists():
        temp.unlink()
    try:
        shutil.copy2(source, temp)
        if temp.stat().st_size != source.stat().st_size:
            raise OSError(f"Temporary copy size mismatch for {destination}")
        temp.replace(destination)
        source.unlink()
    except Exception:
        if temp.exists():
            temp.unlink()
        raise


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
    return clean_series_folder_name(folder)


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
    if category not in ALLOWED_JELLYFIN_CATEGORIES or not title:
        return None
    if category == "series":
        season_number = int(target.get("season_number") or 0)
        raw_episodes = target.get("episode_numbers") or []
        if not isinstance(raw_episodes, list):
            raw_episodes = [raw_episodes]
        episode_numbers = [int(item) for item in raw_episodes if str(item).isdigit()]
        if season_number <= 0 or not episode_numbers:
            return None
        series_folder = clean_series_folder_name(str(target.get("series_folder") or title).strip())
        try:
            safe_target_folder(f"{series_folder}/{season_folder_name(season_number)}")
        except ValueError:
            return None
        return series_target(
            series_folder=series_folder,
            season_number=season_number,
            episode_numbers=episode_numbers,
            score=float(target.get("score") or 0.86),
            reason=reason[:120],
            existing=False,
            episode_title=str(target.get("episode_title") or "").strip() or None,
            tmdb_id=tmdb_id if re.fullmatch(r"\d+", tmdb_id) else None,
        )
    if not re.fullmatch(r"\d{4}", year) or not re.fullmatch(r"\d+", tmdb_id):
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
    return target_with_defaults({
        "category": category,
        "folder": folder,
        "score": float(target.get("score") or 0.86),
        "reason": reason[:120],
        "existing": False,
    })


def naming_examples(settings: Settings, limit: int = NAMING_EXAMPLE_LIMIT) -> list[dict[str, str]]:
    examples: list[dict[str, str]] = []
    library = Path(settings.jellyfin_library_path)
    pattern = re.compile(r".+ \(\d{4}\) \[tmdbid-\d+\]$")
    for category in TARGET_CATEGORY_ORDER:
        root = library / category
        if not root.exists():
            continue
        for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
            if not child.is_dir():
                continue
            if category == "series":
                seasons = [season.name for season in child.iterdir() if season.is_dir() and re.fullmatch(r"Season \d{2}", season.name)]
                examples.append({"category": category, "folder": child.name, "season_examples": ", ".join(sorted(seasons)[:3])})
                if len(examples) >= limit:
                    return examples
            elif pattern.fullmatch(child.name):
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


def target_llm_file_context(file_names: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for name in file_names or []:
        value = str(name or "").strip()
        if not value:
            continue
        cleaned.append(value[:500])
        if len(cleaned) >= 40:
            break
    return cleaned


async def llm_generated_target_suggestion(query: str, settings: Settings, file_names: list[str] | None = None) -> dict[str, Any] | None:
    file_context = target_llm_file_context(file_names)
    tmdb_query = " ".join([query, *file_context[:8]])
    tmdb_candidates = await tmdb_web_candidates(tmdb_query)
    prompt = {
        "task": "Identify the media in a qBittorrent download task and generate one Jellyfin folder target.",
        "release_name": query,
        "download_file_names": file_context,
        "cleaned_search_text": release_search_text(query),
        "tmdb_candidates_from_web_search": tmdb_candidates,
        "existing_folder_style_examples": naming_examples(settings),
        "required_folder_format": "movies: Title (YYYY) [tmdbid-NUMBER]; series: Series Title/Season NN",
        "rules": [
            "Use both release_name and download_file_names. File names are decisive when they reveal the actual media.",
            "Find the actual movie/series title; ignore release groups, websites, codecs, quality, audio, subtitles and container text.",
            "If release_name or any download_file_names indicate a specific episode, classify it as series, not movie. Episode markers can be in Chinese, English, numeric season/episode forms, or mixed release notation.",
            "For series, infer season_number and episode_numbers from the most specific evidence in release_name or download_file_names.",
            "Use TMDb as the ID source. Prefer the provided TMDb web-search candidates when they match the release name.",
            "For Chinese releases, prefer the title style already used by Jellyfin/TMDb Chinese metadata if clear; otherwise use the official English title.",
            "For series, TMDb ID is optional because the target folder does not include it. If the task/file evidence clearly identifies a series episode, return a series target even when TMDb candidates are weak or absent.",
            "For series, return category=series, title/series_folder without year or TMDb ID, season_number and episode_numbers. The final Jellyfin path is series_folder/Season NN.",
            "Do not classify as a movie only because TMDb candidates include a movie; reconcile candidates against the file names first.",
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
                "folder": "movie folder only; leave empty for series",
                "series_folder": "series folder name without id, for category=series",
                "season_number": 1,
                "episode_numbers": [1],
                "episode_title": "optional",
                "score": 0.0,
                "reason": "short Chinese explanation",
            }
        },
    }
    try:
        response_json, _ = await chat_completion(
            settings,
            system_content="You return valid compact JSON only. Use TMDb IDs for folder naming.",
            user_content=json.dumps(prompt, ensure_ascii=False),
            temperature=0.05,
            max_tokens=900,
            response_format={"type": "json_object"},
        )
        content = response_json["choices"][0]["message"]["content"]
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
    episode_info = parse_episode_info(query)
    for category in TARGET_CATEGORY_ORDER:
        root = library / category
        if not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            score, reason = target_score(query, child.name)
            if category != "series" and score < MIN_EXISTING_TARGET_SCORE:
                continue
            if category == "series" and score <= 0:
                continue
            if category == "series":
                if not episode_info:
                    suggestions.append(
                        target_with_defaults(
                            {
                                "category": "series",
                                "folder": child.name,
                                "score": min(score, 0.4),
                                "reason": f"{reason}；无法识别季集号",
                                "existing": True,
                                "media_type": "series",
                                "series_folder": child.name,
                                "disabled": True,
                            }
                        )
                    )
                    continue
                suggestions.append(
                    series_target(
                        series_folder=child.name,
                        season_number=episode_info["season_number"],
                        episode_numbers=episode_info["episode_numbers"],
                        score=score,
                        reason=reason,
                        existing=True,
                    )
                )
            elif not episode_info:
                suggestions.append(
                    target_with_defaults(
                        {
                            "category": category,
                            "folder": child.name,
                            "score": score,
                            "reason": reason,
                            "existing": True,
                        }
                    )
                )
    suggestions = refresh_targets_existing(suggestions, settings)
    generated = generated_folder_name(query)
    if include_fallback and not episode_info and not any(item["category"] == "movies" and item["folder"] == generated for item in suggestions):
        suggestions.append(
            target_with_defaults(
                {
                    "category": "movies",
                    "folder": generated,
                    "score": 0.2,
                    "reason": "自动生成的新目录名",
                    "existing": False,
                }
            )
        )
    return suggestions[:limit]


async def jellyfin_target_suggestions_with_llm(
    query: str,
    settings: Settings,
    limit: int = 24,
    file_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    suggestions = jellyfin_target_suggestions(query, settings, limit=limit, include_fallback=False)
    llm_target = await llm_generated_target_suggestion(query, settings, file_names=file_names)
    if llm_target and not any(item["category"] == llm_target["category"] and item["folder"] == llm_target["folder"] for item in suggestions):
        suggestions.append(llm_target)
    return refresh_targets_existing(suggestions, settings)[:limit]


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


def selected_source_files(base: Path, selected_paths: list[str]) -> list[tuple[PurePosixPath, Path]]:
    files: list[tuple[PurePosixPath, Path]] = []
    for rel in compress_selected_paths(selected_paths):
        source = ensure_inside(base.joinpath(*rel.parts), base)
        if not source.exists():
            raise FileNotFoundError(f"Selected path does not exist: {rel}")
        if source.is_dir():
            for child in sorted(source.rglob("*")):
                if child.is_file():
                    child_rel = PurePosixPath(child.relative_to(base).as_posix())
                    files.append((child_rel, child))
        else:
            files.append((rel, source))
    return files


def existing_selected_source_files(base: Path, selected_paths: list[str]) -> list[tuple[PurePosixPath, Path]]:
    files: list[tuple[PurePosixPath, Path]] = []
    for rel in compress_selected_paths(selected_paths):
        source = ensure_inside(base.joinpath(*rel.parts), base)
        if not source.exists():
            continue
        if source.is_dir():
            for child in sorted(source.rglob("*")):
                if child.is_file():
                    child_rel = PurePosixPath(child.relative_to(base).as_posix())
                    files.append((child_rel, child))
        else:
            files.append((rel, source))
    return files


def selected_paths_all_complete(file_items: list[dict[str, Any]], selected_paths: list[str]) -> bool:
    compressed = compress_selected_paths(selected_paths)
    normalized_items = []
    for item in file_items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            rel = safe_relative_path(name)
        except ValueError:
            continue
        normalized_items.append((rel, float(item.get("progress") or 0)))
    for selected in compressed:
        matched = [progress for rel, progress in normalized_items if rel == selected or rel.is_relative_to(selected)]
        if not matched:
            return False
        if any(progress < FILE_COMPLETE_THRESHOLD for progress in matched):
            return False
    return True


def is_video(path: Path) -> bool:
    return path.suffix.casefold() in VIDEO_EXTENSIONS


def is_subtitle(path: Path) -> bool:
    return path.suffix.casefold() in SUBTITLE_EXTENSIONS


def is_media_rel(rel: PurePosixPath) -> bool:
    return rel.suffix.casefold() in VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS


def season_from_target_folder(folder: str) -> tuple[str, int]:
    rel = safe_target_folder(folder)
    if len(rel.parts) < 2:
        raise ValueError("Series target must include series folder and Season NN")
    match = re.fullmatch(r"Season\s+(\d{1,2})", rel.parts[-1], flags=re.I)
    if not match:
        raise ValueError("Series target must end with Season NN")
    return "/".join(rel.parts[:-1]), int(match.group(1))


def leading_episode_number(source_rel: PurePosixPath) -> int | None:
    match = re.match(r"^\s*0*(\d{1,3})(?:\D|$)", source_rel.stem)
    if not match:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def series_rename_plan_overrides(
    files: list[tuple[PurePosixPath, Path]],
    rename_plan: dict[str, Any] | None,
    series_folder: str,
    target_season: int,
) -> dict[str, str]:
    if not isinstance(rename_plan, dict):
        return {}
    file_episode_map = rename_plan.get("file_episode_map")
    if isinstance(file_episode_map, dict):
        overrides: dict[str, str] = {}
        for rel, path in files:
            mapping = file_episode_map.get(rel.as_posix()) or file_episode_map.get(rel.name)
            if not isinstance(mapping, dict):
                continue
            raw_episodes = mapping.get("episode_numbers") or []
            if not isinstance(raw_episodes, list):
                raw_episodes = [raw_episodes]
            episode_numbers = [int(item) for item in raw_episodes if str(item).isdigit()]
            season_number = int(mapping.get("season_number") or target_season)
            if not episode_numbers or season_number != target_season:
                continue
            overrides[rel.as_posix()] = f"{series_rename_base(Path(series_folder).name, target_season, episode_numbers)}{path.suffix}"
        if overrides:
            return overrides
    raw_episodes = rename_plan.get("episode_numbers") or []
    if not isinstance(raw_episodes, list):
        raw_episodes = [raw_episodes]
    episode_numbers = [int(item) for item in raw_episodes if str(item).isdigit()]
    plan_season = int(rename_plan.get("season_number") or 0)
    if not episode_numbers or plan_season != target_season:
        return {}
    media_files = [(rel, path) for rel, path in files if is_video(path) or is_subtitle(path)]
    if not media_files:
        return {}
    overrides: dict[str, str] = {}
    if len(media_files) == 1:
        rel, path = media_files[0]
        overrides[rel.as_posix()] = f"{series_rename_base(Path(series_folder).name, target_season, episode_numbers)}{path.suffix}"
        return overrides
    video_files = [(rel, path) for rel, path in media_files if is_video(path)]
    if len(video_files) != len(episode_numbers):
        return {}
    ordered: list[tuple[int, PurePosixPath, Path]] = []
    for rel, path in video_files:
        episode_number = leading_episode_number(rel)
        if episode_number is None:
            return {}
        ordered.append((episode_number, rel, path))
    ordered.sort(key=lambda item: item[0])
    if [item[0] for item in ordered] != episode_numbers:
        return {}
    for episode_number, rel, path in ordered:
        overrides[rel.as_posix()] = f"{series_rename_base(Path(series_folder).name, target_season, [episode_number])}{path.suffix}"
    return overrides


def series_destination_name(
    source_rel: PurePosixPath,
    source: Path,
    torrent: QbitTorrent,
    series_folder: str,
    target_season: int,
    rename_overrides: dict[str, str] | None = None,
) -> str | None:
    if not (is_video(source) or is_subtitle(source)):
        return None
    override = (rename_overrides or {}).get(source_rel.as_posix())
    if override:
        return override
    info = parse_episode_info(" ".join([str(source_rel), source.name, torrent.name]))
    if not info:
        raise ValueError(f"无法识别季集号：{source_rel}")
    if info["season_number"] != target_season:
        raise ValueError(f"文件季号 S{info['season_number']:02d} 与目标 Season {target_season:02d} 不一致：{source_rel}")
    return f"{series_rename_base(Path(series_folder).name, target_season, info['episode_numbers'])}{source.suffix}"


def series_destination_name_from_rel(
    source_rel: PurePosixPath,
    torrent: QbitTorrent,
    series_folder: str,
    target_season: int,
    rename_overrides: dict[str, str] | None = None,
) -> str | None:
    if not is_media_rel(source_rel):
        return None
    override = (rename_overrides or {}).get(source_rel.as_posix())
    if override:
        return override
    info = parse_episode_info(" ".join([str(source_rel), source_rel.name, torrent.name]))
    if not info:
        raise ValueError(f"无法识别季集号：{source_rel}")
    if info["season_number"] != target_season:
        raise ValueError(f"文件季号 S{info['season_number']:02d} 与目标 Season {target_season:02d} 不一致：{source_rel}")
    return f"{series_rename_base(Path(series_folder).name, target_season, info['episode_numbers'])}{source_rel.suffix}"


def skipped_existing_missing_source(source: Path, destination: Path) -> dict[str, str]:
    return {
        "source": str(source),
        "destination": str(destination),
        "skipped": "true",
        "missing_source": "true",
    }


def ensure_unique_series_destinations(
    files: list[tuple[PurePosixPath, Path]],
    torrent: QbitTorrent,
    series_folder: str,
    target_season: int,
    rename_overrides: dict[str, str],
) -> None:
    destinations: dict[str, PurePosixPath] = {}
    for rel, source in files:
        destination_name = series_destination_name(rel, source, torrent, series_folder, target_season, rename_overrides)
        if not destination_name:
            continue
        previous = destinations.get(destination_name)
        if previous and previous != rel:
            raise ValueError(f"电视剧重命名冲突：{previous} 和 {rel} 都会命名为 {destination_name}")
        destinations[destination_name] = rel


async def llm_series_file_episode_map(
    settings: Settings,
    torrent: QbitTorrent,
    files: list[tuple[PurePosixPath, Path]],
    series_folder: str,
    target_season: int,
    rename_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    media_files = [(rel, path) for rel, path in files if is_video(path) or is_subtitle(path)]
    if len(media_files) <= 1:
        return {}
    requested_episodes = []
    if isinstance(rename_plan, dict):
        raw_episodes = rename_plan.get("episode_numbers") or []
        if not isinstance(raw_episodes, list):
            raw_episodes = [raw_episodes]
        requested_episodes = [int(item) for item in raw_episodes if str(item).isdigit()]
    prompt = {
        "task": "Assign each selected TV media file to the correct episode numbers for Jellyfin renaming.",
        "series_title": Path(series_folder).name,
        "torrent_name": torrent.name,
        "target_season": target_season,
        "requested_episode_numbers": requested_episodes,
        "selected_media_files": [
            {
                "path": rel.as_posix(),
                "name": path.name,
                "is_video": is_video(path),
                "is_subtitle": is_subtitle(path),
                "size": path.stat().st_size,
            }
            for rel, path in media_files
        ],
        "rules": [
            "Return strict JSON only.",
            "Map each media file to the most likely episode_numbers within the target season.",
            "For separate single-episode files like 01.mkv and 02.mkv, assign [1] and [2] separately, not [1,2] to both.",
            "Only return episode numbers that are supported by the file naming evidence and requested_episode_numbers context.",
            "Keep subtitle files aligned with their matching video files when obvious.",
            "Do not assign the same single-episode destination to two different video files.",
        ],
        "schema": {
            "file_episode_map": {
                "relative/path.mkv": {
                    "season_number": 1,
                    "episode_numbers": [1],
                }
            }
        },
    }
    try:
        response_json, _ = await chat_completion(
            settings,
            system_content="You return valid compact JSON only.",
            user_content=json.dumps(prompt, ensure_ascii=False),
            temperature=0.05,
            max_tokens=900,
            response_format={"type": "json_object"},
        )
        content = response_json["choices"][0]["message"]["content"]
        data = json.loads(content)
        mapping = data.get("file_episode_map") if isinstance(data, dict) else None
        return mapping if isinstance(mapping, dict) else {}
    except Exception:
        return {}


def move_selected_files(
    selected_paths: list[str],
    torrent: QbitTorrent,
    target_category: str,
    target_folder: str,
    settings: Settings,
    rename_plan: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    base = qbit_path_to_local(torrent.save_path or settings.qbit_downloads_path, settings)
    target_root = jellyfin_target_path(target_category, target_folder, settings)
    target_root.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, str]] = []
    if target_category == "series":
        series_folder, target_season = season_from_target_folder(target_folder)
        all_existing_source_files = existing_selected_source_files(base, selected_paths)
        rename_overrides = series_rename_plan_overrides(all_existing_source_files, rename_plan, series_folder, target_season)
        ensure_unique_series_destinations(all_existing_source_files, torrent, series_folder, target_season, rename_overrides)
        for selected_rel in compress_selected_paths(selected_paths):
            selected_source = ensure_inside(base.joinpath(*selected_rel.parts), base)
            source_files = selected_source_files(base, [selected_rel.as_posix()]) if selected_source.exists() else []
            if not selected_source.exists():
                if selected_rel.suffix:
                    destination_name = series_destination_name_from_rel(selected_rel, torrent, series_folder, target_season, rename_overrides)
                    if destination_name and (target_root / destination_name).exists():
                        moved.append(skipped_existing_missing_source(selected_source, target_root / destination_name))
                        continue
                if target_root.exists() and any(child.is_file() for child in target_root.iterdir()):
                    moved.append(skipped_existing_missing_source(selected_source, target_root))
                    continue
                raise FileNotFoundError(f"Selected path does not exist: {selected_rel}")
            for rel, source in source_files:
                destination_name = series_destination_name(rel, source, torrent, series_folder, target_season, rename_overrides)
                if not destination_name:
                    continue
                moved.append(move_or_skip_existing(source, target_root / destination_name))
        if not moved:
            raise ValueError("没有可移动的电视剧视频或字幕文件")
        return moved
    for rel in compress_selected_paths(selected_paths):
        source = ensure_inside(base.joinpath(*rel.parts), base)
        if not source.exists():
            destination = target_root / rel.name
            if destination.exists():
                moved.append(skipped_existing_missing_source(source, destination))
                continue
            raise FileNotFoundError(f"Selected path does not exist: {rel}")
        moved.append(move_or_skip_existing(source, target_root / rel.name))
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
        info_hash = result_info_hash(result)
        if info_hash and await self.torrent_exists(info_hash):
            raise TorrentAlreadyExistsError("Download task already exists")
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
            if info_hash and await self.torrent_exists(info_hash):
                raise TorrentAlreadyExistsError("Download task already exists")
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

    async def torrent_exists(self, torrent_hash: str) -> bool:
        try:
            await self.torrent(torrent_hash)
            return True
        except ValueError:
            return False

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
        for _ in range(10):
            if not await self.torrent_exists(torrent_hash):
                return
            await asyncio.sleep(0.3)
        raise RuntimeError("qBittorrent delete returned success but task still exists")

    async def move_selected(
        self,
        torrent_hash: str,
        selected_paths: list[str],
        target_category: str,
        target_folder: str,
        rename_plan: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        torrent = await self.torrent(torrent_hash)
        if not selected_paths:
            raise ValueError("No files or folders selected")
        file_items = await self.files(torrent_hash)
        if not selected_paths_all_complete(file_items, selected_paths):
            raise ValueError("勾选项尚未全部下载完成")
        effective_rename_plan = dict(rename_plan or {}) if isinstance(rename_plan, dict) else rename_plan
        if target_category == "series":
            base = qbit_path_to_local(torrent.save_path or self.settings.qbit_downloads_path, self.settings)
            series_folder, target_season = season_from_target_folder(target_folder)
            source_files = existing_selected_source_files(base, selected_paths)
            file_episode_map = await llm_series_file_episode_map(
                self.settings,
                torrent,
                source_files,
                series_folder,
                target_season,
                effective_rename_plan if isinstance(effective_rename_plan, dict) else None,
            )
            if file_episode_map:
                if not isinstance(effective_rename_plan, dict):
                    effective_rename_plan = {}
                effective_rename_plan["file_episode_map"] = file_episode_map
        await self.stop_torrent(torrent_hash)
        moved = move_selected_files(selected_paths, torrent, target_category, target_folder, self.settings, rename_plan=effective_rename_plan)
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
