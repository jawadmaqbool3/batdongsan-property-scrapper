# ApartmentsVietnam Property Scraper

A production-oriented pipeline for collecting real-estate listings from a
public property marketplace, moving listing images into owned FTP storage,
resolving agents' hidden phone numbers, and translating listing content.

Data is stored in PostgreSQL (default) or MySQL. A small PHP/PDO endpoint
serves filtered, paginated JSON to a frontend.

The three main workers run concurrently behind a single entry point and are
designed to run as a long-lived daemon on Linux. The pipeline is idempotent:
listings are deduplicated by `listing_code`, and re-runs refresh
`last_seen_at` instead of creating duplicates.

---

## Highlights

- **Concurrent workers** — scraping, image migration, and phone resolution run
  independently; a failure in one does not stop the others.
- **Two storage backends** — PostgreSQL via `DATABASE_URL`, or MySQL via
  individual `DB_*` variables.
- **Image ownership** — downloads source images, optionally re-encodes them,
  uploads to FTP, and rewrites DB URLs to the public CDN/base path.
- **Agent phone resolution** — rotates accounts and proxies, reuses per-account
  browser sessions, and backfills every listing for an agent after one reveal.
- **Batch translation** — optional supervised worker pool for translating
  stored listing text.
- **Operational scripts** — idempotent setup, daemon start/stop, status
  reporting, and log tailing.

---

## Architecture

```
                         ┌──────────────────┐
                         │     main.py      │
                         └────────┬─────────┘
                                  │ asyncio
          ┌───────────────────────┼───────────────────────┐
          ▼                       ▼                       ▼
  ┌───────────────┐      ┌─────────────────┐     ┌────────────────────┐
  │  src/scraper  │      │ src/ftp_upload  │     │ src/agent_number_  │
  │               │      │                 │     │ finder             │
  │ crawl + upsert│      │ images → FTP    │     │ login + reveal     │
  └───────┬───────┘      └────────┬────────┘     └─────────┬──────────┘
          │                       │                        │
          └───────────────────────┼────────────────────────┘
                                  ▼
                         ┌──────────────────┐
                         │ PostgreSQL/MySQL │
                         └────────┬─────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
          ┌──────────────────┐        ┌──────────────────────┐
          │ api/get_props.php│        │ src/translate.py     │
          │ read-only JSON   │        │ batch translation    │
          └──────────────────┘        └──────────────────────┘
```

---

## Project layout

```
property-scraper/
├── .env                         # Real config (NEVER commit)
├── .env.example                 # Redacted template
├── .gitignore
├── requirements.txt
├── schema.sql                   # MySQL DDL (single `scrapy` table)
├── README.md
│
├── main.py                      # Run all three workers concurrently
├── probe_extract.py             # Dev helper: dump one listing's fields
├── probe_login.py               # Dev helper: inspect login page elements
│
├── setup.sh                     # One-time Linux setup
├── start.sh                     # Start main.py as a daemon under Xvfb
├── stop.sh                      # Stop workers + sweep orphan browsers
├── status.sh                    # Liveness, children, logs, DB summary
│
├── src/
│   ├── scraper.py               # Worker 1 — crawl categories, upsert listings
│   ├── ftp_upload.py            # Worker 2 — migrate images to FTP
│   ├── agent_number_finder.py   # Worker 3 — log in, reveal phones
│   ├── db.py                    # MySQL backend (pymysql)
│   ├── pg_store.py              # PostgreSQL backend (psycopg)
│   ├── store.py                 # Backend dispatch from STORAGE
│   └── translate.py             # Batch translation worker
│
├── api/
│   └── get_properties.php       # Read endpoint (filters + pagination)
│
├── run_workers.sh               # Translation worker pool (Linux)
├── run_workers.ps1              # Translation worker pool (Windows)
├── _worker_supervisor.ps1       # Per-worker restart loop (Windows)
├── progress.ps1                 # Translation progress (Windows)
├── _progress.py                 # Translation progress (Python)
│
├── data/                        # Runtime credentials + progress (gitignored)
├── logs/                        # Runtime logs (gitignored)
├── .pw-data-scraper/            # Scraper browser profile (gitignored)
├── .pw-data-acct-*/             # Per-account finder profiles (gitignored)
└── .venv/                       # Python virtualenv (gitignored)
```

---

## Requirements

Linux deployment target:

- Ubuntu/Debian (or another distro with `apt`, `dnf`, or `yum`)
- Python 3.11+
- Xvfb — installed by `setup.sh`; provides a virtual display for headed Chrome
- Chromium — installed through Playwright/Patchright
- PostgreSQL (default) or MySQL 5.7+ / 8.0+ with `utf8mb4`
- An FTP account with write access to `FTP_REMOTE_ROOT`

