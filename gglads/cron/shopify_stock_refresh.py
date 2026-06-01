"""Two-hourly stock refresh — catalog re-pull from Shopify + today's
inventory snapshot + OOS-state reconcile. Skips the orders sync to keep
the run fast (so it can safely fire every 2 hours).

Schedule: every odd hour at :30 (01:30, 03:30, ..., 23:30 UTC). Offset by
30 minutes from the 04:00 / 16:00 full syncs so the two crons never overlap.
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
logger = logging.getLogger("gglads.cron.shopify_stock_refresh")


def main() -> int:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        logger.info("Starting Shopify stock refresh (catalog + snapshot)")
        ok, detail, stats = shopify_svc.sync_stock_refresh(db)
        if ok:
            logger.info("Stock refresh succeeded: %s | stats=%s", detail, stats)
            return 0
        logger.error("Stock refresh failed: %s", detail)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
