"""Single place to pick the storage backend so all three workers stay in sync.

Read by scraper.py, ftp_upload.py and agent_number_finder.py.

  STORAGE=postgres  (default)  → PostgresStore (DATABASE_URL)
  STORAGE=mysql                → Database (DB_HOST / DB_PORT / DB_USER / …)
"""
import os


def make_store():
    backend = os.getenv("STORAGE", "postgres").lower()
    if backend in ("postgres", "postgresql", "pg", "supabase", "neon"):
        from pg_store import PostgresStore
        return PostgresStore()
    if backend in ("db", "mysql"):
        from db import Database
        return Database()
    raise RuntimeError(
        f"Unknown STORAGE={backend!r}. Set STORAGE=postgres (default) or "
        f"STORAGE=mysql in .env."
    )
