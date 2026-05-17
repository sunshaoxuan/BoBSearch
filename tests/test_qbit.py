from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models import QbitTorrent, SearchResult
from app.qbit import (
    QbitClient,
    TorrentAlreadyExistsError,
    build_file_tree,
    compress_selected_paths,
    folder_name_from_llm_target,
    jellyfin_target_path,
    jellyfin_target_suggestions,
    jellyfin_target_suggestions_with_llm,
    llm_series_file_episode_map,
    move_selected_files,
    parse_tmdb_page,
    parse_episode_info,
    qbit_path_to_local,
    release_search_text,
    refresh_targets_existing,
    result_info_hash,
    selected_paths_all_complete,
    series_rename_base,
    safe_relative_path,
    target_score,
    unique_destination,
)


def settings(tmp_path):
    return Settings(
        web_password="web",
        session_secret="session",
        jackett_api_key="jackett",
        qbit_password="qbit",
        llm_base_url="http://llm.test/v1",
        llm_api_key="llm",
        qbit_save_path="/app/qBittorrent/downloads/movies-staging",
        qbit_downloads_path="/app/qBittorrent/downloads",
        qbit_local_downloads_path=str(tmp_path / "config-downloads"),
        qbit_extra_downloads_path="/downloads",
        qbit_extra_local_downloads_path=str(tmp_path / "downloads"),
        jellyfin_library_path=str(tmp_path / "jellyfin"),
    )


def torrent() -> QbitTorrent:
    return QbitTorrent(
        hash="abc",
        name="Sample",
        progress=1,
        save_path="/app/qBittorrent/downloads/movies-staging",
        category="movies-staging",
        is_complete=True,
    )


def mock_response(status_code: int = 200, text: str = "") -> httpx.Response:
    return httpx.Response(status_code, text=text, request=httpx.Request("POST", "http://qbit.test/api"))


def test_safe_relative_path_rejects_escape():
    with pytest.raises(ValueError):
        safe_relative_path("../escape.mkv")
    with pytest.raises(ValueError):
        safe_relative_path("/absolute.mkv")


def test_qbit_and_jellyfin_paths_stay_inside_roots(tmp_path):
    cfg = settings(tmp_path)
    assert qbit_path_to_local("/app/qBittorrent/downloads/movies-staging/Movie/file.mkv", cfg).name == "file.mkv"
    assert "config-downloads" in str(qbit_path_to_local("/app/qBittorrent/downloads/other/file.mkv", cfg))
    assert "downloads" in str(qbit_path_to_local("/downloads/other/file.mkv", cfg))
    with pytest.raises(ValueError):
        qbit_path_to_local("/app/qBittorrent/other/file.mkv", cfg)
    assert jellyfin_target_path("movies", "Movie (2026)", cfg).name == "Movie (2026)"
    assert jellyfin_target_path("series", "权力的游戏/Season 01", cfg).name == "Season 01"
    with pytest.raises(ValueError):
        jellyfin_target_path("movies", "../bad", cfg)
    with pytest.raises(ValueError):
        jellyfin_target_path("series", "权力的游戏/../Season 01", cfg)


def test_file_tree_groups_directories():
    tree = build_file_tree(
        [
            {"name": "Pack/Movie.mkv", "size": 10, "progress": 1, "priority": 1},
            {"name": "Pack/Subs/Movie.srt", "size": 2, "progress": 1, "priority": 1},
        ]
    )
    assert tree[0].type == "directory"
    assert tree[0].path == "Pack"
    assert tree[0].size == 12
    assert {child.path for child in tree[0].children} == {"Pack/Movie.mkv", "Pack/Subs"}


def test_compress_selected_paths_removes_children_when_parent_selected():
    selected = compress_selected_paths(["Pack/Movie.mkv", "Pack", "Other/file.mkv"])
    assert [str(path) for path in selected] == ["Pack", "Other/file.mkv"]


def test_unique_destination_adds_suffix(tmp_path):
    target = tmp_path / "Movie.mkv"
    target.write_text("old")
    assert unique_destination(target).name == "Movie.1.mkv"


