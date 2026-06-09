"""Giveaway maintenance cron.

Refreshes entries for live Instagram giveaways (reading comments / tag-a-friend),
and when a giveaway's window ends, draws a random winner and closes it. Weekly
giveaways automatically spin up next week's draft (image generated, queued for
publish approval — nothing publishes without sign-off).
"""

from __future__ import annotations

import logging
import sys

from gglads.db.session import get_sessionmaker
from gglads.services.helena import giveaways as gv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gglads.cron.giveaway_sync")


def main() -> int:
    session_local = get_sessionmaker()
    db = session_local()
    try:
        ok, detail, stats = gv.run_due(db)
        logger.info("%s | %s", detail, stats)
        return 0 if ok else 1
    except Exception:
        logger.exception("Giveaway sync crashed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
