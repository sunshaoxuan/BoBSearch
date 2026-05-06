# Changelog

All notable changes to BoBSearch will be documented in this file.

## Unreleased

No unreleased changes.

## [1.0.4] - 2026-05-06

### Changed

- Reopening a download task now reuses the cached file tree instead of rebuilding it every time.
- Target-folder status refresh now uses a lightweight endpoint that only checks current Jellyfin filesystem state.
- Full target-name generation only runs on first open or when the user clears/regenerates the target name.

## [1.0.3] - 2026-05-06

### Changed

- UI service labels now use product-facing names: search tool, download tool, and large model.
- Health panel errors are sanitized so implementation names are not shown in the interface.

## [1.0.2] - 2026-05-06

### Changed

- qBittorrent delete now verifies that the task actually disappears after `deleteFiles=true`.
- Move-and-clean removes the completed task from the UI immediately, then forces a qB refresh even if the 15-second auto-refresh is running.

### Fixed

- Fixed a case where moved or skipped files were treated as complete but the qB task could remain visible or remain undeleted.
- Fixed forced qB refresh being blocked by an in-flight automatic refresh after a move-and-clean operation.

## [1.0.1] - 2026-05-06

### Added

- Server-side search history persisted in `APP_DATA_DIR`, keeping the latest 30 searches by default.
- History APIs for listing, restoring the latest result, and loading a specific saved search without rerunning Jackett or LLM.
- Active tab persistence so refreshing on Download Management stays on Download Management.
- qBittorrent task controls in Download Management: start, stop, and delete task with files.
- Icon-only torrent controls with a separate expand/collapse file row.
- TV-series Jellyfin targets using `series/<show>/Season NN`.
- Jellyfin-compatible TV episode rename planning, for example `Show - S01E02.mkv`.
- Cached Jellyfin target-folder suggestions with a manual clear/regenerate action.
- Modal progress overlay while moving selected downloads into Jellyfin and cleaning qB tasks.
- Download Management auto-refresh every 15 seconds while the tab is active.

### Changed

- Download Management now shows all qBittorrent tasks instead of only the staging category.
- Search result action text now says "Add to Download" instead of exposing qBittorrent wording.
- Jellyfin target existence is refreshed from the filesystem before suggestions are returned.
- Auto-refresh preserves expanded torrent file panels.
- Move-and-clean is retry-safe: if a selected file was already moved and the destination exists, the item is skipped as complete and qB cleanup still runs.
- Existing-destination handling no longer creates `.1`, `.2`, or other duplicate files during move retries.
- Target status is displayed separately from target folder names.

### Fixed

- Prevented stale target-folder cache from showing existing folders as newly created.
- Avoided sequel mismatches when matching release names against existing movie folders.
- Kept qB tasks from getting stuck after a partial/repeated Jellyfin move where destination files already exist.

## [1.0.0] - 2026-05-05

### Added

- BoBSearch product branding and logo assets.
- External-services deployment mode for existing Jackett/qBittorrent/Jellyfin stacks.
- Bundled Docker profile for internal Jackett and qBittorrent support services.
- `env.sample` and `env.bundled.sample` for publishable configuration.
- Responsive mobile UI with expandable long names.
- Jackett search aggregation, deterministic deduplication, relevance scoring, and LLM cleanup.
- qBittorrent add/download management and Jellyfin move workflow.
- TMDb-aware Jellyfin target folder naming.

### Changed

- Docker Compose is env-driven and exposes only BoBSearch by default.
- Runtime configuration is consolidated into `.env`.
- Documentation rewritten for public release usage.
