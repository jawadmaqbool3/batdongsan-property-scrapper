"""
Translate Vietnamese fields in the `scrapy` table to English IN PLACE.

Safety rails:
  * --dry-run  : show before/after, write nothing.
  * --limit N  : only process N rows (great for sanity checks).
  * Resumable  : adds a `translated_at` marker column, skips rows already done.
  * Idempotent : rows that already look English (no VN diacritics) are skipped per-field.
  * Cache      : repeated short strings (property_type, city names) are translated once.

Usage:
    python src/translate.py --dry-run --limit 3
    python src/translate.py --limit 50
    python src/translate.py                    # everything

Strongly recommended: take a mysqldump backup before running without --dry-run.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Optional

import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv
from deep_translator import GoogleTranslator

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

TEXT_FIELDS = (
    "title", "description", "footer_disclaimer",
    "property_type", "legal_type", "news_type", "interior",
    "direction", "category",
    "location_city", "location_province", "ward", "street", "address",
    "posted_by",
)

# Google's free endpoint hard-caps a single call near 5000 chars; stay under.
MAX_CHUNK = 4500

# Vietnamese-specific letters. If a string has none, assume it's already English/Latin.
VN_CHARS = re.compile(
    r"[ăâđêôơưĂÂĐÊÔƠƯáàảãạắằẳẵặấầẩẫậéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]"
)


def looks_vietnamese(s: Optional[str]) -> bool:
    return bool(s) and bool(VN_CHARS.search(s))


def split_for_api(text: str) -> list[str]:
    """Split long text on paragraph/sentence boundaries so each chunk fits the API cap."""
    if len(text) <= MAX_CHUNK:
        return [text]
    chunks, buf = [], ""
    # Prefer paragraph splits; fall back to single lines if a paragraph is huge.
    for para in text.split("\n"):
        if len(buf) + len(para) + 1 > MAX_CHUNK and buf:
            chunks.append(buf)
            buf = ""
        if len(para) > MAX_CHUNK:
            # Last resort: hard-slice an oversized line.
            for i in range(0, len(para), MAX_CHUNK):
                chunks.append(para[i:i + MAX_CHUNK])
            continue
        buf = (buf + "\n" + para) if buf else para
    if buf:
        chunks.append(buf)
    return chunks


class Translator:
    def __init__(self):
        self._engine = GoogleTranslator(source="vi", target="en")
        self._cache: dict[str, str] = {}

    def translate(self, text: Optional[str]) -> Optional[str]:
        if text is None:
            return None
        text = text.strip()
        if not text or not looks_vietnamese(text):
            return text  # already English or empty — leave alone

        if text in self._cache:
            return self._cache[text]

        pieces = split_for_api(text)
        out_pieces = []
        for piece in pieces:
            out_pieces.append(self._call_with_retry(piece))
        out = "\n".join(out_pieces).strip()
        # Only cache short values; descriptions are mostly unique.
        if len(text) <= 256:
            self._cache[text] = out
        return out

    def _call_with_retry(self, piece: str, attempts: int = 4) -> str:
        delay = 1.5
        for n in range(attempts):
            try:
                return self._engine.translate(piece) or piece
            except Exception as e:
                if n == attempts - 1:
                    raise
                print(f"  ! translator error ({e!r}); retry in {delay:.1f}s", flush=True)
                time.sleep(delay)
                delay *= 2
        return piece


def connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=15,
    )


def ensure_marker_column(conn) -> None:
    """Add `translated_at` once so we can resume mid-run."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "ALTER TABLE scrapy ADD COLUMN translated_at TIMESTAMP NULL, "
                "ADD INDEX idx_translated_at (translated_at)"
            )
        except pymysql.err.OperationalError as e:
            if e.args and e.args[0] == 1060:  # ER_DUP_FIELDNAME
                return
            raise