For development on Windows or macOS, the same Python version and Chromium are
enough; `setup.sh` is Linux-only, so install dependencies manually.

---

## Setup

### 1. Clone and bootstrap

```bash
git clone <repo> property-scraper
cd property-scraper
bash setup.sh
```

`setup.sh` is idempotent and will:

- create `.venv/` and install `requirements.txt`
- install Chromium for Playwright and Patchright
- install `xvfb` if it is missing
- create header-only `data/accounts.csv` and `data/proxies.csv` templates
- copy `.env.example` to `.env` when no `.env` exists

### 2. Configure environment

```bash
cp .env.example .env   # if setup.sh did not already
chmod 600 .env         # contains DB, FTP, and account passwords
```

Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `STORAGE` | `postgres` | `postgres` (libpq URL) or `mysql` |
| `DATABASE_URL` | — | Postgres connection string, e.g. `postgresql://user:pw@host/db?sslmode=require` |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | localhost / 3306 / root / "" / property | Read only when `STORAGE=mysql` |
| `FTP_HOST` / `FTP_USER` / `FTP_PASSWORD` | — | FTP credentials |
| `FTP_REMOTE_ROOT` | `/properties` | Remote folder for per-listing directories |
| `FTP_PUBLIC_BASE` | `https://apartmentsvietnam.com/properties` | Public URL prefix written to the DB |
| `FTP_SLEEP_BETWEEN_LISTINGS_SEC` / `FTP_SLEEP_WHEN_IDLE_SEC` | 1 / 60 | Uploader pacing |
| `FTP_TRANSFORM` / `FTP_JPEG_QUALITY` | true / 90 | Optional image re-encoding |
| `HEADLESS` | `false` | Scraper headlessness |
| `MAX_PAGES_PER_CATEGORY` | `0` | `0` = walk all pages; `1` for smoke tests |
| `MAX_CONSECUTIVE_FAILURES` | `5` | Stop a category after repeated failures |
| `REQUEST_DELAY_MIN_MS` / `REQUEST_DELAY_MAX_MS` | 800 / 2000 | Random request delay |
| `PROXY_SERVER` / `PROXY_USER` / `PROXY_PASSWORD` | — | Optional scraper proxy |
| `SOURCE_USERNAME` / `SOURCE_PASSWORD` | — | Optional credentials for authenticated probe/debug flows |
| `ACCOUNTS_CSV` / `PROXIES_CSV` | `data/accounts.csv` / `data/proxies.csv` | Phone-finder inputs |
| `AGENT_FETCH_DELAY_MIN_SEC` / `AGENT_FETCH_DELAY_MAX_SEC` | 270 / 330 | Delay between phone reveals |
| `ACCOUNT_COOLDOWN_SEC` | `1800` | Cool-down after a block or failed login |
| `FINDER_HEADLESS` | `false` | Phone-finder headlessness |
| `RUN_SCRAPER` / `RUN_FTP_UPLOAD` / `RUN_FINDER` | `true` | Per-worker switches used by `main.py` |

### 3. Phone-finder inputs

`setup.sh` creates header-only CSV templates. Fill them in before enabling
the finder:

```csv
# data/accounts.csv
username,password
0900000001,examplePass
0900000002,examplePass
```

```csv
# data/proxies.csv
server,username,password
http://proxy1.example.com:8000,proxyuser,proxypass
http://proxy2.example.com:8000,proxyuser,proxypass
```

Leave the files header-only to keep the finder idle, or set
`RUN_FINDER=false`.

### 4. Database

**PostgreSQL (default).** No manual schema step is required. `pg_store.py`
creates the `scrapy` table, indexes, and `last_seen_at` trigger on first
connect.

**MySQL.** Apply the DDL once:

```bash
mysql -h <host> -u <user> -p <database> < schema.sql
```

`schema.sql` creates a single `scrapy` table with:

- `UNIQUE KEY` on `listing_code` for deduplication
- `UNIQUE KEY` on `url_source(255)`
- indexes on property type, listing type, price, acreage, location, and
  `last_seen_at`

### 5. Windows / macOS development

