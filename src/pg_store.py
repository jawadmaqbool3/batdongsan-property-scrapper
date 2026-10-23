"""PostgreSQL backend — mirrors src/db.py's Database interface.

Activated by `STORAGE=postgres` in .env. Connects via DATABASE_URL (libpq URL
including any sslmode / channel_binding params, e.g. a Neon pooler URL).

All four call sites in the scraper / FTP uploader / phone finder go through
this class, so the workers stay storage-agnostic.
"""
import json
import logging
import os

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger("pg_store")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scrapy (
  id                 BIGSERIAL    PRIMARY KEY,
  url_source         VARCHAR(512) NOT NULL,
  listing_code       VARCHAR(32)  NOT NULL UNIQUE,
  title              VARCHAR(512),
  slug               VARCHAR(255),
  description        TEXT,
  footer_disclaimer  TEXT,
  price              NUMERIC(18,2),
  acreage            NUMERIC(10,2),
  bedrooms           INTEGER,
  bathrooms          INTEGER,
  property_type      VARCHAR(128),
  legal_type         VARCHAR(128),
  news_type          VARCHAR(128),
  interior           VARCHAR(128),
  date_posted        VARCHAR(64),
  posted_by          VARCHAR(128),
  hidden_number      VARCHAR(64),
  phone_number       VARCHAR(64),
  agent_id           VARCHAR(32),
  agent_url          VARCHAR(512),
  location_city      VARCHAR(128),
  location_province  VARCHAR(128),
  listing_type       VARCHAR(32),
  thumbnail_paths    JSONB,
  large_image_paths  JSONB,
  created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  price_raw          VARCHAR(64),
  direction          VARCHAR(64),
  address            TEXT,
  ward               VARCHAR(128),
  street             VARCHAR(256),
  latitude           NUMERIC(10,7),
  longitude          NUMERIC(10,7),
  expires_at_raw     VARCHAR(64),
  category           VARCHAR(64),
  last_seen_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_url_source UNIQUE (url_source)
);

CREATE INDEX IF NOT EXISTS idx_property_type ON scrapy (property_type);
CREATE INDEX IF NOT EXISTS idx_listing_type  ON scrapy (listing_type);
CREATE INDEX IF NOT EXISTS idx_price         ON scrapy (price);
CREATE INDEX IF NOT EXISTS idx_acreage       ON scrapy (acreage);
CREATE INDEX IF NOT EXISTS idx_location      ON scrapy (location_city, location_province);
CREATE INDEX IF NOT EXISTS idx_last_seen     ON scrapy (last_seen_at);

