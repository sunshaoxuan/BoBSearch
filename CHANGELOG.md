# Changelog

All notable changes to BoBSearch will be documented in this file.

## Unreleased

No unreleased changes.

## [1.0.21] - 2026-05-24

### Added

- Added a `software` destination category for non-movie/non-series downloads, mapped outside Jellyfin to the configured software library path.

## [1.0.20] - 2026-05-17

### Fixed

- Torrent-level progress now uses the same no-round-up display rule as file-level progress, so near-complete tasks do not appear more complete than qB reports.

## [1.0.19] - 2026-05-17

### Fixed

- File progress display no longer rounds incomplete qB files up to `100%`.
- Move-and-clean now remains strict: selected files must be truly complete before they can be moved, while near-complete files display as `99.x%`.

## [1.0.18] - 2026-05-17

### Fixed

- File-completion checks now use the same effective threshold as the UI display, so qB file progress values like `0.999` that display as `100%` are accepted for move-and-clean.
- The partial-torrent move validation now treats selected files consistently between the frontend button state and the backend qB file-progress check.

## [1.0.17] - 2026-05-17

### Changed

- Download management no longer requires the whole torrent task to be complete before moving to Jellyfin.
- The move button now becomes available as soon as the currently selected file or folder items are all at 100%.
- Backend move validation now checks qB file progress only for the selected paths, so completed videos inside otherwise incomplete tasks can be moved and cleaned immediately.

## [1.0.16] - 2026-05-11

### Changed

- Download-management start and stop buttons are now state-aware in the UI.
- Tasks that are already running can no longer trigger another start action from the current page state.
- Tasks that are already paused, stopped, or otherwise non-runnable can no longer trigger another stop action from the current page state.

## [1.0.15] - 2026-05-10

### Changed

- Series move-to-Jellyfin now asks the configured large model to assign episode numbers across the full selected file batch before renaming, so multi-file packs such as `01.mkv` and `02.mkv` can be mapped automatically without manual intervention.
- The LLM-provided per-file mapping is now honored during the actual move step, taking precedence over the older deterministic batch override when available.

### Fixed

- Fixed the case where separate selected episode files from the same TV pack could still be blocked by a rename collision instead of being auto-resolved into distinct episode targets.

## [1.0.14] - 2026-05-10

### Fixed

- Fixed a TV rename bug where selecting multiple plain episode files such as `01.mkv` and `02.mkv` under a `01-02` pack could incorrectly apply the same multi-episode target name to both files.
- Series rename-plan overrides are now computed once for the full selected batch, so `01` and `02` correctly become `S01E01` and `S01E02` instead of both becoming `S01E01-E02`.
- Added a same-operation destination collision guard: if two selected source files would resolve to the same Jellyfin target filename, the move is aborted before any overwrite can happen.

## [1.0.13] - 2026-05-09

### Changed

- Fallback-model configuration can now point to a separate OpenAI-compatible endpoint such as Ollama `/v1`, with an empty fallback API key.
- Health checks now validate the primary and fallback model against their own real endpoints instead of reusing the primary-model bearer token.
- Search-result LLM enrichment and Jellyfin target generation now actually fail over from the primary model to the configured fallback model.
- Local deployment now points the fallback model to `http://ccnode.briconbric.com:22545/v1` with `qwen3:14b`.

## [1.0.12] - 2026-05-09

### Changed

- Health panel labels now use `主模型` and `备用模型`.
- Local deployment now configures the fallback model to `gpt-5.4` on the current `ccnode` endpoint instead of leaving it empty.

## [1.0.11] - 2026-05-09

### Added

- Health status now shows both the primary large model and the optional fallback model separately.
- Added optional `LLM_FALLBACK_BASE_URL`, `LLM_FALLBACK_API_KEY`, and `LLM_FALLBACK_MODEL` configuration entries.

## [1.0.10] - 2026-05-09

### Fixed

- Series move-to-Jellyfin now uses the generated rename plan when torrent files are named only with plain episode numbers such as `01.mkv`, `02.mkv`.
- Multi-episode TV packs that were correctly classified as series no longer fail during the move step just because the local filenames do not contain `SxxEyy`.

## [1.0.9] - 2026-05-08

### Added

- Search history can now be cleared completely from the UI.
- The currently selected history item can now be deleted from the UI.
- Added backend APIs for deleting one saved search history item or clearing all saved history.

## [1.0.8] - 2026-05-08

### Fixed

- Adding an already existing download task is now idempotent and returns "下载任务已存在" instead of HTTP 500.
- qBittorrent `Fails.` responses are rechecked by info hash so duplicate add attempts are handled as successful no-ops.

## [1.0.7] - 2026-05-06

### Changed

- Series target choices now show only the real Jellyfin directory, for example `series/黑夜告白/Season 01`.
- Episode codes such as `S01E03` and rename previews are displayed in the explanation line instead of being mixed into the target folder label.

## [1.0.6] - 2026-05-06

### Changed

- Jellyfin target generation now sends the qBittorrent task name plus its file list to the large model, so episode evidence inside downloaded file names can drive movie-vs-series classification.
- Download-task folder generation prompts the large model to treat episode releases as TV series and return `series/<show>/Season NN` targets with episode rename metadata.

## [1.0.5] - 2026-05-06

### Changed

- Move-and-clean now replaces an existing same-name target file when the source file size differs, instead of failing and leaving the download task behind.
- Existing same-size files are still skipped as complete, and no `.1` duplicate files are created.

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