`setup.sh` is bash-only. For local development:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip
.venv/bin/python -m playwright install chromium
.venv/bin/python -m patchright install chromium
```

Then run `python main.py` directly. No Xvfb or daemonization is needed; a real
browser window is useful for the first challenge solve and for debugging
selectors with the probe scripts.

---

## Running

### First run: Cloudflare bootstrap

The source marketplace is protected by Cloudflare. Patchright handles most
passive checks, but an interactive challenge may appear once. Either:

1. Solve the challenge on a desktop machine and copy `.pw-data-scraper/` to
   the server, or
2. Connect to the server's Xvfb display via VNC and click through it.

The clearance cookie then persists in `.pw-data-scraper/` across restarts.

### Smoke test

```ini
# .env
MAX_PAGES_PER_CATEGORY=1
RUN_FTP_UPLOAD=false
RUN_FINDER=false
```

```bash
bash start.sh
bash status.sh
tail -f logs/scrape.log
```

Check the database:

```sql
SELECT COUNT(*) FROM scrapy;
SELECT large_image_paths FROM scrapy LIMIT 1;
```

When the output looks correct, restore the worker switches to `true` and
`MAX_PAGES_PER_CATEGORY=0`, then restart.

### Daemon lifecycle

```bash
bash start.sh         # background, survives SSH logout
bash status.sh        # liveness + children + log tail + DB summary
bash stop.sh          # graceful TERM, then KILL after 8s
bash stop.sh --clean  # also wipe .pw-data-* profiles
```

`start.sh` runs `nohup setsid xvfb-run -a .venv/bin/python main.py`, writes
the PID to `.scraper.pid`, and refuses to start a second instance.

`stop.sh` kills the process group and sweeps orphaned `xvfb-run`, Chromium,
and worker processes.

The pipeline is idempotent and continuous. For scheduled daily restarts, use
`stop.sh && start.sh`.

---

## Captured fields

| Field | Description |
|---|---|
| `url_source` | Full URL of the listing |
| `listing_code` | Unique property ID parsed from the URL |
| `title` | Headline |
| `slug` | SEO-friendly URL text |
| `description` | Main body content |
| `footer_disclaimer` | Disclaimer block above the footer |
| `price` | Price in VND, parsed from "tỷ" / "triệu" |
| `acreage` | Land area in m² |
| `bedrooms` / `bathrooms` | Room counts |
| `property_type` | Apartment / villa / house / land / etc. |
| `legal_type` | Ownership or legal status |
| `news_type` | Ad tier, e.g. "Tin VIP Kim Cương" |
| `interior` | Furnished / unfurnished |
| `date_posted` | Listing post date |
| `posted_by` | Agent, agency, or owner name |
| `hidden_number` | Phone as displayed before reveal |
| `phone_number` | Unmasked phone, written by the finder |
| `agent_id` | Numeric agent ID |
| `agent_url` | Agent profile URL |
| `location_city` | Top-level administrative area |
| `location_province` | District within the city |
| `listing_type` | "Bán" or "Cho thuê" |
| `thumbnail_paths` | JSON array of thumbnail URLs |
| `large_image_paths` | JSON array of full-size image URLs |
| `created_at` | First-insert timestamp |

Auxiliary fields: `price_raw`, `direction`, `address`, `ward`, `street`,
`latitude`, `longitude`, `expires_at_raw`, `category`, `last_seen_at`.

Missing data is stored as `NULL`, never guessed.

---

## Worker details

### 1. Scraper — `src/scraper.py`

- Opens a Patchright/Playwright persistent context in `.pw-data-scraper/`.
- Walks configured category/city targets, following `/p1`, `/p2`, … until no
  new URLs are found or `MAX_PAGES_PER_CATEGORY` is reached.
- Extracts listing fields and the agent profile URL, then upserts on
  `listing_code`.

### 2. Image migration — `src/ftp_upload.py`

- Polls for rows whose image URLs still point at the source marketplace.
- Downloads each image, optionally re-encodes it, and uploads it to
  `{FTP_REMOTE_ROOT}/<listing_code>/`.
- Rewrites the row with public URLs under `{FTP_PUBLIC_BASE}`.
- Per-image failures are logged and skipped; a listing is never aborted
  because one image failed.

### 3. Phone finder — `src/agent_number_finder.py`

- Reads `data/accounts.csv` and `data/proxies.csv`.
- For each agent with no phone number:
  - selects an active `(account, proxy)` pair, skipping cooled pairs,
  - opens the agent profile and logs in once per account,
  - clicks the reveal control and reads the unmasked number,
  - updates every listing for that agent in one statement,
  - sleeps between reveals to stay under the per-account quota.
- On a block or failed login, cools the pair and rotates to the next.

If FTP settings are unset, the uploader stays idle and the other workers keep
running.

### 4. Batch translation — `src/translate.py`

Translates stored listing text and marks rows with `translated_at` so runs can
resume. It has no built-in supervisor; use the runner for your platform:

```bash
./run_workers.sh            # 10 workers (override with a number)
./run_workers.sh status
./run_workers.sh stop
```

```powershell
.\run_workers.ps1 -Workers 10
.\run_workers.ps1 -Action status
.\run_workers.ps1 -Action stop
```

Use `progress.ps1` on Windows or `_progress.py` anywhere for throughput and
ETA. The runner restarts a worker 30 seconds after it exits.

---

## FTP layout

```
/<FTP_REMOTE_ROOT>/
  └── <listing_code>/
        ├── img1.jpg       ← large_image_paths[0]
        ├── img2.jpg       ← large_image_paths[1]
        ├── ...
        ├── thumb1.jpg     ← thumbnail_paths[0]
        └── thumb2.jpg     ← thumbnail_paths[1]
