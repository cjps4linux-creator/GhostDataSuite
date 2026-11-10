# Ghost-Data Suite

<p align="center">
  <a href="https://github.com/cjps4linux-creator/ghost-data-suite/actions/workflows/ci.yml"><img src="https://github.com/cjps4linux-creator/ghost-data-suite/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/cjps4linux-creator/ghost-data-suite/releases/tag/v1.0.0"><img src="https://img.shields.io/badge/release-v1.0.0-0a7563" alt="Release v1.0.0"></a>
</p>

A hardened, encrypted web scraping and monitoring stack with dashboard, auth, multi-target support, export, metrics, and Windows launcher.

## Quick Start

```bash
cd path\to\ghost-data-suite
docker compose up -d --build
```

Then open:
- http://localhost:5000/
- Desktop shortcut: `Ghost-Data Suite.lnk`

## Structure

- `src/ghost_server.py`  -  Hardened Flask API + SocketIO + metrics
- `src/scraper.py`  -  Multi-target Playwright scraper with plugin hooks
- `dashboard.html`  -  Full dashboard with auth, search, export, live logs
- `config/config.yaml`  -  Targets, auth token, intervals
- `keys/master.key`  -  AES-256 key (auto-generated)
- `data/alchemy.db`  -  Encrypted captures
- `scripts/launch-dashboard.bat`  -  Launcher
- `scripts/key-rotate.py`  -  Key rotation utility
- `scripts/backup-restore.py`  -  Backup/restore utility
- `hooks/`  -  Plugin hooks (optional)

## Auth

Set `GHOST_AUTH_TOKEN` in `docker-compose.yml` or `config/config.yaml`. Dashboard uses `X-Ghost-Token` header. Token is stored in browser localStorage.

## API

- `/`  -  Dashboard
- `/health`  -  Health check
- `/status`  -  DB stats, key status
- `/captures`  -  List captures (`?q=hash&status=ok|error`)
- `/captures/<[lorem]>/decrypt`  -  Decrypt capture
- `/export/json`  -  Export all captures
- `/export/csv`  -  Export CSV
- `/logs`  -  Tail logs
- `/logs/clear`  -  Clear logs
- `/scrape`  -  POST JSON `{url, interval}` trigger scrape
- `/backup`  -  Download encrypted backup ZIP
- `/restore`  -  Upload backup ZIP
- `/metrics`  -  Prometheus metrics

## Environment

- `GHOST_AUTH_TOKEN`  -  API/dashboard auth token
- `TARGET_URL`  -  Fallback scrape target
- `SCRAPE_INTERVAL`  -  Fallback interval
- `DB_PATH`  -  Default `data/alchemy.db`
- `ENCRYPTION_KEY_FILE`  -  Default `keys/master.key`
- `LOG_LEVEL`  -  `INFO`, `DEBUG`, or `ERROR`
- `SILENT_MODE`  -  `true` reduces logs
- `GHOST_PORT`  -  server port, default `5000`
- `SECRET_FILE`  -  Flask secret key path

---

Built by **Conrad CJ Wilson**  -  [GitHub](https://github.com/cjps4linux-creator) · [LinkedIn](https://linkedin.com/in/conradcjwilson)
