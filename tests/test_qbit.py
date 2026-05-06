from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.models import QbitTorrent
from app.qbit import (
    QbitClient,
    build_file_tree,
    compress_selected_paths,
    folder_name_from_llm_target,
    jellyfin_target_path,
    jellyfin_target_suggestions,
    jellyfin_target_suggestions_with_llm,
    move_selected_files,
    parse_tmdb_page,
    parse_episode_info,
    qbit_path_to_local,
    release_search_text,
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


def mock_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("POST", "http://qbit.test/api"))


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


def test_move_selected_file_rejects_existing_different_size(tmp_path):
    cfg = settings(tmp_path)
    source = tmp_path / "config-downloads" / "movies-staging" / "Pack" / "Disc" / "Movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("movie")
    target = tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_text("different")

    with pytest.raises(FileExistsError):
        move_selected_files(["Pack/Disc/Movie.mkv"], torrent(), "movies", "Movie (2026)", cfg)

    assert not (target.parent / "Movie.1.mkv").exists()


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


def test_move_series_folder_skips_ads_and_renames_media(tmp_path):
    cfg = settings(tmp_path)
    folder = tmp_path / "config-downloads" / "movies-staging" / "Pack"
    folder.mkdir(parents=True)
    (folder / "权力的游戏.S01E03.1080p.mkv").write_text("episode")
    (folder / "广告.png").write_text("ad")
    move_selected_files(["Pack"], torrent(), "series", "权力的游戏/Season 01", cfg)
    assert (tmp_path / "jellyfin" / "series" / "权力的游戏" / "Season 01" / "权力的游戏 - S01E03.mkv").exists()
    assert not (tmp_path / "jellyfin" / "series" / "权力的游戏" / "Season 01" / "广告.png").exists()


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

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal deleted
            deleted = True

        monkeypatch.setattr(client, "torrent", fake_torrent)
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

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal deleted
            deleted = True

        monkeypatch.setattr(client, "torrent", fake_torrent)
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

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
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

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
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

        monkeypatch.setattr(client, "login", fake_login)
        monkeypatch.setattr(client.client, "post", fake_post)

        await client.delete_torrent("abc", delete_files=True)
        assert calls == [("/api/v2/torrents/delete", {"hashes": "abc", "deleteFiles": "true"})]
        await client.close()

    asyncio.run(run())


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

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
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

        async def fake_stop(_hash):
            return None

        async def fake_delete(_hash, delete_files):
            nonlocal delete_args
            delete_args = (_hash, delete_files)

        monkeypatch.setattr(client, "torrent", fake_torrent)
        monkeypatch.setattr(client, "stop_torrent", fake_stop)
        monkeypatch.setattr(client, "delete_torrent", fake_delete)

        await client.move_selected("ghi", ["Movie.mkv"], "movies", "Movie (2026)")
        assert delete_args == ("ghi", True)
        assert (tmp_path / "jellyfin" / "movies" / "Movie (2026)" / "Movie.mkv").exists()
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


def test_llm_target_can_outrank_low_confidence_existing_prior_sequel(monkeypatch, tmp_path):
    async def run():
        cfg = settings(tmp_path)
        target = tmp_path / "jellyfin" / "movies" / "疯狂动物城 (2016) [tmdbid-269149]"
        target.mkdir(parents=True)

        async def fake_llm(query, settings):
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

        async def fake_llm(query, settings):
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

        async def fake_llm(query, settings):
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