```

Stored public URLs follow:

```
{FTP_PUBLIC_BASE}/{listing_code}/{filename}
```

With the default config this is:

```
https://apartmentsvietnam.com/properties/12345678/img1.jpg
```

The mapping between `FTP_REMOTE_ROOT` and the path served by the web server
must match. The default assumes the FTP user's home is the web document root
and `/properties` is served from the site root.

---

## Read API — `api/get_properties.php`

PHP/PDO endpoint serving filtered, paginated JSON. It currently uses a MySQL
DSN; for Postgres-only deployments, port the DSN to `pgsql:…` or expose a
separate API service.

```
GET api/get_properties.php
   ?property_type=Căn hộ chung cư
   &listing_type=Bán
   &location_city=Hà Nội
   &min_price=2000000000
   &max_price=10000000000
   &min_acreage=50
   &bedrooms=2
   &q=quận 7
   &page=1
   &per_page=50
```

It reads the same `DB_*` environment variables as the MySQL backend and can be
dropped into any PHP-capable web root.

---

## Troubleshooting

**`status.sh` reports a failed DB query.** Check `DB_NAME`, confirm MySQL is
running, and note that `status.sh` queries MySQL directly even when
`STORAGE=postgres`.

**Cloudflare interstitial in the logs.** The first challenge was not solved.
Run headed on a desktop or through VNC, solve it once, and copy
`.pw-data-scraper/` back. `bash stop.sh --clean` forces a fresh solve.

**`FTP uploader disabled`.** At least one of `FTP_HOST`, `FTP_USER`,
`FTP_PASSWORD`, or `FTP_PUBLIC_BASE` is unset.

**`FTP upload failed … 550`.** The remote directory could not be created.
Check write permissions on `FTP_REMOTE_ROOT`.

**Images return 404.** `FTP_PUBLIC_BASE` does not map to `FTP_REMOTE_ROOT` on
the web server. Upload a test file manually to verify the mapping.

**Slow runs or repeated timeouts.** Increase `REQUEST_DELAY_MIN_MS` and
`REQUEST_DELAY_MAX_MS`. The source marketplace rate-limits aggressively.

**Phone finder reveals nothing.** Verify `data/accounts.csv` is populated and
that each account can log in manually. Each account has a daily quota; add
more accounts or increase the delay.

**Re-scrape a listing.** Delete the row and let the scraper find it again:

```sql
DELETE FROM scrapy WHERE listing_code = '<id>';
```

**Debug extraction or login.** Use `python probe_extract.py <url>` for one
listing and `python probe_login.py` for the login page.

**Start refuses with "Already running".** A stale PID file exists. Run
`bash stop.sh`; `status.sh` also removes stale PID files when the process is
gone.

---

## Security

- Never commit `.env`; it contains DB, FTP, and account passwords.
- Never commit `data/accounts.csv` or `data/proxies.csv`. `.gitignore`
  excludes `data/*.csv`; use `git rm --cached` if one slips in.
- FTP transfers are plain FTP by default. If the host supports FTPS, switch
  to `FTP_TLS` in `src/ftp_upload.py`.
- The PHP endpoint uses prepared statements, so it has no SQL injection
  surface.
- Rotate any password that appears in a shared `.env`, log, or screenshot.

---

## File reference

| File | Purpose |
|---|---|
| `main.py` | Orchestrator for scraper, FTP upload, and phone finder |
| `src/scraper.py` | Crawl and upsert worker |
| `src/ftp_upload.py` | Image migration worker |
| `src/agent_number_finder.py` | Login and phone-reveal worker |
| `src/db.py` | MySQL backend |
| `src/pg_store.py` | PostgreSQL backend |
| `src/store.py` | Backend dispatch |
| `src/translate.py` | Batch translation worker |
| `schema.sql` | MySQL DDL |
| `api/get_properties.php` | Read endpoint with filters and pagination |
| `setup.sh` | One-time Linux setup |
| `start.sh` | Daemonize `main.py` under Xvfb |
| `stop.sh` | Graceful stop and orphan sweep |
| `status.sh` | Liveness, children, log tail, DB summary |
| `probe_extract.py` | Dev — extract one listing |
| `probe_login.py` | Dev — inspect login elements |
| `run_workers.sh` / `run_workers.ps1` | Translation worker pools |
| `_progress.py` / `progress.ps1` | Translation progress and ETA |
| `requirements.txt` | Python dependencies |
| `.env` / `.env.example` | Configuration |
| `.gitignore` | Keeps secrets, profiles, logs, and build output out of git |