def test_move_selected_file_flattens_above_folders(tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "Disc" / "Movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("movie")
    moved = move_selected_files(["Pack/Disc/Movie.mkv"], torrent(), "movies", "Movie (2026)", cfg)
    assert (tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv").read_text() == "movie"
    assert moved[0]["destination"].endswith("Movie.mkv")


def test_move_selected_file_skips_existing_same_size_without_suffix(tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "Disc" / "Movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("movie")
    target = tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("movie")

    moved = move_selected_files(["Pack/Disc/Movie.mkv"], torrent(), "movies", "Movie (2026)", cfg)

    assert moved[0]["skipped"] == "true"
    assert not (target.parent / "Movie.1.mkv").exists()
    assert source.exists()


def test_move_selected_missing_source_skips_when_destination_exists(tmp_path):
    cfg = settings(tmp_path)
    target = tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("movie")

    moved = move_selected_files(["Pack/Disc/Movie.mkv"], torrent(), "movies", "Movie (2026)", cfg)

    assert moved[0]["skipped"] == "true"
    assert moved[0]["missing_source"] == "true"


def test_move_selected_file_replaces_existing_different_size(tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "Disc" / "Movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("movie")
    target = tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("different")

    moved = move_selected_files(["Pack/Disc/Movie.mkv"], torrent(), "movies", "Movie (2026)", cfg)

    assert moved[0]["replaced"] == "true"
    assert target.read_text() == "movie"
    assert not source.exists()
    assert not (target.parent / "Movie.1.mkv").exists()


def test_move_selected_file_replace_cleans_temp_on_copy_failure(monkeypatch, tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "Disc" / "Movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("movie")
    target = tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("different")

    def fake_copy(_source, destination):
        Path(destination).write_text("partial")
        raise OSError("copy failed")

    monkeypatch.setattr("app.qbit.shutil.copy2", fake_copy)
    monkeypatch.setattr("app.qbit.Path.replace", lambda self, target: (_ for _ in ()).throw(OSError("cross device")))

    with pytest.raises(OSError, match="copy failed"):
        move_selected_files(["Pack/Disc/Movie.mkv"], torrent(), "movies", "Movie (2026)", cfg)

    assert target.read_text() == "different"
    assert source.read_text() == "movie"
    assert not (target.parent / ".Movie.mkv.bobsearch-tmp").exists()


def test_move_selected_folder_keeps_selected_folder_downward(tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "Disc" / "Movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("movie")
    move_selected_files(["Pack/Disc"], torrent(), "movies", "Movie (2026)", cfg)
    assert (tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Disc" / "Movie.mkv").read_text() == "movie"


def test_parse_episode_info_common_formats():
    assert parse_episode_info("Show.S01E02.1080p") == {"season_number": 1, "episode_numbers": [2]}
    assert parse_episode_info("Show S1E2") == {"season_number": 1, "episode_numbers": [2]}
    assert parse_episode_info("Show 第1季第2集") == {"season_number": 1, "episode_numbers": [2]}
    assert parse_episode_info("Show 01x02") == {"season_number": 1, "episode_numbers": [2]}
    assert parse_episode_info("Show.S01E01-E02") == {"season_number": 1, "episode_numbers": [1, 2]}


def test_series_rename_base_standard_names():
    assert series_rename_base("权力的游戏", 1, [2]) == "权力的游戏 - S01E02"
    assert series_rename_base("权力的游戏", 1, [1, 2]) == "权力的游戏 - S01E01-E02"


def test_series_target_suggestion_matches_existing_series_season(tmp_path):
    cfg = settings(tmp_path)
    (tmp_path / "jellyfin" / "series" / "权力的游戏" / "Season 01").mkdir(parents=True)
    suggestions = jellyfin_target_suggestions("权力的游戏.Game.of.Thrones.S01E02.1080p.mkv", cfg)
    assert suggestions[0]["category"] == "series"
    assert suggestions[0]["folder"] == "权力的游戏/Season 01"
    assert suggestions[0]["season_number"] == 1
    assert suggestions[0]["episode_numbers"] == [2]
    assert suggestions[0]["rename_plan"]["preview"] == "权力的游戏 - S01E02"


def test_series_target_without_episode_is_disabled(tmp_path):
    cfg = settings(tmp_path)
    (tmp_path / "jellyfin" / "series" / "权力的游戏").mkdir(parents=True)
    suggestions = jellyfin_target_suggestions("权力的游戏.1080p.mkv", cfg)
    assert suggestions[0]["category"] == "series"
    assert suggestions[0]["disabled"] is True


def test_move_series_video_renames_into_season_folder(tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "权力的游戏.S01E02.1080p.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("episode")
    move_selected_files(["Pack/权力的游戏.S01E02.1080p.mkv"], torrent(), "series", "权力的游戏/Season 01", cfg)
    assert (tmp_path / "jellyfin" / "series" / "权力的游戏" / "Season 01" / "权力的游戏 - S01E02.mkv").read_text() == "episode"


def test_move_series_video_skips_existing_same_size_without_suffix(tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "权力的游戏.S01E02.1080p.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("episode")
    target = tmp_path / "jellyfin" / "series" / "权力的游戏" / "Season 01" / "权力的游戏 - S01E02.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("episode")

    moved = move_selected_files(["Pack/权力的游戏.S01E02.1080p.mkv"], torrent(), "series", "权力的游戏/Season 01", cfg)

    assert moved[0]["skipped"] == "true"
    assert not (target.parent / "权力的游戏 - S01E02.1.mkv").exists()


def test_move_series_video_replaces_existing_different_size(tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "权力的游戏.S01E02.1080p.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("new episode")
    target = tmp_path / "jellyfin" / "series" / "权力的游戏" / "Season 01" / "权力的游戏 - S01E02.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("old")

    moved = move_selected_files(["Pack/权力的游戏.S01E02.1080p.mkv"], torrent(), "series", "权力的游戏/Season 01", cfg)

    assert moved[0]["replaced"] == "true"
    assert target.read_text() == "new episode"
    assert not (target.parent / "权力的游戏 - S01E02.1.mkv").exists()


def test_move_series_folder_skips_ads_and_renames_media(tmp_path):
    cfg = settings(tmp_path)
    folder = tmp_path / "config-downloads" / "movies-staging" / "Pack"
    folder.mkdir(parents=True)
    (folder / "权力的游戏.S01E03.1080p.mkv").write_text("episode")
    (folder / "广告.png").write_text("ad")
    move_selected_files(["Pack"], torrent(), "series", "权力的游戏/Season 01", cfg)
    assert (tmp_path / "jellyfin" / "series" / "权力的游戏" / "Season 01" / "权力的游戏 - S01E03.mkv").exists()
    assert not (tmp_path / "jellyfin" / "series" / "权力的游戏" / "Season 01" / "广告.png").exists()


def test_move_series_folder_uses_rename_plan_for_plain_episode_numbers(tmp_path):
    cfg = settings(tmp_path)
    folder = tmp_path / "config-downloads" / "movies-staging" / "黑夜告白01-02.2160p"
    folder.mkdir(parents=True)
    (folder / "01.2160p.mkv").write_text("ep1")
    (folder / "02.2160p.mkv").write_text("ep2")

    move_selected_files(
        ["黑夜告白01-02.2160p"],
        torrent(),
        "series",
        "黑夜告白/Season 01",
        cfg,
        rename_plan={"season_number": 1, "episode_numbers": [1, 2]},
    )

    season = tmp_path / "jellyfin" / "series" / "黑夜告白" / "Season 01"
    assert (season / "黑夜告白 - S01E01.mkv").read_text() == "ep1"
    assert (season / "黑夜告白 - S01E02.mkv").read_text() == "ep2"


def test_move_series_individual_files_use_batch_rename_plan(tmp_path):
    cfg = settings(tmp_path)
    folder = tmp_path / "config-downloads" / "movies-staging" / "黑夜告白01-02.2160p"
    folder.mkdir(parents=True)
    (folder / "01.2160p.mkv").write_text("ep1")
    (folder / "02.2160p.mkv").write_text("ep2")

    move_selected_files(
        ["黑夜告白01-02.2160p/01.2160p.mkv", "黑夜告白01-02.2160p/02.2160p.mkv"],
        torrent(),
        "series",
        "黑夜告白/Season 01",
        cfg,
        rename_plan={"season_number": 1, "episode_numbers": [1, 2]},
    )

    season = tmp_path / "jellyfin" / "series" / "黑夜告白" / "Season 01"
    assert (season / "黑夜告白 - S01E01.mkv").read_text() == "ep1"
    assert (season / "黑夜告白 - S01E02.mkv").read_text() == "ep2"


def test_move_series_file_episode_map_overrides_names(tmp_path):
    cfg = settings(tmp_path)
    folder = tmp_path / "config-downloads" / "movies-staging" / "黑夜告白01-02.2160p"
    folder.mkdir(parents=True)
    (folder / "01.2160p.mkv").write_text("ep1")
    (folder / "02.2160p.mkv").write_text("ep2")

    move_selected_files(
        ["黑夜告白01-02.2160p/01.2160p.mkv", "黑夜告白01-02.2160p/02.2160p.mkv"],
        torrent(),
        "series",
        "黑夜告白/Season 01",
        cfg,
        rename_plan={
            "season_number": 1,
            "episode_numbers": [1, 2],
            "file_episode_map": {
                "黑夜告白01-02.2160p/01.2160p.mkv": {"season_number": 1, "episode_numbers": [1]},
                "黑夜告白01-02.2160p/02.2160p.mkv": {"season_number": 1, "episode_numbers": [2]},
            },
        },
    )

    season = tmp_path / "jellyfin" / "series" / "黑夜告白" / "Season 01"
    assert (season / "黑夜告白 - S01E01.mkv").read_text() == "ep1"
    assert (season / "黑夜告白 - S01E02.mkv").read_text() == "ep2"


def test_llm_series_file_episode_map_parses_model_mapping(monkeypatch, tmp_path):
    async def fake_chat_completion(*_args, **_kwargs):
        return (
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"file_episode_map":{"黑夜告白01-02.2160p/01.2160p.mkv":{"season_number":1,"episode_numbers":[1]},"黑夜告白01-02.2160p/02.2160p.mkv":{"season_number":1,"episode_numbers":[2]}}}'
                        }
                    }
                ]
            },
            "primary",
        )

    monkeypatch.setattr("app.qbit.chat_completion", fake_chat_completion)
    cfg = settings(tmp_path)
    folder = tmp_path / "config-downloads" / "movies-staging" / "黑夜告白01-02.2160p"
    folder.mkdir(parents=True)
    first = folder / "01.2160p.mkv"
    second = folder / "02.2160p.mkv"
    first.write_text("ep1")
    second.write_text("ep2")

    mapping = asyncio.run(
        llm_series_file_episode_map(
            cfg,
            torrent(),
            [
                (Path("黑夜告白01-02.2160p/01.2160p.mkv"), first),
                (Path("黑夜告白01-02.2160p/02.2160p.mkv"), second),
            ],
            "黑夜告白",
            1,
            {"season_number": 1, "episode_numbers": [1, 2]},
        )
    )

    assert mapping["黑夜告白01-02.2160p/01.2160p.mkv"]["episode_numbers"] == [1]
    assert mapping["黑夜告白01-02.2160p/02.2160p.mkv"]["episode_numbers"] == [2]


def test_move_series_detects_same_operation_destination_collision(tmp_path):
    cfg = settings(tmp_path)
    folder = tmp_path / "config-downloads" / "movies-staging" / "Pack"
    folder.mkdir(parents=True)
    (folder / "01.first.mkv").write_text("ep1")
    (folder / "01.second.mkv").write_text("ep2")

    with pytest.raises(ValueError, match="电视剧重命名冲突"):
        move_selected_files(
            ["Pack/01.first.mkv", "Pack/01.second.mkv"],
            torrent(),
            "series",
            "黑夜告白/Season 01",
            cfg,
            rename_plan={"season_number": 1, "episode_numbers": [1, 1]},
        )


def test_move_series_without_episode_fails_before_delete(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        source = tmp_path / "config-downloads" / "movies-staging" / "Movie.mkv"
        source.parent.mkdir(parents=True)
        source.write_text("movie")
        client = QbitClient(cfg)
        deleted = False

        async def fake_torrent(_hash):
            return torrent()

        async def fake_files(_hash):
            return [{"name": "Movie.mkv", "progress": 1.0}]

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal deleted
            deleted = True

        monkeypatch.setattr(client, "torrent", fake_torrent)
        monkeypatch.setattr(client, "files", fake_files)
        monkeypatch.setattr(client, "stop_torrent", fake_stop)
        monkeypatch.setattr(client, "delete_torrent", fake_delete)

        with pytest.raises(ValueError, match="无法识别季集号"):
            await client.move_selected("abc", ["Movie.mkv"], "series", "权力的游戏/Season 01")
        assert deleted is False
        await client.close()

    asyncio.run(run())


def test_move_failure_does_not_delete(monkeypatch, tmp_path):
    async def run():
        client = QbitClient(settings(tmp_path))
        deleted = False

        async def fake_torrent(_hash):
            return torrent()

        async def fake_files(_hash):
            return [{"name": "missing.mkv", "progress": 1.0}]

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal deleted
            deleted = True

        monkeypatch.setattr(client, "torrent", fake_torrent)
        monkeypatch.setattr(client, "files", fake_files)
        monkeypatch.setattr(client, "stop_torrent", fake_stop)
        monkeypatch.setattr(client, "delete_torrent", fake_delete)

        with pytest.raises(FileNotFoundError):
            await client.move_selected("abc", ["missing.mkv"], "movies", "Movie (2026)")
        assert deleted is False
        await client.close()

    asyncio.run(run())


def test_move_success_deletes_with_files(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        source = tmp_path / "config-downloads" / "movies-staging" / "Movie.mkv"
        source.parent.mkdir(parents=True)
        source.write_text("movie")
        client = QbitClient(cfg)
        delete_args = None

        async def fake_torrent(_hash):
            return torrent()

        async def fake_files(_hash):
            return [{"name": "Movie.mkv", "progress": 1.0}]

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
        monkeypatch.setattr(client, "files", fake_files)
        monkeypatch.setattr(client, "stop_torrent", fake_stop)
        monkeypatch.setattr(client, "delete_torrent", fake_delete)

        await client.move_selected("abc", ["Movie.mkv"], "movies", "Movie (2026)")
        assert delete_args == ("abc", True)
        await client.close()

    asyncio.run(run())


def test_move_missing_source_but_existing_destination_deletes_with_files(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        target = tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv"
        target.parent.mkdir(parents=True)
        target.write_text("movie")
        client = QbitClient(cfg)
        delete_args = None

        async def fake_torrent(_hash):
            return torrent()

        async def fake_files(_hash):
            return [{"name": "Pack/Disc/Movie.mkv", "progress": 1.0}]

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
        monkeypatch.setattr(client, "files", fake_files)
        monkeypatch.setattr(client, "stop_torrent", fake_stop)
        monkeypatch.setattr(client, "delete_torrent", fake_delete)

        moved = await client.move_selected("abc", ["Pack/Disc/Movie.mkv"], "movies", "Movie (2026)")
        assert moved[0]["missing_source"] == "true"
        assert delete_args == ("abc", True)
        await client.close()

    asyncio.run(run())


def test_start_torrent_uses_start_endpoint(monkeypatch, tmp_path):
    async def run():
        client = QbitClient(settings(tmp_path))
        calls = []

        async def fake_login():
            client.logged_in = True

        async def fake_post(endpoint, data):
            calls.append((endpoint, data))
            return mock_response()

        monkeypatch.setattr(client, "login", fake_login)
        monkeypatch.setattr(client.client, "post", fake_post)

        await client.start_torrent("abc")
        assert calls == [("/api/v2/torrents/start", {"hashes": "abc"})]
        await client.close()

    asyncio.run(run())


def test_start_torrent_falls_back_to_resume(monkeypatch, tmp_path):
    async def run():
        client = QbitClient(settings(tmp_path))
        calls = []

        async def fake_login():
            client.logged_in = True

        async def fake_post(endpoint, data):
            calls.append(endpoint)
            return mock_response(404 if endpoint.endswith("/start") else 200)

        monkeypatch.setattr(client, "login", fake_login)
        monkeypatch.setattr(client.client, "post", fake_post)

        await client.start_torrent("abc")
        assert calls == ["/api/v2/torrents/start", "/api/v2/torrents/resume"]
        await client.close()

    asyncio.run(run())


def test_delete_torrent_can_delete_files(monkeypatch, tmp_path):
    async def run():
        client = QbitClient(settings(tmp_path))
        calls = []

        async def fake_login():
            client.logged_in = True

        async def fake_post(endpoint, data):
            calls.append((endpoint, data))
            return mock_response()

        async def fake_exists(_hash):
            return False

        monkeypatch.setattr(client, "login", fake_login)
        monkeypatch.setattr(client.client, "post", fake_post)
        monkeypatch.setattr(client, "torrent_exists", fake_exists)

        await client.delete_torrent("abc", delete_files=True)
        assert calls == [("/api/v2/torrents/delete", {"hashes": "abc", "deleteFiles": "true"})]
        await client.close()

    asyncio.run(run())


def test_delete_torrent_raises_when_task_remains(monkeypatch, tmp_path):
    async def run():
        client = QbitClient(settings(tmp_path))

        async def fake_login():
            client.logged_in = True

        async def fake_post(endpoint, data):
            return mock_response()

        async def fake_exists(_hash):
            return True

        async def fake_sleep(_seconds):
            return None

        monkeypatch.setattr(client, "login", fake_login)
        monkeypatch.setattr(client.client, "post", fake_post)
        monkeypatch.setattr(client, "torrent_exists", fake_exists)
        monkeypatch.setattr("app.qbit.asyncio.sleep", fake_sleep)

        with pytest.raises(RuntimeError, match="task still exists"):
            await client.delete_torrent("abc", delete_files=True)
        await client.close()

    asyncio.run(run())


def test_add_result_is_idempotent_when_hash_already_exists(monkeypatch, tmp_path):
    async def run():
        client = QbitClient(settings(tmp_path))
        calls = []

        async def fake_ensure_category():
            return None

        async def fake_exists(info_hash):
            calls.append(info_hash)
            return True

        monkeypatch.setattr(client, "ensure_category", fake_ensure_category)
        monkeypatch.setattr(client, "torrent_exists", fake_exists)

        with pytest.raises(TorrentAlreadyExistsError, match="already exists"):
            await client.add_result(SearchResult(token="t", title="Movie", info_hash="abc123", magnet_uri="magnet:?xt=urn:btih:abc123"))
        assert calls == ["ABC123"]
        await client.close()

    asyncio.run(run())


def test_add_result_treats_qbit_fails_as_duplicate_after_recheck(monkeypatch, tmp_path):
    async def run():
        client = QbitClient(settings(tmp_path))
        exists_calls = []
        post_calls = []

        async def fake_ensure_category():
            return None

        async def fake_exists(info_hash):
            exists_calls.append(info_hash)
            return len(exists_calls) > 1

        async def fake_post(endpoint, data):
            post_calls.append((endpoint, data["urls"]))
            return mock_response(text="Fails.")

        monkeypatch.setattr(client, "ensure_category", fake_ensure_category)
        monkeypatch.setattr(client, "torrent_exists", fake_exists)
        monkeypatch.setattr(client.client, "post", fake_post)

        with pytest.raises(TorrentAlreadyExistsError, match="already exists"):
            await client.add_result(SearchResult(token="t", title="Movie", info_hash="abc123", magnet_uri="magnet:?xt=urn:btih:abc123"))
        assert exists_calls == ["ABC123", "ABC123"]
        assert post_calls == [("/api/v2/torrents/add", "magnet:?xt=urn:btih:abc123")]
        await client.close()

    asyncio.run(run())


def test_result_info_hash_falls_back_to_magnet():
    result = SearchResult(token="t", title="Movie", magnet_uri="magnet:?xt=urn:btih:abcdef1234567890abcdef1234567890abcdef12")
    assert result_info_hash(result) == "ABCDEF1234567890ABCDEF1234567890ABCDEF12"


def test_move_success_allows_non_staging_category(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        source = tmp_path / "config-downloads" / "other" / "Movie.mkv"
        source.parent.mkdir(parents=True)
        source.write_text("movie")
        client = QbitClient(cfg)
        delete_args = None

        async def fake_torrent(_hash):
            return QbitTorrent(
                hash="def",
                name="Other category",
                progress=1,
                save_path="/app/qBittorrent/downloads/other",
                category="other",
                is_complete=True,
            )

        async def fake_files(_hash):
            return [{"name": "Movie.mkv", "progress": 1.0}]

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
        monkeypatch.setattr(client, "files", fake_files)
        monkeypatch.setattr(client, "stop_torrent", fake_stop)
        monkeypatch.setattr(client, "delete_torrent", fake_delete)

        await client.move_selected("def", ["Movie.mkv"], "movies", "Movie (2026)")
        assert delete_args == ("def", True)
        await client.close()

    asyncio.run(run())


def test_move_success_supports_downloads_mount_path(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        source = tmp_path / "downloads" / "other" / "Movie.mkv"
        source.parent.mkdir(parents=True)
        source.write_text("movie")
        client = QbitClient(cfg)
        delete_args = None

        async def fake_torrent(_hash):
            return QbitTorrent(
                hash="ghi",
                name="Downloads category",
                progress=1,
                save_path="/downloads/other",
                category="other",
                is_complete=True,
            )

        async def fake_files(_hash):
            return [{"name": "Movie.mkv", "progress": 1.0}]

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
        monkeypatch.setattr(client, "files", fake_files)
        monkeypatch.setattr(client, "stop_torrent", fake_stop)
        monkeypatch.setattr(client, "delete_torrent", fake_delete)

        await client.move_selected("ghi", ["Movie.mkv"], "movies", "Movie (2026)")
        assert delete_args == ("ghi", True)
        assert (tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv").exists()
        await client.close()

    asyncio.run(run())


def test_selected_paths_all_complete_accepts_complete_selected_file_only():
    file_items = [
        {"name": "done.mp4", "progress": 1.0},
        {"name": "other.doc", "progress": 0.0},
    ]
    assert selected_paths_all_complete(file_items, ["done.mp4"]) is True


def test_selected_paths_all_complete_accepts_display_rounded_complete_file():
    file_items = [
        {"name": "done.mp4", "progress": 0.999},
        {"name": "other.doc", "progress": 0.0},
    ]
    assert selected_paths_all_complete(file_items, ["done.mp4"]) is True


def test_selected_paths_all_complete_rejects_incomplete_selected_directory():
    file_items = [
        {"name": "Pack/done.mp4", "progress": 1.0},
        {"name": "Pack/other.doc", "progress": 0.5},
    ]
    assert selected_paths_all_complete(file_items, ["Pack"]) is False


def test_move_selected_allows_partial_torrent_when_selected_file_complete(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        source = tmp_path / "config-downloads" / "movies-staging" / "done.mp4"
        source.parent.mkdir(parents=True)
        source.write_text("movie")
        client = QbitClient(cfg)
        delete_args = None

        async def fake_torrent(_hash):
            return QbitTorrent(
                hash="partial",
                name="Partial torrent",
                progress=0.5,
                save_path="/app/qBittorrent/downloads/movies-staging",
                category="movies-staging",
                is_complete=False,
            )

        async def fake_files(_hash):
            return [
                {"name": "done.mp4", "progress": 1.0},
                {"name": "other.doc", "progress": 0.0},
            ]

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
        monkeypatch.setattr(client, "files", fake_files)
        monkeypatch.setattr(client, "stop_torrent", fake_stop)
        monkeypatch.setattr(client, "delete_torrent", fake_delete)

        moved = await client.move_selected("partial", ["done.mp4"], "movies", "Movie (2026)")
        assert moved[0]["destination"].endswith("done.mp4")
        assert delete_args == ("partial", True)
        await client.close()

    asyncio.run(run())


def test_jellyfin_target_suggestions_match_existing_tmdb_folder(tmp_path):
    cfg = settings(tmp_path)
    target = tmp_path / "jellyfin" / "movies" / "飞驰人生3 (2026) [tmdbid-1462229]"
    target.mkdir(parents=True)
    query = "奶活家教发布组★飞驰人生3 Pegasus 3★WEB-DL★HDR★1080P★60帧率★x265 AAC MKV★简体中文.mkv"
    suggestions = jellyfin_target_suggestions(query, cfg)
    assert suggestions[0]["category"] == "movies"
    assert suggestions[0]["folder"] == "飞驰人生3 (2026) [tmdbid-1462229]"
    assert suggestions[0]["score"] == 1.0


def test_jellyfin_target_suggestions_do_not_match_prior_sequel_folder(tmp_path):
    cfg = settings(tmp_path)
    target = tmp_path / "jellyfin" / "movies" / "疯狂动物城 (2016) [tmdbid-269149]"
    target.mkdir(parents=True)
    query = "【高清影视之家发布 www.BBQDDQ.com】 疯狂动物城2[简繁英字幕].Zootopia.2.2025.2160p.iT.WEB-DL.H.265.DDP5.1.Atmos-QuickIO"

    score, reason = target_score(query, target.name)
    assert score < 0.5
    assert "续集" in reason or "年份不一致" in reason


def test_jellyfin_target_suggestions_hide_low_confidence_existing_movie(tmp_path):
    cfg = settings(tmp_path)
    (tmp_path / "jellyfin" / "movies" / "Project Hail Mary (2026) [tmdbid-687163]").mkdir(parents=True)
    query = "【高清剧集网发布 www.BPHDTV.com】黑夜告白[第03集].Light.to.the.Night.S01.2026.1080p"

    suggestions = jellyfin_target_suggestions(query, cfg, include_fallback=False)

    assert suggestions == []


def test_llm_target_can_outrank_low_confidence_existing_prior_sequel(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        target = tmp_path / "jellyfin" / "movies" / "疯狂动物城 (2016) [tmdbid-269149]"
        target.mkdir(parents=True)

        async def fake_llm(query, settings, file_names=None):
            return {
                "category": "movies",
                "folder": "疯狂动物城2 (2025) [tmdbid-1234567]",
                "score": 0.9,
                "reason": "TMDb 匹配续集和年份",
                "existing": False,
            }

        monkeypatch.setattr("app.qbit.llm_generated_target_suggestion", fake_llm)
        query = "【高清影视之家发布 www.BBQDDQ.com】 疯狂动物城2[简繁英字幕].Zootopia.2.2025.2160p.iT.WEB-DL.H.265.DDP5.1.Atmos-QuickIO"
        suggestions = await jellyfin_target_suggestions_with_llm(query, cfg)
        assert suggestions[0]["folder"] == "疯狂动物城2 (2025) [tmdbid-1234567]"
        assert suggestions[0]["existing"] is False

    asyncio.run(run())


def test_llm_target_existing_state_is_refreshed_from_filesystem(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        target = tmp_path / "jellyfin" / "movies" / "疯狂动物城2 (2025) [tmdbid-1084242]"
        target.mkdir(parents=True)

        async def fake_llm(query, settings, file_names=None):
            return {
                "category": "movies",
                "folder": "疯狂动物城2 (2025) [tmdbid-1084242]",
                "score": 0.9,
                "reason": "TMDb 匹配",
                "existing": False,
            }

        monkeypatch.setattr("app.qbit.llm_generated_target_suggestion", fake_llm)
        suggestions = await jellyfin_target_suggestions_with_llm("Zootopia.2.2025.2160p", cfg)
        assert suggestions[0]["folder"] == "疯狂动物城2 (2025) [tmdbid-1084242]"
        assert suggestions[0]["existing"] is True

    asyncio.run(run())


def test_refresh_targets_existing_updates_without_llm(tmp_path):
    cfg = settings(tmp_path)
    target = tmp_path / "jellyfin" / "movies" / "Existing Movie (2026) [tmdbid-1]"
    target.mkdir(parents=True)

    refreshed = refresh_targets_existing(
        [
            {
                "category": "movies",
                "folder": "Existing Movie (2026) [tmdbid-1]",
                "score": 0.4,
                "reason": "cached",
                "existing": False,
            },
            {
                "category": "movies",
                "folder": "Missing Movie (2026) [tmdbid-2]",
                "score": 0.9,
                "reason": "cached",
                "existing": True,
            },
        ],
        cfg,
    )

    states = {item["folder"]: item["existing"] for item in refreshed}
    assert states["Existing Movie (2026) [tmdbid-1]"] is True
    assert states["Missing Movie (2026) [tmdbid-2]"] is False


def test_jellyfin_target_suggestions_generate_fallback(tmp_path):
    cfg = settings(tmp_path)
    (tmp_path / "jellyfin" / "movies").mkdir(parents=True)
    suggestions = jellyfin_target_suggestions("Some.Unknown.Release.1080p.WEB-DL.mkv", cfg)
    assert suggestions[-1]["existing"] is False
    assert suggestions[-1]["category"] == "movies"


def test_llm_target_must_have_tmdb_folder_rule():
    target = folder_name_from_llm_target(
        {
            "category": "movies",
            "title": "Wuthering Heights",
            "year": "2026",
            "tmdb_id": "12345",
            "reason": "TMDb 匹配",
        }
    )
    assert target
    assert target["folder"] == "Wuthering Heights (2026) [tmdbid-12345]"
    assert target["existing"] is False
    assert folder_name_from_llm_target({"category": "movies", "title": "高清影视之家发布", "year": "2026"}) is None


def test_llm_series_target_strips_tmdb_id_from_folder():
    target = folder_name_from_llm_target(
        {
            "category": "series",
            "title": "权力的游戏",
            "year": "2011",
            "tmdb_id": "1399",
            "series_folder": "权力的游戏 [tmdbid-1399]",
            "season_number": 1,
            "episode_numbers": [2],
        }
    )
    assert target
    assert target["folder"] == "权力的游戏/Season 01"
    assert target["rename_plan"]["preview"] == "权力的游戏 - S01E02"


def test_llm_series_target_does_not_require_tmdb_id():
    target = folder_name_from_llm_target(
        {
            "category": "series",
            "title": "Light to the Night",
            "series_folder": "Light to the Night",
            "season_number": 1,
            "episode_numbers": [3],
            "score": 0.93,
            "reason": "文件名显示 S01E03",
        }
    )
    assert target
    assert target["folder"] == "Light to the Night/Season 01"
    assert target["rename_plan"]["preview"] == "Light to the Night - S01E03"


def test_tmdb_candidate_helpers_extract_clean_title():
    query = "【高清影视之家发布 www.BBQDDQ.com】 呼啸山庄[中文字幕].Wuthering.Heights.2026.1080p.iTunes.WEB-DL.mkv"
    assert release_search_text(query) == "Wuthering Heights 2026"
    page = "<title>&quot;Wuthering Heights&quot; (2026) &#8212; The Movie Database (TMDB)</title>"
    parsed = parse_tmdb_page("movie", "1316092", "https://www.themoviedb.org/movie/1316092", page)
    assert parsed
    assert parsed["title"] == "Wuthering Heights"
    assert parsed["year"] == "2026"
    assert parsed["tmdb_id"] == "1316092"


def test_jellyfin_target_suggestions_with_llm_adds_rule_folder(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        (tmp_path / "jellyfin" / "movies").mkdir(parents=True)

        async def fake_llm(query, settings, file_names=None):
            return {
                "category": "movies",
                "folder": "Wuthering Heights (2026) [tmdbid-12345]",
                "score": 0.86,
                "reason": "LLM/TMDb 生成",
                "existing": False,
            }

        monkeypatch.setattr("app.qbit.llm_generated_target_suggestion", fake_llm)
        suggestions = await jellyfin_target_suggestions_with_llm("www.BBQDDQ.com Wuthering.Heights.2026.1080p.WEB-DL", cfg)
        assert suggestions[0]["folder"] == "Wuthering Heights (2026) [tmdbid-12345]"
        assert "高清" not in suggestions[0]["folder"]

    asyncio.run(run())


def test_target_generation_sends_file_context_to_llm(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        (tmp_path / "jellyfin" / "series").mkdir(parents=True)

        async def fake_llm(query, settings, file_names=None):
            assert "黑夜告白[第03集]" in query
            assert any("Light.to.the.Night.S01E03" in item for item in (file_names or []))
            return {
                "category": "series",
                "folder": "黑夜告白/Season 01",
                "score": 0.95,
                "reason": "文件名显示 S01E03，按电视剧入库",
                "existing": False,
                "media_type": "series",
                "series_folder": "黑夜告白",
                "season_number": 1,
                "episode_numbers": [3],
                "target_folder": "黑夜告白/Season 01",
            }

        monkeypatch.setattr("app.qbit.llm_generated_target_suggestion", fake_llm)
        suggestions = await jellyfin_target_suggestions_with_llm(
            "【高清剧集网发布 www.BPHDTV.com】黑夜告白[第03集][国语配音+中文字幕].Light.to.the.Night.S01.2026.1080p.WEB-DL.H264.AAC-BlackTV",
            cfg,
            file_names=["Light.to.the.Night.S01E03.2026.1080p.WEB-DL.H264.AAC-BlackTV.mkv"],
        )

        assert suggestions[0]["category"] == "series"
        assert suggestions[0]["folder"] == "黑夜告白/Season 01"
        assert suggestions[0]["episode_numbers"] == [3]
        assert all(item["category"] != "movies" for item in suggestions)

    asyncio.run(run())