-- Postgres has no `ON UPDATE CURRENT_TIMESTAMP` column option; replicate it
-- with a BEFORE UPDATE trigger so UPDATEs from ftp_upload / agent_number_finder
-- bump last_seen_at the same way MySQL did.
CREATE OR REPLACE FUNCTION set_last_seen_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.last_seen_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_set_last_seen_at ON scrapy;
CREATE TRIGGER trg_set_last_seen_at
BEFORE UPDATE ON scrapy
FOR EACH ROW EXECUTE FUNCTION set_last_seen_at();
"""


def _conninfo() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "STORAGE=postgres but DATABASE_URL is empty. Set DATABASE_URL in "
            ".env to a libpq URL (e.g. a Neon connection string)."
        )
    return url


class PostgresStore:
    def __init__(self):
        self.conn = psycopg.connect(_conninfo(), autocommit=True)

    # ─── schema ────────────────────────────────────────────────────────
    def init_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)

    # ─── scraper ───────────────────────────────────────────────────────
    def has_listing(self, listing_code: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM scrapy WHERE listing_code = %s LIMIT 1",
                (listing_code,),
            )
            return cur.fetchone() is not None

    def upsert_property(self, data: dict) -> None:
        payload = {
            **{c: data.get(c) for c in _COLUMNS},
            "thumbnail_paths":   Jsonb(data.get("thumbnail_paths") or []),
            "large_image_paths": Jsonb(data.get("large_image_paths") or []),
        }
        # listing_code is the conflict target (no point re-assigning it).
        # phone_number gets a COALESCE so a re-scrape never wipes a number the
        # finder already revealed — keep it out of the generic SET loop.
        _NO_OVERWRITE = {"listing_code", "phone_number"}
        sql = f"""
        INSERT INTO scrapy ({", ".join(_COLUMNS)})
        VALUES ({", ".join(f"%({c})s" for c in _COLUMNS)})
        ON CONFLICT (listing_code) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c not in _NO_OVERWRITE)},
            phone_number = COALESCE(EXCLUDED.phone_number, scrapy.phone_number),
            last_seen_at = NOW()
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, payload)

    # ─── ftp_upload ────────────────────────────────────────────────────
    def pending_listings_for_ftp(self, limit: int = 100) -> list[tuple]:
        """Rows whose image URLs still point at batdongsan."""
        sql = (
            "SELECT listing_code, large_image_paths, thumbnail_paths "
            "FROM scrapy "
            "WHERE large_image_paths::text LIKE %s "
            "   OR thumbnail_paths::text   LIKE %s "
            "ORDER BY last_seen_at DESC LIMIT %s"
        )
        needle = "%batdongsan.com.vn%"
        with self.conn.cursor() as cur:
            cur.execute(sql, (needle, needle, limit))
            return cur.fetchall()

    def update_image_urls(self, listing_code: str,
                          large: list[str], thumb: list[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE scrapy SET large_image_paths = %s, "
                "thumbnail_paths = %s WHERE listing_code = %s",
                (Jsonb(large), Jsonb(thumb), listing_code),
            )

    # ─── agent_number_finder ───────────────────────────────────────────
    def backfill_phones_from_known_agents(self) -> int:
        sql = """
            UPDATE scrapy p
            SET phone_number = k.phone
            FROM (
                SELECT agent_id, MIN(phone_number) AS phone
                FROM scrapy
                WHERE phone_number IS NOT NULL AND phone_number <> ''
                  AND agent_id IS NOT NULL AND agent_id <> ''
                GROUP BY agent_id
            ) k
            WHERE p.agent_id = k.agent_id
              AND (p.phone_number IS NULL OR p.phone_number = '')
        """
        with self.conn.cursor() as cur:
            cur.execute(sql)
            return cur.rowcount or 0

    def pending_agents(self, limit: int = 200) -> list[tuple[str, str, str]]:
        sql = """
            SELECT agent_id,
                   MAX(agent_url) AS agent_url,
                   MAX(posted_by) AS posted_by
            FROM scrapy
            WHERE agent_id IS NOT NULL AND agent_id <> ''
              AND agent_url IS NOT NULL
            GROUP BY agent_id
            HAVING SUM(CASE WHEN phone_number IS NOT NULL AND phone_number <> ''
                            THEN 1 ELSE 0 END) = 0
            ORDER BY random()
            LIMIT %s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [(str(a), u or "", n) for (a, u, n) in cur.fetchall()]

    def set_agent_phone(self, agent_id: str, phone: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE scrapy SET phone_number = %s "
                "WHERE agent_id = %s AND (phone_number IS NULL OR phone_number = '')",
                (phone, agent_id),
            )
            return cur.rowcount or 0

    # ─── lifecycle ─────────────────────────────────────────────────────
    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# Column order used for INSERT — kept in one place so the SQL string and the
# payload dict can't drift apart.
_COLUMNS = (
    "url_source", "listing_code", "title", "slug", "description",
    "footer_disclaimer", "price", "acreage", "bedrooms", "bathrooms",
    "property_type", "legal_type", "news_type", "interior",
    "date_posted", "posted_by", "hidden_number", "phone_number",
    "agent_id", "agent_url",
    "location_city", "location_province", "listing_type",
    "thumbnail_paths", "large_image_paths",
    "price_raw", "direction", "address", "ward", "street",
    "latitude", "longitude", "expires_at_raw", "category",
)
