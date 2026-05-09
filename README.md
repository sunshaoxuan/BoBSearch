# BoBSearch

Current release: **1.0.12**

BoBSearch is a self-hosted media search and download management console. It searches Jackett indexers, deduplicates results, uses an OpenAI-compatible LLM to summarize releases, adds selected resources to qBittorrent, and moves completed downloads into a Jellyfin library.

Only BoBSearch needs to be exposed to users. qBittorrent and Jackett can either be existing external services or bundled internal support containers.

## Features

- Search Jackett indexers with deterministic deduplication.
- Relevance scoring for Chinese and mixed-language queries.
- LLM-assisted release naming, quality tags, and recommendation notes.
- Add selected results to qBittorrent through BoBSearch only.
- Manage qBittorrent tasks, inspect file trees, and move completed files to Jellyfin.
- Start, stop, and delete qBittorrent tasks, including delete-with-files cleanup.
- Generate Jellyfin movie folder names using existing folder rules and TMDb IDs.
- Move TV episodes into `series/<show>/Season NN` and rename video files in Jellyfin-compatible `Show - SxxEyy.ext` form.
- Use qBittorrent file lists as LLM context when generating Jellyfin targets, so episode files can be classified as series even when the task name is noisy.
- Keep the last 30 successful searches in server-side history so refreshes and repeated review do not rerun Jackett or LLM.
- Retry-safe Jellyfin moves: already moved files with matching destinations are skipped and qB cleanup still completes.
- Download Management refreshes every 15 seconds while preserving expanded task panels.
- Responsive Web UI for desktop and mobile.

## Deployment Modes

### External Services

Use this when you already run Jackett, qBittorrent, and Jellyfin.

```bash
cp env.sample .env
# edit .env
docker compose up -d --build
```

This starts only BoBSearch.

### Bundled Services

Use this when you do not already run Jackett or qBittorrent.

```bash
cp env.bundled.sample .env
# edit .env
docker compose --profile bundled up -d --build
```

This starts BoBSearch, Jackett, and qBittorrent. Jackett and qBittorrent are not exposed through host ports by default; BoBSearch reaches them on the internal Compose network.

Notes:

- Jackett still requires indexer configuration. Public indexers can be added later through a BoBSearch source-management feature; private indexers require user credentials.
- Current LinuxServer qBittorrent images print a temporary admin password on first startup unless a permanent WebUI password has already been set. Align `QBIT_USERNAME` / `QBIT_PASSWORD` in `.env` with the internal qBittorrent credentials before using add/download management.

## Configuration

BoBSearch reads `.env` at runtime. Do not commit `.env`.

Important groups:

- `APP_*`: app name/version, image, container name, public port, uvicorn host/port.
- `WEB_*`: BoBSearch login credentials.
- `JACKETT_*`: Jackett API URL, API key, and indexer config mount.
- `QBIT_*`: qBittorrent API URL, credentials, category, and path mapping.
- `JELLYFIN_*`: Jellyfin library path mounted into BoBSearch.
- `LLM_*`: OpenAI-compatible API base URL, key, main model, and optional fallback model.
- `SEARCH_*`: concurrency and timeout tuning.

The qB path mapping is important:

- `QBIT_DOWNLOADS_PATH` is the path as qBittorrent reports it.
- `QBIT_LOCAL_DOWNLOADS_PATH` is the matching path inside the BoBSearch container.
- Host paths are only used by Docker Compose volume mounts.

## Security

- Keep BoBSearch behind a trusted LAN, VPN, or reverse proxy with authentication.
- Use a strong `WEB_PASSWORD` and high-entropy `SESSION_SECRET`.
- Never commit `.env`, API keys, qB passwords, or private tracker credentials.
- Bundled qBittorrent and Jackett are internal support services by default and should not be exposed unless debugging.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
node --check app/static/app.js
.venv/bin/pytest -q
.venv/bin/python -m compileall app
```

Validate Compose files:

```bash
docker compose --env-file env.sample config
docker compose --env-file env.bundled.sample --profile bundled config
```

## Remote Deploy Helper

The optional deploy helper copies the repo and a local env file to a remote Docker host.

Required `.env` values:

```env
DEPLOY_SSH_USER=
DEPLOY_SSH_HOST=
DEPLOY_TARGET_DIR=
DEPLOY_SSH_PASSWORD=
```

Alternatively, on macOS, set `DEPLOY_SSH_PASSWORD_KEYCHAIN_ACCOUNT` and `DEPLOY_SSH_PASSWORD_KEYCHAIN_SERVICE`.

Run:

```bash
./scripts/deploy.sh
```

Use bundled services remotely:

```bash
COMPOSE_PROFILES=bundled ./scripts/deploy.sh
```

Push the built image:

```bash
PUBLISH_DOCKERHUB=1 ./scripts/deploy.sh
```
