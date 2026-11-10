# Ghost-Data Suite  -  Tutorial & Walkthrough

## What is this?

Ghost-Data Suite is a **local, encrypted web scraping stack**.

It captures web pages, encrypts the content, stores it in a local database, and gives you a dashboard to view and manage everything.

Everything runs on **your machine**. Nothing is sent to the cloud.

---

## Prerequisites

- Windows 11
- Docker Desktop installed and running
- Git Bash or PowerShell
- A modern web browser

---

## Part 1: Start the Stack

### Step 1: Open the project folder

```bash
cd path\to\ghost-data-suite
```

### Step 2: Start the containers

```bash
docker compose up -d --build
```

**What this does:**
- Builds the `ghost-shell` and `alchemist-core` images
- Starts the Ghost Server (dashboard + API) on port 5000
- Starts the Data Alchemist (scraper) which captures pages
- Creates encrypted storage volumes

### Step 3: Verify everything is running

```bash
docker compose ps
```

**Expected output:**
```
NAME             IMAGE                          PORTS
alchemist-core   ghost-data-suite-alchemist     8080/tcp
ghost-shell      ghost-data-suite-ghost-shell   0.0.0.0:5000->5000/tcp
```

Both should say `Up`.

---

## Part 2: Access the Dashboard

### Step 1: Open your browser

Navigate to:

```
http://localhost:5000/
```

### Step 2: Enter the auth token

The dashboard will ask for an auth token.

Default token: **`change-me`**

Enter it and click **Set**.

**Why is there auth?**
- Prevents unauthorized access to your captured data
- All API requests require this token
- Change it in `docker-compose.yml` under `GHOST_AUTH_TOKEN`

---

## Part 3: Your First Safe Test

We'll use **https://example.com**  -  it's designed for testing and always available.

### Step 1: Trigger a manual capture

In the dashboard:
1. Find the **Scraper Control** section
2. URL field: `https://example.com`
3. Interval: `60` (seconds)
4. Click **Scrape Now**

**What happens:**
- The scraper opens a headless browser
- Navigates to the page
- Extracts the title and content
- Encrypts it with AES-256
- Stores it in the database

### Step 2: Watch the logs

In the dashboard, scroll to the **Live Logs** section.

You should see:
```
2026-09-04 10:21:16,277 | INFO | Starting Data Alchemist
2026-09-04 10:21:16,283 | INFO | Store initialized | Records=96
2026-09-04 10:21:16,954 | INFO | Navigating to https://example.com
2026-09-04 10:21:17,926 | INFO | Captured and encrypted | title=Example Domain
```

### Step 3: View your captures

In the **Captures** table, you'll see entries like:

| [lorem] | Created At | Status | URL Hash |
|----|------------|--------|----------|
| 96 | 2026-09-04 10:21:17 | ok | 100680ad... |

### Step 4: Decrypt a capture

1. Click the **Decrypt** button on any capture row
2. A modal opens showing the decrypted content
3. You'll see the page title, URL, and content

**Security note:** Decryption happens on demand. The database stores only encrypted blobs.

---

## Part 4: Explore the API

All API requests require the token header:

```
X-Ghost-Token: change-me
```

### Health check

```bash
curl http://localhost:5000/health
```

**Response:**
```json
{"service":"ghost-server","status":"ok"}
```

### Get status

```bash
curl -H 'X-Ghost-Token: change-me' http://localhost:5000/status
```

**Response:**
```json
{
  "db": {"capture_count": 96, "exists": true},
  "env": {"auth_enabled": true, "db_path": "/app/data/alchemy.db"},
  "key_exists": true,
  "service": "ghost-server"
}
```

### List captures

```bash
curl -H 'X-Ghost-Token: change-me' http://localhost:5000/captures
```

### Decrypt a capture ([lorem] 96)

```bash
curl -H 'X-Ghost-Token: change-me' http://localhost:5000/captures/96/decrypt
```

### Trigger a scrape

```bash
curl -X POST -H 'X-Ghost-Token: change-me' \
  'http://localhost:5000/scrape?url=https://example.com&interval=60'
```

### View logs

```bash
curl -H 'X-Ghost-Token: change-me' http://localhost:5000/logs
```

---

## Part 5: Safe Test Sites You Can Use

These are designed for testing and safe to scrape:

| Site | Purpose | Example |
|------|---------|---------|
| **example.com** | Basic HTML page | `https://example.com` |
| **httpbin.org** | HTTP testing service | `https://httpbin.org/html` |
| **jsonplaceholder.typicode.com** | Fake JSON API | `https://jsonplaceholder.typicode.com/posts/1` |
| **quotes.toscrape.com** | Book quotes (scraping practice) | `http://quotes.toscrape.com` |

**Recommended first test:**
```
https://example.com
```

---

## Part 6: Understanding What You Built

### Architecture

```
┌──────────────┐      5000/tcp      ┌──────────────────┐
│   Browser    │ ──────────────────▶│   ghost-shell    │
│  (you)       │ ◀──────────────────│  (Flask + API)   │
└──────────────┘                    └────────┬─────────┘
                                               │ shared volume
                                               ▼
                                    ┌──────────────────┐
                                    │  ghost-data      │
                                    │  (SQLite DB)     │
                                    └──────────────────┘

┌──────────────┐      8080/tcp      ┌──────────────────┐
│   Internet   │ ──────────────────▶│ alchemist-core   │
│              │ ◀──────────────────│ (Playwright +    │
└──────────────┘                    │  Encryption)     │
                                    └──────────────────┘
```

