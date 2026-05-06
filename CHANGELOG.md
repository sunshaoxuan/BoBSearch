# Changelog

All notable changes to BoBSearch will be documented in this file.

## Unreleased

### Added

- qBittorrent task controls in Download Management: start, stop, and delete task with files.
- Icon-only torrent controls with a separate expand/collapse file row.
- Cached Jellyfin target-folder suggestions with a manual clear/regenerate action.
- Safer Jellyfin target matching for sequels and release-year mismatches.
- Modal progress overlay while moving selected downloads into Jellyfin and cleaning qB tasks.

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
