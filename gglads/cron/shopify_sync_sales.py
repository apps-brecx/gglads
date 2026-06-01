"""Second-of-the-day full Shopify sync.

Originally called sync_sales_only — but sales-only does NOT refresh
total_inventory, so the OOS state could be 24h stale until the morning
catalog sync. Bumped to sync_full so inventory really does refresh
twice a day (matching what the OOS page promises).

Filename kept for render.yaml back-compat.
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
        logger.info("Starting Shopify second-of-day full sync")
        ok, detail, stats = shopify_svc.sync_full(db)
        if ok:
            logger.info("Sync succeeded: %s | stats=%s", detail, stats)
            return 0
        logger.error("Sync failed: %s", detail)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
