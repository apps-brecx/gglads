"""Helena task-queue worker.

Drains due ScheduledTasks (publish posts, push campaigns, fetch metrics, post
digests, create email drafts) and reschedules recurring ones. Runs as a Render
cron service every minute. Approval-required tasks are skipped until a human
approves them in the app. Mirrors the existing cron entrypoints' shape.

Exit 0 always (a failed task is recorded on the task row, not the process) so
Render doesn't mark the minute-ly run as failed for an expected business error.
"""

from __future__ import annotations

import logging
import sys

from gglads.db.session import get_sessionmaker
from gglads.services.helena import execution as exec_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gglads.cron.helena_worker")


def main() -> int:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        summary = exec_svc.run_due_tasks(db)
        logger.info("Helena worker tick: %s", summary)
        return 0
    except Exception:
        logger.exception("Helena worker crashed")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
