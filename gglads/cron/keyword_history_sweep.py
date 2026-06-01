"""Daily: snapshot Search Console queries per product into product_keyword_history.

Run via Render Cron Job (see render.yaml). Cheap call to SC per product; the
upsert is a delete-then-insert for today's rows so a re-run is idempotent.
"""

from __future__ import annotations

import logging
import sys

from gglads.db.session import get_sessionmaker
from gglads.services import keyword_history as kh_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gglads.cron.kw_history")


def main() -> int:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        ok, skipped, failed = kh_svc.snapshot_all_products(db)
        logger.info(
            "Keyword history sweep: %d ok, %d skipped, %d failed",
            ok, skipped, failed,
        )
        return 0 if failed == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