### Data flow

1. You click **Scrape Now** in the dashboard
2. Dashboard sends POST to `/scrape`
3. `ghost-shell` forwards request to `alchemist-core`
4. Alchemist opens headless Chromium
5. Navigates to the URL
6. Extracts page content
7. Encrypts with AES-256 Fernet
8. Stores in SQLite database
9. Returns success response
10. Dashboard refreshes capture list

### Encryption

- **Algorithm:** AES-256-CBC + HMAC
- **Key:** Stored in `keys/master.key` (auto-generated)
- **Database:** Contains only encrypted blobs
- **Decryption:** Happens only when you click **Decrypt**

---

## Part 7: Common Tasks

### Change the auth token

Edit `docker-compose.yml`:
```yaml
environment:
  GHOST_AUTH_TOKEN: "your-new-token-here"
```

Then restart:
```bash
docker compose up -d --force-recreate
```

### Change the scrape target

Option 1: Use the dashboard
- Enter new URL in the Scrape Now form

Option 2: Use the API
```bash
curl -X POST -H 'X-Ghost-Token: change-me' \
  'http://localhost:5000/scrape?url=https://httpbin.org/html&interval=30'
```

Option 3: Edit `config/config.yaml`
```yaml
alchemist:
  targets:
    - url: "https://example.com"
      interval: 60
      enabled: true
```

### Export captures

In the dashboard:
1. Click **Export JSON** or **Export CSV**
2. File downloads automatically

Via API:
```bash
curl -H 'X-Ghost-Token: change-me' http://localhost:5000/export/json -o captures.json
curl -H 'X-Ghost-Token: change-me' http://localhost:5000/export/csv -o captures.csv
```

### View logs

Dashboard: Scroll to **Live Logs** section

CLI:
```bash
curl -H 'X-Ghost-Token: change-me' http://localhost:5000/logs
```

### Clear logs

```bash
curl -X POST -H 'X-Ghost-Token: change-me' \
  -H 'Content-Type: application/json' \
  http://localhost:5000/logs/clear
```

---

## Part 8: Backing Up Your Data

### Manual backup

```bash
# Create a backup directory
mkdir -p backups

# Copy the database and keys
cp data\alchemy.db backups\
cp keys\master.key backups\
```

**Important:** Back up both files. Without the `master.key`, the database is unreadable.

### Using the backup script

```bash
cd path\to\ghost-data-suite
python scripts/backup-restore.py backup
```

### Restore from backup

```bash
cd path\to\ghost-data-suite
python scripts/backup-restore.py restore
```

---

## Part 9: Stopping and Restarting

### Stop the stack

```bash
cd path\to\ghost-data-suite
docker compose down
```

### Start the stack again

```bash
cd path\to\ghost-data-suite
docker compose up -d
```

### View logs

```bash
docker compose logs -f ghost-shell
docker compose logs -f alchemist
```

### Restart a specific service

```bash
docker compose restart alchemist
```

---

## Part 10: Troubleshooting

### Port 5000 already in use

If something else is on port 5000:

1. Edit `docker-compose.yml`:
   ```yaml
   ports:
     - "5001:5000"
   ```
2. Update `config/config.yaml`:
   ```yaml
   ghost_server:
     port: 5001
   ```
3. Restart: `docker compose up -d`

### Dashboard shows "unauthorized"

- Make sure you entered the correct token
- Default token is `change-me`
- Check `docker-compose.yml` for `GHOST_AUTH_TOKEN`

### No captures appearing

1. Check alchemist logs:
   ```bash
   docker compose logs alchemist
   ```
2. Verify the URL is accessible
3. Try a safe test site like `https://example.com`

### Logs are empty

- Logs are shared via `ghost-logs` volume
- Restart alchemist: `docker compose restart alchemist`
- Check file exists: `docker compose exec alchemist ls -l /app/logs`

### Containers keep restarting

Check logs for errors:
```bash
docker compose logs ghost-shell
docker compose logs alchemist
```

Common causes:
- Permission issues on volumes
- Missing dependencies
- Port conflicts

---

## Part 11: What's Next?

Once you're comfortable with the basics:

1. **Add more targets** in `config/config.yaml`
2. **Enable monitoring** with `docker compose --profile monitoring up -d`
3. **Set up auto-start** via Windows Startup folder
4. **Rotate the encryption key** with `python scripts/key-rotate.py`
5. **Create custom hooks** in the `hooks/` directory

---

## Quick Reference Card

| Action | Command / URL |
|--------|---------------|
| Start stack | `docker compose up -d --build` |
| Stop stack | `docker compose down` |
| View logs | `docker compose logs -f` |
| Dashboard | `http://localhost:5000/` |
| Default token | `change-me` |
| Test site | `https://example.com` |
| Backup DB | `python scripts/backup-restore.py backup` |
| Restore DB | `python scripts/backup-restore.py restore` |
| Rotate key | `python scripts/key-rotate.py` |
| API health | `curl http://localhost:5000/health` |

---

## Support

If you hit issues:

1. Check container logs: `docker compose logs`
2. Verify ports: `docker compose ps`
3. Restart stack: `docker compose down && docker compose up -d`

---

**You're ready to go. Start with Part 1 and work through each step.**