def fetch_pending(conn, limit: int, worker: int = 0, workers: int = 1) -> list[dict]:
    cols = ", ".join(("id", "listing_code", *TEXT_FIELDS))
    sql = f"SELECT {cols} FROM scrapy WHERE translated_at IS NULL "
    params: list = []
    if workers > 1:
        sql += "AND id %% %s = %s "
        params.extend([workers, worker])
    sql += "ORDER BY id LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def translate_row(t: Translator, row: dict) -> dict[str, str]:
    """Return dict of {field: new_value} only for fields that actually changed."""
    changes: dict[str, str] = {}
    for f in TEXT_FIELDS:
        original = row.get(f)
        if not looks_vietnamese(original):
            continue
        new = t.translate(original)
        if new and new != original:
            changes[f] = new
    return changes


def apply_changes(conn, row_id: int, changes: dict[str, str], mark: bool) -> None:
    sets = [f"{f} = %s" for f in changes]
    params: list = list(changes.values())
    if mark:
        sets.append("translated_at = CURRENT_TIMESTAMP")
    if not sets:
        return
    sql = f"UPDATE scrapy SET {', '.join(sets)} WHERE id = %s"
    params.append(row_id)
    with conn.cursor() as cur:
        cur.execute(sql, params)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10_000_000,
                    help="Max rows to process this run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print before/after, write nothing.")
    ap.add_argument("--workers", type=int, default=1,
                    help="Total worker count for parallel runs across processes.")
    ap.add_argument("--worker", type=int, default=0,
                    help="This worker's index (0..workers-1). Each owns rows where id %% workers = worker.")
    args = ap.parse_args()

    if not (0 <= args.worker < args.workers):
        ap.error(f"--worker must be in [0, {args.workers}); got {args.worker}")

    tag = f"[w{args.worker}/{args.workers}] " if args.workers > 1 else ""

    conn = connect()
    if not args.dry_run:
        ensure_marker_column(conn)
        rows = fetch_pending(conn, args.limit, args.worker, args.workers)
    else:
        # Dry-run shouldn't mutate schema. If the marker column already exists, use
        # the same filter as a real run; otherwise just pick the first N rows.
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM scrapy LIKE 'translated_at'")
            has_marker = cur.fetchone() is not None
        if has_marker:
            rows = fetch_pending(conn, args.limit, args.worker, args.workers)
        else:
            with conn.cursor() as cur:
                cols = ", ".join(("id", "listing_code", *TEXT_FIELDS))
                sql = f"SELECT {cols} FROM scrapy "
                params: list = []
                if args.workers > 1:
                    sql += "WHERE id %% %s = %s "
                    params.extend([args.workers, args.worker])
                sql += "ORDER BY id LIMIT %s"
                params.append(args.limit)
                cur.execute(sql, params)
                rows = cur.fetchall()

    print(f"{tag}Fetched {len(rows)} row(s) to translate.")
    if not rows:
        return

    t = Translator()
    done = 0
    t0 = time.time()
    for row in rows:
        changes = translate_row(t, row)
        if args.dry_run:
            print(f"\n--- id={row['id']} listing_code={row['listing_code']} ---")
            if not changes:
                print("  (nothing to translate — already English or empty)")
            for f, new in changes.items():
                old = row[f] or ""
                preview_old = (old[:140] + "…") if len(old) > 140 else old
                preview_new = (new[:140] + "…") if len(new) > 140 else new
                print(f"  {f}:")
                print(f"    VI: {preview_old}")
                print(f"    EN: {preview_new}")
        else:
            apply_changes(conn, row["id"], changes, mark=True)
            done += 1
            if done % 25 == 0:
                rate = done / (time.time() - t0)
                print(f"  {tag}…{done}/{len(rows)} rows done ({rate:.1f}/s, cache={len(t._cache)})",
                      flush=True)

    if not args.dry_run:
        print(f"\n{tag}Done. Translated {done} row(s) in {time.time() - t0:.1f}s. "
              f"Cache had {len(t._cache)} short-string entries.")

    conn.close()


if __name__ == "__main__":
    main()
