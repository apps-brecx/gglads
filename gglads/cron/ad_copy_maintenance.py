"""Nightly: regenerate ad copy for ad groups whose keywords changed since the
copy was written (stashed as PENDING for user approval), and pause any
previous Google Ads RSAs whose 24h grace window has elapsed.

Run via Render Cron Job (see render.yaml).
"""

from __future__ import annotations

import logging
import sys

from gglads.db.session import get_sessionmaker
from gglads.services import ad_copy_generation as ad_copy_svc
from gglads.services import campaigns as campaigns_svc
from gglads.services import google_ads_push as gads_push_svc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gglads.cron.ad_copy_maintenance")


def regen_stale_to_pending(db) -> tuple[int, int]:
    """For each ad group with stale copy and no pending version: generate
    a fresh copy and save it as pending. Returns (succeeded, failed)."""
    stale = campaigns_svc.ad_groups_with_stale_copy(db)
    logger.info("Found %d ad group(s) with stale copy", len(stale))
    ok_count = 0
    fail_count = 0
    for ag in stale:
        reason = "Keywords have changed since this copy was generated."
        try:
            ok, detail, _ = ad_copy_svc.generate_for_ad_group(
                db,
                campaign_id=ag.campaign_id,
                ad_group_id=ag.id,
                save_as_pending=True,
                reason=reason,
            )
            if ok:
                ok_count += 1
                logger.info(
                    "Pending copy stashed for ad_group_id=%d: %s", ag.id, detail
                )
            else:
                fail_count += 1
                logger.warning(
                    "Stale-copy regen failed for ad_group_id=%d: %s", ag.id, detail
                )
        except Exception:  # noqa: BLE001
            fail_count += 1
            logger.exception("Stale-copy regen crashed for ad_group_id=%d", ag.id)
    return ok_count, fail_count


def pause_due_ads(db) -> tuple[int, list[str]]:
    """Pause any prev_ad past its pause-at time."""
    paused, errors = gads_push_svc.pause_due_prev_ads(db)
    logger.info("Paused %d previous Google Ads RSAs (errors=%d)", paused, len(errors))
    for e in errors:
        logger.warning("Pause error: %s", e)
    return paused, errors


def main() -> int:
    SessionLocal = get_sessionmaker()
    db = SessionLocal()
    try:
        regen_ok, regen_fail = regen_stale_to_pending(db)
        paused, pause_errors = pause_due_ads(db)
        logger.info(
            "Done: %d pending stashed, %d failed; %d ads paused, %d pause errors.",
            regen_ok, regen_fail, paused, len(pause_errors),
        )
        # Don't fail the cron just because Google Ads complained for one ad —
        # the pending-copy work is independent.
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
