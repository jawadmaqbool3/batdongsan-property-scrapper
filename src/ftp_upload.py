"""FTP image migrator — class + background loop in one file.

Continuously finds rows in the DB whose images still point at batdongsan's
CDN, downloads each image, uploads it to your FTP under
`<FTP_REMOTE_ROOT>/<listing_code>/`, and rewrites the row's `large_image_paths`
+ `thumbnail_paths` to the public `<FTP_PUBLIC_BASE>/<listing_code>/...` URLs.

Safe to interrupt and resume — only processes rows whose URLs still point at
batdongsan, so re-runs only touch unmigrated rows.

Layout on remote:
    <FTP_REMOTE_ROOT>/<listing_code>/img1.jpg     (large gallery, in order)
    <FTP_REMOTE_ROOT>/<listing_code>/thumb1.jpg   (thumbnails, in order)

Public URLs stored in DB:
    <FTP_PUBLIC_BASE>/<listing_code>/img1.jpg
    <FTP_PUBLIC_BASE>/<listing_code>/thumb1.jpg

Run standalone:
    python src/ftp_upload.py
"""
import asyncio
import io
import json
import logging
import os
import random
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from ftplib import FTP, error_perm
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlparse

from PIL import Image, ImageEnhance, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv()

from store import make_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ftp")


# macOS Python builds from python.org don't ship trusted CAs, so urllib HTTPS
# downloads fail with CERTIFICATE_VERIFY_FAILED. Prefer the certifi bundle if
# it's installed; fall back to the OS defaults otherwise.
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

# batdongsan's CDN sometimes 403s default urllib UA.
_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://batdongsan.com.vn/",
}

