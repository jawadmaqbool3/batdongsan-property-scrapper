"""Single entry point — runs all three workers concurrently.

  1) Scraper          (src/scraper.py)            — walks categories, saves
                                                    listings with agent_id +
                                                    agent_url. No login.
  2) FTP image upload (src/ftp_upload.py)         — migrates batdongsan
                                                    image URLs to your FTP.
  3) Phone finder     (src/agent_number_finder.py)— logs in with rotating
                                                    accounts, reveals phones,
                                                    writes to DB.

Each worker handles its own retries and errors. Stopping any one of them
doesn't kill the others — but if you Ctrl-C this process, all three stop.

Per-worker control:
  RUN_SCRAPER=true        # default true
  RUN_FTP_UPLOAD=true     # default true
  RUN_FINDER=true         # default true
Set to false to disable that worker for this run.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").as_posix()))
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("main")


def _enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() == "true"


async def _safe(name: str, coro_factory):
    """Run a worker and log if it ever exits or crashes. Doesn't restart —
    we'd rather see the error than mask it with a loop."""
    log.info("Starting %s", name)
    try:
        await coro_factory()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Worker %s crashed", name)
    log.info("Worker %s exited", name)


async def main() -> None:
    tasks: list[asyncio.Task] = []

    if _enabled("RUN_SCRAPER"):
        from scraper import run_scraper
        tasks.append(asyncio.create_task(_safe("scraper", run_scraper),
                                          name="scraper"))
    if _enabled("RUN_FTP_UPLOAD"):
        from ftp_upload import run_ftp_upload
        tasks.append(asyncio.create_task(_safe("ftp-upload", run_ftp_upload),
                                          name="ftp-upload"))
    if _enabled("RUN_FINDER"):
        from agent_number_finder import run_finder
        tasks.append(asyncio.create_task(_safe("number-finder", run_finder),
                                          name="number-finder"))

    if not tasks:
        log.error("All workers disabled. Set RUN_SCRAPER / RUN_FTP_UPLOAD / "
                  "RUN_FINDER to true in .env.")
        return

    log.info("Running %d worker(s).", len(tasks))
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down…")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
