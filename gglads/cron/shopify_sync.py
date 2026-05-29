"""Daily Shopify catalog sync.

Run by a Render Cron Job. Pulls collections + publications + products +
variants + orders, then writes today's inventory snapshot. Exit code 0
on success, 1 on failure (so Render marks the run failed).
"""

from __future__ import annotations

import logging
import sys

from gglads.db.session import get_sessionmaker
from gglads.services import shopify as shopify_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gglads.cron.shopify_sync")


def main() -> int:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        logger.info("Starting Shopify sync")
        ok, detail, stats = shopify_svc.sync_catalog(db)
        if ok:
            logger.info("Sync succeeded: %s | stats=%s", detail, stats)
            return 0
        logger.error("Sync failed: %s", detail)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