_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(?:$|[?#])", re.IGNORECASE)

SLEEP_BETWEEN_LISTINGS_SEC = float(os.getenv("FTP_SLEEP_BETWEEN_LISTINGS_SEC", "1.0"))
SLEEP_WHEN_IDLE_SEC = float(os.getenv("FTP_SLEEP_WHEN_IDLE_SEC", "60"))

# Per-image randomisation so every uploaded copy has a slightly different
# pixel fingerprint than batdongsan's original. Toggle off with FTP_TRANSFORM=false.
TRANSFORM_ENABLED = os.getenv("FTP_TRANSFORM", "true").lower() in ("1", "true", "yes", "on")
JPEG_QUALITY = int(os.getenv("FTP_JPEG_QUALITY", "90"))


def _transform_image(data: bytes, ext: str) -> bytes:
    """Apply small random tweaks so the uploaded image differs from the source.

    Returns the original bytes unchanged for animated GIFs or anything PIL can't
    decode — better to ship the original than nothing.
    """
    if not TRANSFORM_ENABLED:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        # Animated GIFs: don't try to re-encode, we'd lose the animation.
        if getattr(img, "is_animated", False):
            return data
        fmt = (img.format or "").upper()
        img.load()

        # 1-3 px random crop from each edge.
        w, h = img.size
        l, t = random.randint(1, 3), random.randint(1, 3)
        r, b = random.randint(1, 3), random.randint(1, 3)
        if w - l - r > 10 and h - t - b > 10:
            img = img.crop((l, t, w - r, h - b))

        # +1-3% resize on the cropped image.
        scale = 1.0 + random.uniform(0.01, 0.03)
        new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        img = img.resize(new_size, Image.LANCZOS)

        # Tonal tweaks (small, so listings still look natural).
        img = ImageEnhance.Brightness(img).enhance(random.uniform(0.95, 1.05))
        img = ImageEnhance.Contrast(img).enhance(random.uniform(0.95, 1.05))
        img = ImageEnhance.Sharpness(img).enhance(random.uniform(1.2, 1.5))
        img = ImageEnhance.Color(img).enhance(random.uniform(0.95, 1.10))
        img = img.filter(ImageFilter.DETAIL)

        out = io.BytesIO()
        ext_l = ext.lower().lstrip(".")
        if ext_l in ("jpg", "jpeg"):
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        elif ext_l == "png":
            img.save(out, format="PNG", optimize=True)
        elif ext_l == "webp":
            img.save(out, format="WEBP", quality=JPEG_QUALITY)
        else:
            # Unknown ext — fall back to the input format if PIL knew it,
            # else JPEG.
            save_fmt = fmt if fmt in ("JPEG", "PNG", "WEBP") else "JPEG"
            if save_fmt == "JPEG" and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out, format=save_fmt, quality=JPEG_QUALITY)
        return out.getvalue()
    except Exception as e:
        log.warning("image transform failed (%s) — uploading original", e)
        return data


# ───────────────────────────────────────────────────────  FTP class

class FtpUploader:
    """Connect once, upload many listings. Re-opens on transient drops."""

    def __init__(self):
        self.host = os.getenv("FTP_HOST", "").strip()
        self.user = os.getenv("FTP_USER", "").strip()
        self.password = os.getenv("FTP_PASSWORD", "")
        self.remote_root = "/" + os.getenv("FTP_REMOTE_ROOT", "/properties").strip("/")
        self.public_base = os.getenv("FTP_PUBLIC_BASE", "").rstrip("/")
        self.timeout = int(os.getenv("FTP_TIMEOUT", "30"))
        # Concurrent image downloads per listing. FTP itself stays single-
        # connection (uploads are serial on the persistent session).
        self.download_workers = max(1, int(os.getenv("FTP_DOWNLOAD_WORKERS", "8")))
        self.enabled = bool(self.host and self.user and self.password and self.public_base)
        if not self.enabled:
            log.warning(
                "FTP disabled — set FTP_HOST/FTP_USER/FTP_PASSWORD/FTP_PUBLIC_BASE in .env."
            )
        self._ftp: FTP | None = None
        self._created_dirs: set[str] = set()

    def _connect(self) -> FTP:
        if self._ftp is not None:
            return self._ftp
        log.info("FTP connect %s as %s", self.host, self.user)
        ftp = FTP(self.host, timeout=self.timeout)
        ftp.login(self.user, self.password)
        ftp.set_pasv(True)
        self._ftp = ftp
        self._created_dirs.clear()
        return ftp

    def _close_quietly(self):
        if self._ftp is not None:
            try: self._ftp.close()
            except Exception: pass
            self._ftp = None

    def close(self):
        if self._ftp is not None:
            try: self._ftp.quit()
            except Exception: self._close_quietly()
            self._ftp = None

    def _ensure_dir(self, ftp: FTP, path: str):
        if path in self._created_dirs:
            return
        cwd = ""
        for part in PurePosixPath(path).parts:
            if not part or part == "/":
                continue
            cwd = f"{cwd}/{part}" if cwd else f"/{part}"
            try:
                ftp.mkd(cwd)
            except error_perm:
                pass  # 550: already exists, or permission — continue.
        self._created_dirs.add(path)

    @staticmethod
    def _ext_from_url(url: str) -> str:
        path = urlparse(url).path
        m = _EXT_RE.search(path)
        if not m:
            return ".jpg"
        ext = m.group(1).lower()
        return f".{'jpg' if ext == 'jpeg' else ext}"

    @staticmethod
    def _dedupe(urls: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _download(self, url: str) -> bytes | None:
        try:
            req = urllib.request.Request(url, headers=_DOWNLOAD_HEADERS)
            with urllib.request.urlopen(req, timeout=self.timeout, context=_SSL_CONTEXT) as resp:
                return resp.read()
        except Exception as e:
            log.warning("download failed %s: %s", url, e)
            return None

    def _upload_bytes(self, ftp: FTP, remote_path: str, data: bytes) -> bool:
        try:
            ftp.storbinary(f"STOR {remote_path}", io.BytesIO(data))
            return True
        except Exception as e:
            log.warning("FTP upload failed %s: %s — retrying once", remote_path, e)
            try:
                self._close_quietly()
                ftp2 = self._connect()
                self._ensure_dir(ftp2, str(PurePosixPath(remote_path).parent))
                ftp2.storbinary(f"STOR {remote_path}", io.BytesIO(data))
                return True
            except Exception as e2:
                log.error("FTP upload retry failed %s: %s", remote_path, e2)
                return False

    def upload_listing(
        self,
        listing_code: str,
        large_urls: list[str] | None,
        thumb_urls: list[str] | None,
    ) -> tuple[list[str], list[str]]:
        """Upload every image for one listing, return new public URLs in
        the same order (failures dropped). No-op when disabled."""
        large = self._dedupe(large_urls or [])
        thumb = self._dedupe(thumb_urls or [])
        if not self.enabled or not listing_code:
            return large, thumb
        try:
            ftp = self._connect()
        except Exception as e:
            log.error("FTP connect failed: %s — keeping original URLs", e)
            return large, thumb
        remote_dir = f"{self.remote_root}/{listing_code}"
        try:
            self._ensure_dir(ftp, remote_dir)
        except Exception as e:
            log.error("FTP mkdir %s failed: %s — keeping original URLs", remote_dir, e)
            return large, thumb
        new_large = self._upload_set(ftp, listing_code, large, prefix="img")
        new_thumb = self._upload_set(ftp, listing_code, thumb, prefix="thumb")
        return new_large, new_thumb

    def _upload_set(
        self,
        ftp: FTP,
        listing_code: str,
        urls: list[str],
        prefix: str,
    ) -> list[str]:
        if not urls:
            return []
        # Download in parallel — image fetches are the slow part. FTP STORs
        # still go serially on the single persistent session.
        with ThreadPoolExecutor(max_workers=self.download_workers) as ex:
            payloads = list(ex.map(self._download, urls))
        public_urls: list[str] = []
        for i, (url, data) in enumerate(zip(urls, payloads), 1):
            if not data:
                continue
            ext = self._ext_from_url(url)
            data = _transform_image(data, ext)
            name = f"{prefix}{i}{ext}"
            remote_path = f"{self.remote_root}/{listing_code}/{name}"
            if not self._upload_bytes(ftp, remote_path, data):
                continue
            public_urls.append(f"{self.public_base}/{listing_code}/{name}")
        return public_urls


# ───────────────────────────────────────────────────────  DB helpers

def _decode_urls(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


# ───────────────────────────────────────────────────────  Background loop

async def run_ftp_upload() -> None:
    """Long-running: drain pending rows, sleep, repeat.

    Every blocking FTP / urllib call is dispatched to `asyncio.to_thread`
    so the event loop stays free for the scraper + finder running in the
    same process. Without this, downloading/uploading a single listing
    would stall the other two workers for tens of seconds.
    """
    uploader = FtpUploader()
    if not uploader.enabled:
        log.error("FTP not configured — exiting.")
        return

    store = make_store()
    try:
        log.info("FTP upload loop started.")
        total_migrated = 0
        while True:
            rows = await asyncio.to_thread(store.pending_listings_for_ftp, 100)
            if not rows:
                log.info("No pending listings. Sleeping %.0fs.", SLEEP_WHEN_IDLE_SEC)
                await asyncio.sleep(SLEEP_WHEN_IDLE_SEC)
                continue

            for listing_code, raw_large, raw_thumb in rows:
                large = _decode_urls(raw_large)
                thumb = _decode_urls(raw_thumb)
                source_count = sum(1 for u in (large + thumb) if "batdongsan.com.vn" in u)
                if not source_count:
                    continue
                log.info("→ Listing %s: %d batdongsan images to migrate",
                         listing_code, source_count)
                try:
                    new_large, new_thumb = await asyncio.to_thread(
                        uploader.upload_listing, listing_code, large, thumb,
                    )
                except Exception as e:
                    log.error("  upload_listing raised: %s", e)
                    continue
                migrated_any = any(
                    "batdongsan.com.vn" not in u
                    for u in (new_large + new_thumb)
                )
                if not migrated_any:
                    log.warning("  ✗ no images migrated for %s — leaving row alone",
                                listing_code)
                    continue
                await asyncio.to_thread(
                    store.update_image_urls, listing_code, new_large, new_thumb,
                )
                total_migrated += 1
                log.info("  ✓ %s migrated (%d total since start)",
                         listing_code, total_migrated)
                await asyncio.sleep(SLEEP_BETWEEN_LISTINGS_SEC)
    finally:
        try: uploader.close()
        except Exception: pass
        try: store.close()
        except Exception: pass


if __name__ == "__main__":
    asyncio.run(run_ftp_upload())
