"""Weekly: generate fresh AI collection suggestions from site-wide organic queries.

Run via Render Cron Job. Output goes into collection_suggestions (status=pending);
the UI surfaces them on /collections.
"""

from __future__ import annotations

import logging
import sys

from gglads.db.session import get_sessionmaker
from gglads.services import collection_suggestions as cs_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gglads.cron.coll_suggest")


def main() -> int:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        ok, detail, saved = cs_svc.generate_suggestions(db, days=90, max_suggestions=8)
        logger.info("Collection suggestions sweep: %s | saved=%d", detail, len(saved))
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
