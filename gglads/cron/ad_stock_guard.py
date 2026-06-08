"""Out-of-stock ad guard cron.

Pauses Meta ads whose Shopify product is out of stock (and emails an alert),
and auto-resumes the ads it paused once stock returns. Admin overrides
(allow_oos) keep specific ads running while OOS. Pausing never spends, so it's
safe to run automatically; resume only restarts ads the guard itself paused.
"""

from __future__ import annotations

import logging
import sys

from gglads.db.session import get_sessionmaker
from gglads.services.helena import ad_stock_guard as guard_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gglads.cron.ad_stock_guard")


def main() -> int:
    session_local = get_sessionmaker()
    db = session_local()
    try:
        ok, detail, stats = guard_svc.run_guard(db)
        if ok:
            logger.info("%s | %s", detail, stats)
            return 0
        logger.warning("Stock guard did not run: %s", detail)
        return 0  # not-connected / read-only is not a hard failure
    except Exception:
        logger.exception("Stock guard crashed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
