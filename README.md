# ApartmentsVietnam Property Scraper

Three-worker pipeline that scrapes real-estate listings from
[the source site](https://batdongsan.com.vn), migrates the listing images to
your FTP server, and resolves the agents' hidden phone numbers by logging in
through rotating accounts and proxies. Data is written to PostgreSQL (default,
e.g. a Neon connection) or MySQL.

A PHP read endpoint (`api/get_properties.php`) is included for serving the
scraped data to a frontend.

The pipeline is designed to run as a long-lived background daemon on a Linux
server (Ubuntu/Debian tested), driven by Xvfb so headed Chrome works without a
real display. Windows is supported for development but not for deployment.

---

## 1. What it captures

For every listing page, the scraper extracts these fields (per client spec):

| Field | Description |
|---|---|
| `url_source` | Full URL of the listing |
| `listing_code` | Unique property ID (from `-pr<digits>` in the URL) |
| `title` | Headline |
| `slug` | SEO-friendly URL text |
| `description` | Main body content |
| `footer_disclaimer` | Disclaimer block above the footer |
| `price` | Price in VND (parsed from "tỷ" / "triệu") |
| `acreage` | Land area in m² |
| `bedrooms` | Number of bedrooms |
| `bathrooms` | Number of bathrooms |
| `property_type` | apartment / villa / house / land / etc. (mapped from category) |
| `legal_type` | Ownership / legal status |
| `news_type` | Ad tier (e.g. "Tin VIP Kim Cương") |
| `interior` | Furnished / unfurnished |
| `date_posted` | Date listing was posted |
| `posted_by` | Agent / agency / owner name |
| `hidden_number` | Phone as the site shows it (often masked) |
| `phone_number` | Unmasked phone — written by the phone-finder worker after login |
| `agent_id` | Numeric agent ID resolved from the agent profile page |
| `agent_url` | Agent profile URL |
| `location_city` | Top-level admin (province) |
| `location_province` | District within the city |
| `listing_type` | "Bán" or "Cho thuê" |
| `thumbnail_paths` | JSON array — small image URLs (on apartmentsvietnam.com after FTP migration) |
| `large_image_paths` | JSON array — full-size image URLs (on apartmentsvietnam.com after FTP migration) |
| `created_at` | Timestamp set by the DB on first insert |

Auxiliary fields also captured: `price_raw`, `direction`, `address`, `ward`,
`street`, `latitude`, `longitude`, `expires_at_raw`, `category`, `last_seen_at`.

**Missing data is stored as `NULL`** — never guessed.

---

## 2. Project layout

```
property-scraper/
├── .env                         # Real config (NEVER commit)
├── .env.example                 # Redacted template
├── .gitignore
├── requirements.txt
├── schema.sql                   # MySQL DDL (single `scrapy` table)
├── README.md
│
├── main.py                      # Orchestrator — runs all 3 workers concurrently
├── probe_extract.py             # Dev helper: dump a single listing's extracted fields
├── probe_login.py               # Dev helper: walk an account through login + reveal
│
├── setup.sh                     # One-time Linux setup (venv, deps, chromium, xvfb)
├── start.sh                     # Start main.py as a daemon under Xvfb
├── stop.sh                      # Stop main.py + sweep orphan browsers (--clean wipes profiles)
├── status.sh                    # Show liveness, child processes, last log lines, DB summary
│
├── src/
│   ├── scraper.py               # Worker 1 — crawl categories, upsert listings
│   ├── ftp_upload.py            # Worker 2 — migrate image URLs to FTP
│   ├── agent_number_finder.py   # Worker 3 — log in, reveal phone, update DB
│   ├── db.py                    # MySQL backend (pymysql)
│   ├── pg_store.py              # PostgreSQL backend (psycopg)
│   └── store.py                 # Picks backend from STORAGE env var
│
├── api/
│   └── get_properties.php       # Read endpoint (filters, pagination)
│
├── data/                        # Runtime — accounts.csv, proxies.csv (gitignored)
├── logs/                        # Runtime — scrape.log (gitignored)
├── .pw-data-scraper/            # Playwright profile (scraper). Holds Cloudflare cookie. (gitignored)
├── .pw-data-acct-*/             # Per-account Playwright profiles (phone-finder). (gitignored)
└── .venv/                       # Python virtualenv (gitignored)
```

---

## 3. Prerequisites

Linux deployment target:
- **Ubuntu/Debian** (or any distro with `apt`/`dnf`/`yum`)
- **Python 3.11+** (uses `str | None` union syntax)
- **Xvfb** — auto-installed by `setup.sh`; provides the virtual display so
  headed Chrome can run on a server without a real screen
- **Chromium** — auto-installed by `playwright install chromium` in `setup.sh`
- **PostgreSQL** (default) or **MySQL 5.7+ / 8.0+** with `utf8mb4`
- **FTP** account with write access to `FTP_REMOTE_ROOT`

Dev/inspection on Windows or macOS:
- Same Python 3.11+ and Chromium via Playwright; `setup.sh` is bash-only,
  install deps manually (see §4.4).

---

## 4. Setup (Linux)

### 4.1. Clone and run setup

```bash
git clone <repo> property-scraper
cd property-scraper
bash setup.sh
```

`setup.sh` is idempotent — safe to re-run. It will:
- create `.venv/` and install `requirements.txt`
- run `playwright install chromium` + `patchright install chromium`
- install `xvfb` via apt/dnf/yum if missing (uses `sudo`)
- create `data/accounts.csv` and `data/proxies.csv` templates if missing
- copy `.env.example` → `.env` if you don't have one yet

### 4.2. Configure `.env`

```bash
cp .env.example .env   # if setup.sh didn't already
chmod 600 .env         # contains FTP and account passwords
nano .env
```

Key variables (see `.env.example` for the full list):

| Variable | Default | What it does |
|---|---|---|
| `STORAGE` | `postgres` | `postgres` (libpq URL) or `mysql` |
| `DATABASE_URL` | — | Postgres connection string, e.g. `postgresql://user:pw@host/db?sslmode=require` |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | localhost / 3306 / root / "" / property | Only read when `STORAGE=mysql` |
| `FTP_HOST` / `FTP_USER` / `FTP_PASSWORD` | — | FTP credentials |
| `FTP_REMOTE_ROOT` | `/properties` | Folder on FTP where per-listing dirs are created |
| `FTP_PUBLIC_BASE` | `https://apartmentsvietnam.com/properties` | URL prefix stored in DB |
| `FTP_SLEEP_BETWEEN_LISTINGS_SEC` / `FTP_SLEEP_WHEN_IDLE_SEC` | 1 / 60 | Uploader pacing |
| `HEADLESS` | `false` | Scraper headlessness. Patchright keeps headless mostly viable on Cloudflare |
| `MAX_PAGES_PER_CATEGORY` | `0` | `0` = walk pagination until exhausted. Set to `1` for smoke tests |
| `REQUEST_DELAY_MIN_MS` / `REQUEST_DELAY_MAX_MS` | 800 / 2000 | Random delay between requests |
| `PROXY_SERVER` / `PROXY_USER` / `PROXY_PASSWORD` | — | Optional proxy for the scraper |
| `SOURCE_USERNAME` / `SOURCE_PASSWORD` | — | Optional login used by the scraper's phone-reveal helpers |
| `ACCOUNTS_CSV` / `PROXIES_CSV` | `data/accounts.csv` / `data/proxies.csv` | Phone-finder inputs |
| `AGENT_FETCH_DELAY_MIN_SEC` / `AGENT_FETCH_DELAY_MAX_SEC` | 270 / 330 | Sleep between phone reveals (~5 min default per account) |
| `ACCOUNT_COOLDOWN_SEC` | `1800` | After CF block / login failure, cool that (account, proxy) pair |
| `FINDER_HEADLESS` | `false` | Phone-finder headlessness |
| `RUN_SCRAPER` / `RUN_FTP_UPLOAD` / `RUN_FINDER` | `true` | Per-worker switches read by `main.py` |

### 4.3. Phone-finder inputs

`setup.sh` creates header-only templates. Fill them in:

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

Leave both files empty (header-only) to keep the phone-finder idle without
disabling it; or set `RUN_FINDER=false` in `.env`.

### 4.4. Database setup

**Postgres (default).** No manual schema step needed — `pg_store.py` creates
the `scrapy` table, indexes, and `last_seen_at` trigger on first connect.

**MySQL.** Run the DDL once against your DB:

```bash
mysql -h <host> -u <user> -p <database> < schema.sql
```

`schema.sql` creates a single `scrapy` table with:
- `UNIQUE KEY` on `listing_code` (primary dedup)
- `UNIQUE KEY` on `url_source(255)` (belt-and-braces dedup)
- Indexes on `property_type`, `listing_type`, `price`, `acreage`,
  `location_city/province`, `last_seen_at`

### 4.5. Windows / macOS development setup

`setup.sh` is bash-only. For local dev on Windows or macOS:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip
.venv/bin/python -m playwright install chromium
.venv/bin/python -m patchright install chromium
```

Then run `python main.py` directly (no Xvfb, no daemonization). You'll see
a real browser window — useful for solving the first Cloudflare challenge
or debugging selectors with `probe_extract.py` / `probe_login.py`.

---

## 5. Running (Linux server)

### 5.1. First run — Cloudflare bootstrap

The source site sits behind Cloudflare. With Patchright as the browser
backend, headless usually works, but if Cloudflare ever asks for an
interactive challenge you need a real human click once. Two options:

1. **Recommended** — solve the challenge once on a desktop machine, then copy
   `.pw-data-scraper/` to the server.
2. **On the server** — connect to its Xvfb display via VNC and click through.

After the first solve, the clearance cookie persists in `.pw-data-scraper/`
across restarts.

### 5.2. Smoke test before scheduling

```ini
# .env
MAX_PAGES_PER_CATEGORY=1
RUN_FTP_UPLOAD=false        # skip until you've verified the data shape
RUN_FINDER=false
```

```bash
bash start.sh
bash status.sh              # watch progress + last 20 log lines
tail -f logs/scrape.log
```

Then check the DB:

```sql
SELECT COUNT(*) FROM scrapy;                 -- a few dozen rows
SELECT large_image_paths FROM scrapy LIMIT 1;  -- still points at the source site
```

If that's clean, flip the worker switches back to `true`,
`MAX_PAGES_PER_CATEGORY=0`, and restart.

### 5.3. Daemon lifecycle

```bash
bash start.sh         # background, survives SSH logout
bash status.sh        # liveness + children + log tail + DB summary
bash stop.sh          # graceful TERM, then KILL after 8s
bash stop.sh --clean  # also wipe .pw-data-* dirs (forces CF re-solve)
```

`start.sh` uses `nohup setsid xvfb-run -a .venv/bin/python main.py`, writes
the PID to `.scraper.pid`, and refuses to start a second instance.

`stop.sh` first kills the process group, then sweeps any orphaned
`xvfb-run` / chromium / worker processes left behind.

The pipeline is **idempotent** and runs continuously — no cron needed.
Listings are deduped via `listing_code` UNIQUE; re-visits just refresh
`last_seen_at`. If you do want cron-style restarts (e.g. to pick up
config changes daily), schedule `stop.sh && start.sh`.

---

## 6. How it works

`main.py` runs three asyncio tasks concurrently. Each handles its own
retries and errors; a crash in one doesn't take down the others. Ctrl-C
(or `stop.sh`) cancels all three.

1. **Scraper** (`src/scraper.py`)
   - Opens a Patchright/Playwright persistent context at `.pw-data-scraper/`.
   - Walks 10 hard-coded categories (sale + rent across apartment / house /
     villa / land / office), pages `/p1`, `/p2`, … until a page yields no new
     URLs or `MAX_PAGES_PER_CATEGORY` is hit.
   - For each URL not already in the store, opens the detail page, extracts
     the client fields plus the agent's profile URL, then `INSERT … ON
     CONFLICT/DUPLICATE KEY UPDATE` on `listing_code`.

2. **FTP image upload** (`src/ftp_upload.py`)
   - Polls the DB for rows whose `large_image_paths` / `thumbnail_paths`
     still point at the source site.
   - Downloads each image, uploads to `{FTP_REMOTE_ROOT}/<listing_code>/`,
     and rewrites the row with the public `{FTP_PUBLIC_BASE}/<listing_code>/<file>`
     URLs. Per-image failures are logged and skipped — never abort the listing.

3. **Phone finder** (`src/agent_number_finder.py`)
   - Reads `data/accounts.csv` (logins) and `data/proxies.csv` (proxies).
   - For each agent in DB whose `phone_number IS NULL`:
     - pick an active `(account, proxy)` pair (cool ones are skipped for
       `ACCOUNT_COOLDOWN_SEC`),
     - open the agent's `agent_url`,
     - log in once per account (cookies cached per-account in
       `.pw-data-acct-<n>/`),
     - click the red phone-reveal button, read the unmasked number,
     - `UPDATE scrapy SET phone_number = ? WHERE agent_id = ?` — fills every
       listing for that agent in one shot,
     - sleep `AGENT_FETCH_DELAY_MIN_SEC … MAX_SEC` so the per-account quota
       burns slowly.
   - On Cloudflare block / login failure, mark that `(account, proxy)` pair
     cool and rotate to the next.

If FTP variables are unset, the uploader stays idle — original source-site
URLs remain in DB and the other workers still run.

### Batch translation (optional)

`src/translate.py` translates stored listing text in parallel. It has no
built-in supervisor, so use the runner that matches your platform:

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

Use `progress.ps1` (Windows) or `_progress.py` for throughput and ETA. The
runner auto-restarts a worker 30 seconds after it exits.

---

## 7. FTP layout on the remote

```
/<FTP_REMOTE_ROOT>/
  └── <listing_code>/
        ├── img1.jpg       ← large_image_paths[0]
        ├── img2.jpg       ← large_image_paths[1]
        ├── ...
        ├── thumb1.jpg     ← thumbnail_paths[0]
        └── thumb2.jpg     ← thumbnail_paths[1]
```

Public URLs stored in DB follow the pattern:
```
{FTP_PUBLIC_BASE}/{listing_code}/{filename}
```

With the default config this is e.g.
`https://apartmentsvietnam.com/properties/12345678/img1.jpg`.

> The mapping between `FTP_REMOTE_ROOT` on the FTP server and the path served
> by your web server **must match**. The default assumes the FTP user's home
> is the web document root and `/properties` is served at
> `https://apartmentsvietnam.com/properties`.

---

## 8. Read endpoint (`api/get_properties.php`)

PHP/PDO endpoint that serves filtered, paginated JSON. Currently configured
for MySQL (`mysql:host=…`); for Postgres-only deployments, port the PDO DSN
to `pgsql:…` or expose a separate API service.

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

Reads the same `DB_*` env vars as the MySQL backend. Drop into any
PHP-capable web root.

---

## 9. Troubleshooting

**`status.sh` says `(DB query failed — is MySQL up?)`** — the inner Python
block exited non-zero. Most common causes: wrong `DB_NAME` in `.env`, MySQL
service down, or `STORAGE=postgres` (status.sh queries MySQL directly).
Run the inner query without `2>/dev/null` to see the real error.

**Cloudflare interstitial in the logs** — first run failed to solve the
challenge. Stop, run with `HEADLESS=false` on a desktop or via VNC into the
server's Xvfb, solve once, copy `.pw-data-scraper/` back. `bash stop.sh
--clean` wipes the profiles so you can restart fresh.

**`FTP uploader disabled` warning** — one of `FTP_HOST` / `FTP_USER` /
`FTP_PASSWORD` / `FTP_PUBLIC_BASE` is unset. The other workers keep going.

**`FTP upload failed … 550`** — the remote folder couldn't be created.
Check the FTP user has write permission on `FTP_REMOTE_ROOT`.

**Images return 404 in the browser** — `FTP_PUBLIC_BASE` doesn't actually
map to `FTP_REMOTE_ROOT` on your web server. Verify by uploading a test file
manually and opening the corresponding URL.

**Slow runs / repeated timeouts** — increase `REQUEST_DELAY_MIN_MS` /
`REQUEST_DELAY_MAX_MS`. The source site rate-limits aggressively under load.

**Phone-finder isn't revealing anything** — check `data/accounts.csv` is
populated and each account can log in manually. Each account has a daily
reveal quota; spread load with more rows and a longer
`AGENT_FETCH_DELAY_*_SEC`.

**Need to re-scrape a listing** — `DELETE FROM scrapy WHERE listing_code = '<id>';`
then let the scraper find it again on the next pass.

**Debug a specific listing's extraction** — `python probe_extract.py <url>`.
For login/reveal flow, `python probe_login.py`.

**Start refuses with "Already running"** — stale PID file. `bash stop.sh`
clears it; if the process is actually gone, `bash status.sh` removes the
stale PID file too.

---

## 10. Files reference

| File | Purpose |
|---|---|
| `main.py` | Orchestrator. Runs scraper + ftp_upload + agent_number_finder as concurrent asyncio tasks |
| `src/scraper.py` | Crawl + upsert worker |
| `src/ftp_upload.py` | Image migration worker — downloads from the source site, uploads to FTP, rewrites URLs |
| `src/agent_number_finder.py` | Login + phone-reveal worker (multi-account, multi-proxy) |
| `src/db.py` | MySQL backend |
| `src/pg_store.py` | PostgreSQL backend (default) |
| `src/store.py` | Picks backend from `STORAGE=postgres`/`mysql` |
| `schema.sql` | MySQL DDL (Postgres equivalent lives inside `pg_store.py`) |
| `api/get_properties.php` | Read endpoint with filters + pagination (MySQL) |
| `setup.sh` | One-time Linux setup |
| `start.sh` | Daemonize main.py under Xvfb |
| `stop.sh` | Graceful stop + orphan sweep (`--clean` wipes browser profiles) |
| `status.sh` | Liveness + child processes + log tail + DB summary |
| `probe_extract.py` | Dev — extract one listing and print the row |
| `probe_login.py` | Dev — walk one account through login + phone reveal |
| `src/translate.py` | Batch translation worker for stored listing text |
| `run_workers.sh` / `run_workers.ps1` | Start/stop supervised translation worker pools |
| `_progress.py` / `progress.ps1` | Translation progress, throughput, and ETA |
| `requirements.txt` | `playwright`, `patchright`, `python-dotenv`, `pymysql`, `psycopg[binary]`, `certifi`, `Pillow`, `deep-translator` |
| `.env` / `.env.example` | Configuration |
| `.gitignore` | Keeps `.env`, credential CSVs, virtualenvs, Playwright profiles, logs, and debug output out of git |

---

## 11. Security notes

- **Never commit `.env`** — contains DB, FTP, and account passwords.
  `.gitignore` already excludes it. `chmod 600 .env` after editing.
- **Never commit `data/accounts.csv` or `data/proxies.csv`** — they contain
  live credentials. `.gitignore` excludes `data/*.csv`; keep it that way and
  use `git rm --cached` if a credential file ever slips in.
- The FTP password is sent over plain FTP (port 21). If the host supports
  FTPS, swap `FTP` for `FTP_TLS` in `src/ftp_upload.py`.
- The PHP endpoint uses prepared statements (PDO `:named` params) — no SQL
  injection surface.
- Rotate any password that ends up in a shared `.env` or screenshot.
