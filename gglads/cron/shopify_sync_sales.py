"""Sales-only Shopify sync (orders + today's inventory snapshot).

Run by a Render Cron Job a second time per day, between the full-sync runs.
Much faster than the full sync because it skips the catalog phases.
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
logger = logging.getLogger("gglads.cron.shopify_sync_sales")


def main() -> int:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        logger.info("Starting Shopify sales-only sync")
        ok, detail, stats = shopify_svc.sync_sales_only(db)
        if ok:
            logger.info("Sales sync succeeded: %s | stats=%s", detail, stats)
            return 0
        logger.error("Sales sync failed: %s", detail)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
