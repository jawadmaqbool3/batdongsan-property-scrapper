"""Print translate.py progress. Invoked by progress.ps1."""
import os
import pymysql
from dotenv import load_dotenv

load_dotenv()
c = pymysql.connect(
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME"),
    charset="utf8mb4",
    connect_timeout=10,
)
cur = c.cursor()
cur.execute("SELECT COUNT(*), SUM(translated_at IS NOT NULL) FROM scrapy")
total, done = cur.fetchone()
total = int(total or 0)
done = int(done or 0)
pending = total - done
pct = (done * 100.0 / total) if total else 0.0

cur.execute("SELECT COUNT(*) FROM scrapy WHERE translated_at >= NOW() - INTERVAL 1 MINUTE")
r1 = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM scrapy WHERE translated_at >= NOW() - INTERVAL 5 MINUTE")
r5 = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM scrapy WHERE translated_at >= NOW() - INTERVAL 15 MINUTE")
r15 = cur.fetchone()[0]

rate1 = r1 / 60.0
rate5 = r5 / 300.0
rate15 = r15 / 900.0
# Use the 5-min window for ETA — smooths bursts, still reflects current speed.
eta_sec = (pending / rate5) if rate5 > 0 else None


def fmt_eta(s):
    if s is None:
        return "n/a (no recent activity)"
    h, rem = divmod(int(s), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m"


print(f"done    {done:>7,} / {total:>7,}  ({pct:5.2f}%)")
print(f"pending {pending:>7,}")
print(f"rate    1m={rate1:.2f}/s   5m={rate5:.2f}/s   15m={rate15:.2f}/s")
print(f"eta     {fmt_eta(eta_sec)}")
