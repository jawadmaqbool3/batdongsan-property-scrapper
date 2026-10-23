import json
import os
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor


class Database:
    def __init__(self):
        self.conn = pymysql.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASSWORD", ""),
            database=os.getenv("DB_NAME", "property"),
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )

    def init_schema(self):
        sql_path = Path(__file__).resolve().parent.parent / "schema.sql"
        sql = sql_path.read_text(encoding="utf-8")
        with self.conn.cursor() as cur:
            for stmt in filter(None, (s.strip() for s in sql.split(";"))):
                cur.execute(stmt)
            # Idempotent migrations. MySQL has no ADD COLUMN IF NOT EXISTS —
            # we catch error 1060 (ER_DUP_FIELDNAME) instead.
            for col_ddl in (
                "phone_number VARCHAR(64) NULL",
                "agent_id     VARCHAR(32) NULL",
                "agent_url    VARCHAR(512) NULL",
            ):
                try:
                    cur.execute(f"ALTER TABLE scrapy ADD COLUMN {col_ddl}")
                except pymysql.err.OperationalError as e:
                    if e.args and e.args[0] == 1060:
                        continue
                    raise

    def has_listing(self, listing_code: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM scrapy WHERE listing_code = %s LIMIT 1",
                (listing_code,),
            )
            return cur.fetchone() is not None

    def upsert_property(self, data: dict):
        payload = {
            **data,
            "thumbnail_paths":   json.dumps(data.get("thumbnail_paths") or [], ensure_ascii=False),
            "large_image_paths": json.dumps(data.get("large_image_paths") or [], ensure_ascii=False),
        }
        sql = """
        INSERT INTO scrapy (
            url_source, listing_code, title, slug, description, footer_disclaimer,
            price, acreage, bedrooms, bathrooms,
            property_type, legal_type, news_type, interior,
            date_posted, posted_by, hidden_number, phone_number,
            agent_id, agent_url,
            location_city, location_province, listing_type,
            thumbnail_paths, large_image_paths,
            price_raw, direction, address, ward, street,
            latitude, longitude, expires_at_raw, category
        ) VALUES (
            %(url_source)s, %(listing_code)s, %(title)s, %(slug)s, %(description)s, %(footer_disclaimer)s,
            %(price)s, %(acreage)s, %(bedrooms)s, %(bathrooms)s,
            %(property_type)s, %(legal_type)s, %(news_type)s, %(interior)s,
            %(date_posted)s, %(posted_by)s, %(hidden_number)s, %(phone_number)s,
            %(agent_id)s, %(agent_url)s,
            %(location_city)s, %(location_province)s, %(listing_type)s,
            %(thumbnail_paths)s, %(large_image_paths)s,
            %(price_raw)s, %(direction)s, %(address)s, %(ward)s, %(street)s,
            %(latitude)s, %(longitude)s, %(expires_at_raw)s, %(category)s
        )
        ON DUPLICATE KEY UPDATE
            url_source = VALUES(url_source),
            title = VALUES(title),
            slug = VALUES(slug),
            description = VALUES(description),
            footer_disclaimer = VALUES(footer_disclaimer),
            price = VALUES(price),
            acreage = VALUES(acreage),
            bedrooms = VALUES(bedrooms),
            bathrooms = VALUES(bathrooms),
            property_type = VALUES(property_type),
            legal_type = VALUES(legal_type),
            news_type = VALUES(news_type),
            interior = VALUES(interior),
            date_posted = VALUES(date_posted),
            posted_by = VALUES(posted_by),
            hidden_number = VALUES(hidden_number),
            phone_number = COALESCE(VALUES(phone_number), phone_number),
            agent_id = VALUES(agent_id),
            agent_url = VALUES(agent_url),
            location_city = VALUES(location_city),
            location_province = VALUES(location_province),
            listing_type = VALUES(listing_type),
            thumbnail_paths = IF(
                thumbnail_paths IS NULL
                OR JSON_LENGTH(thumbnail_paths) = 0
                OR CAST(thumbnail_paths AS CHAR) LIKE '%%batdongsan.com.vn%%',
                VALUES(thumbnail_paths), thumbnail_paths),
            large_image_paths = IF(
                large_image_paths IS NULL
                OR JSON_LENGTH(large_image_paths) = 0
                OR CAST(large_image_paths AS CHAR) LIKE '%%batdongsan.com.vn%%',
                VALUES(large_image_paths), large_image_paths),
            price_raw = VALUES(price_raw),
            direction = VALUES(direction),
            address = VALUES(address),
            ward = VALUES(ward),
            street = VALUES(street),
            latitude = VALUES(latitude),
            longitude = VALUES(longitude),
            expires_at_raw = VALUES(expires_at_raw),
            category = VALUES(category),
            last_seen_at = CURRENT_TIMESTAMP
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, payload)

    # ─── ftp_upload ────────────────────────────────────────────────────
    def pending_listings_for_ftp(self, limit: int = 100) -> list[tuple]:
        sql = (
            "SELECT listing_code, large_image_paths, thumbnail_paths "
            "FROM scrapy "
            "WHERE (CAST(large_image_paths AS CHAR) LIKE %s "
            "   OR CAST(thumbnail_paths   AS CHAR) LIKE %s) "
            "ORDER BY last_seen_at DESC LIMIT %s"
        )
        needle = "%batdongsan.com.vn%"
        with self.conn.cursor() as cur:
            cur.execute(sql, (needle, needle, limit))
            return [(r["listing_code"], r["large_image_paths"], r["thumbnail_paths"])
                    for r in cur.fetchall()]

    def update_image_urls(self, listing_code: str,
                          large: list[str], thumb: list[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE scrapy SET large_image_paths = %s, "
                "thumbnail_paths = %s WHERE listing_code = %s",
                (json.dumps(large, ensure_ascii=False),
                 json.dumps(thumb, ensure_ascii=False),
                 listing_code),
            )

    # ─── agent_number_finder ───────────────────────────────────────────
    def backfill_phones_from_known_agents(self) -> int:
        sql = """
            UPDATE scrapy p
            JOIN (
                SELECT agent_id, MIN(phone_number) AS phone
                FROM scrapy
                WHERE phone_number IS NOT NULL AND phone_number <> ''
                  AND agent_id IS NOT NULL AND agent_id <> ''
                GROUP BY agent_id
            ) k ON p.agent_id = k.agent_id
            SET p.phone_number = k.phone
            WHERE p.phone_number IS NULL OR p.phone_number = ''
        """
        with self.conn.cursor() as cur:
            cur.execute(sql)
            return cur.rowcount or 0

    def pending_agents(self, limit: int = 200) -> list[tuple[str, str, str]]:
        sql = (
            "SELECT agent_id, MAX(agent_url) AS agent_url, MAX(posted_by) AS posted_by "
            "FROM scrapy "
            "WHERE agent_id IS NOT NULL AND agent_id <> '' "
            "  AND agent_url IS NOT NULL "
            "GROUP BY agent_id "
            "HAVING SUM(CASE WHEN phone_number IS NOT NULL AND phone_number <> '' "
            "             THEN 1 ELSE 0 END) = 0 "
            "ORDER BY RAND() "
            "LIMIT %s"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [(str(r["agent_id"]), r["agent_url"] or "", r["posted_by"])
                    for r in cur.fetchall()]

    def set_agent_phone(self, agent_id: str, phone: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE scrapy SET phone_number = %s "
                "WHERE agent_id = %s AND (phone_number IS NULL OR phone_number = '')",
                (phone, agent_id),
            )
            return cur.rowcount or 0

    def close(self):
        self.conn.close()
