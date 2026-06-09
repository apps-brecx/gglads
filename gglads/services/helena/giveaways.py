"""Instagram giveaway lifecycle.

Create a weekly giveaway for a product (the post always uses the real bottle),
publish it through the approval queue, collect entries from the post's comments
(each tag-a-friend = one entry — more tags, more chances), then draw a random
winner and close it.

Instagram API limits: it can't tell us who follows the account or who shared a
post, so 'must follow / must share' are stated rules enforced by manual review
(an admin can mark an entry ineligible). Comments and tags ARE readable.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gglads.models.helena import Giveaway, GiveawayEntry, GiveawaySample, Post

logger = logging.getLogger("gglads.helena.giveaways")

_TAG_RE = re.compile(r"@([A-Za-z0-9._]{1,30})")


def _now() -> datetime:
    return datetime.now(UTC)


# --- CRUD ---------------------------------------------------------------

def list_giveaways(db: Session, limit: int = 100) -> list[Giveaway]:
    return list(db.scalars(
        select(Giveaway).order_by(Giveaway.created_at.desc()).limit(limit)).all())


def get(db: Session, giveaway_id: int) -> Giveaway | None:
    return db.get(Giveaway, giveaway_id)


def create_giveaway(db: Session, *, name: str, flavor: str | None = None,
                    variant: str | None = None, rules_text: str | None = None,
                    days: int = 7, weekly: bool = True,
                    user_id: int | None = None) -> Giveaway:
    g = Giveaway(
        name=(name or "Weekly giveaway").strip()[:255],
        flavor=(flavor or None), variant=(variant or None),
        rules_text=rules_text or _DEFAULT_RULES,
        recurrence="weekly" if weekly else None,
        ends_at=_now() + timedelta(days=max(1, int(days or 7))),
        status="draft", created_by_user_id=user_id,
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


_DEFAULT_RULES = (
    "To enter: 1) Follow our page, 2) Like this post, 3) Tag a friend in the "
    "comments (each friend you tag = another entry!), 4) Share to your story. "
    "Winner drawn at random when the giveaway ends. Good luck! 🎉"
)


def delete_giveaway(db: Session, giveaway_id: int) -> None:
    g = db.get(Giveaway, giveaway_id)
    if g is not None:
        db.delete(g)
        db.commit()


# --- Samples ------------------------------------------------------------

def list_samples(db: Session) -> list[GiveawaySample]:
    return list(db.scalars(
        select(GiveawaySample).order_by(GiveawaySample.created_at.desc())).all())


def add_sample(db: Session, *, name: str, image_url: str,
               notes: str | None = None) -> GiveawaySample | None:
    image_url = (image_url or "").strip()
    if not image_url:
        return None
    s = GiveawaySample(name=(name or "Giveaway sample").strip()[:255],
                       image_url=image_url, notes=notes)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def delete_sample(db: Session, sample_id: int) -> None:
    s = db.get(GiveawaySample, sample_id)
    if s is not None:
        db.delete(s)
        db.commit()


# --- Post generation (always the real bottle) ---------------------------

def generate_post(db: Session, giveaway: Giveaway, *, user_id: int | None = None) -> dict:
    """Generate an on-brand giveaway image using the REAL bottle and a giveaway
    caption. Stores them on the giveaway. Reuses the generate_image skill so the
    'never invent a bottle' rule applies."""
    from gglads.services.helena import skills as skills_svc
    concept = (f"Eye-catching Instagram giveaway announcement for our drink. "
               f"Bold 'GIVEAWAY' / 'WIN' treatment, festive, on-brand, leaving room "
               f"for text. {('Flavor: ' + giveaway.flavor) if giveaway.flavor else ''}")
    args: dict[str, Any] = {"concept": concept, "aspect_ratio": "1:1"}
    if giveaway.flavor:
        args["flavor"] = giveaway.flavor
    if giveaway.variant:
        args["variant"] = giveaway.variant
    res = skills_svc.run_skill(db, "generate_image", args, user_id=user_id, session_id=None)
    if not res.get("ok") or not res.get("images"):
        return {"ok": False, "error": res.get("error", "Couldn't generate the image.")}
    giveaway.image_url = res["images"][0]["url"]
    if not giveaway.caption:
        giveaway.caption = _build_caption(giveaway)
    giveaway.updated_at = _now()
    db.commit()
    return {"ok": True, "image_url": giveaway.image_url}


def _build_caption(g: Giveaway) -> str:
    head = f"🎉 {g.name} 🎉\n\n"
    rules = g.rules_text or _DEFAULT_RULES
    when = ""
    if g.ends_at:
        when = f"\n\nEnds {g.ends_at.strftime('%b %d')}."
    return head + rules + when


def send_to_approval(db: Session, giveaway: Giveaway, *, user_id: int | None = None) -> dict:
    """Create the IG post draft for this giveaway and queue a publish for
    approval (nothing publishes without sign-off)."""
    from gglads.services.helena import execution as exec_svc
    if not giveaway.image_url:
        return {"ok": False, "error": "Generate the giveaway image first."}
    post = Post(caption=giveaway.caption or _build_caption(giveaway),
                image_url=giveaway.image_url, channel="instagram",
                status="draft", created_by_user_id=user_id)
    db.add(post)
    db.commit()
    db.refresh(post)
    giveaway.post_id = post.id
    giveaway.status = "scheduled"
    giveaway.updated_at = _now()
    db.commit()
    exec_svc.enqueue(db, title=f"Publish giveaway: {giveaway.name}",
                     kind="publish_post", spec={"post_id": post.id}, user_id=user_id)
    return {"ok": True, "post_id": post.id}


# --- Entry collection ---------------------------------------------------

def parse_tags(text: str, *, exclude: set[str] | None = None) -> list[str]:
    """Return the @handles tagged in a comment (lowercased, de-duped, order kept)."""
    exclude = {e.lower().lstrip("@") for e in (exclude or set())}
    seen: list[str] = []
    for h in _TAG_RE.findall(text or ""):
        hl = h.lower()
        if hl not in exclude and hl not in seen:
            seen.append(hl)
    return seen


def sync_published_media(db: Session, giveaway: Giveaway) -> None:
    """Once the approval queue publishes the giveaway's post, copy the IG media
    id + permalink onto the giveaway so entries can be read."""
    if giveaway.media_external_id or not giveaway.post_id:
        return
    post = db.get(Post, giveaway.post_id)
    if post and post.external_id:
        giveaway.media_external_id = post.external_id
        giveaway.permalink = post.permalink
        if giveaway.status in ("draft", "scheduled"):
            giveaway.status = "live"
        giveaway.updated_at = _now()
        db.commit()


def collect_entries(db: Session, giveaway: Giveaway) -> dict:
    """Read the giveaway post's comments and turn each tag-a-friend into an
    entry. Idempotent — re-running only adds new (comment, tag) pairs."""
    sync_published_media(db, giveaway)
    if not giveaway.media_external_id:
        return {"ok": False, "error": "This giveaway isn't published to Instagram yet."}
    from gglads.services.helena.meta.factory import get_meta_provider
    res = get_meta_provider(db).fetch_media_comments(giveaway.media_external_id)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "Couldn't read comments.")}

    existing = {
        (e.comment_id, e.tagged)
        for e in db.scalars(select(GiveawayEntry).where(
            GiveawayEntry.giveaway_id == giveaway.id)).all()
    }
    from gglads.services.helena.meta import oauth as meta_oauth
    brand_handle = (meta_oauth.get_meta_config(db).get("ig_username") or "").lstrip("@")
    added = 0
    for c in res["comments"]:
        commenter = (c.get("username") or "").lower()
        if not commenter:
            continue
        tags = parse_tags(c.get("text"), exclude={brand_handle, commenter})
        for tag in tags:
            key = (c.get("id"), tag)
            if key in existing:
                continue
            db.add(GiveawayEntry(giveaway_id=giveaway.id, username=commenter,
                                 tagged=tag, source="tag", comment_id=c.get("id"),
                                 eligible=True))
            existing.add(key)
            added += 1
    giveaway.entries_synced_at = _now()
    if giveaway.status == "scheduled":
        giveaway.status = "live"
    db.commit()
    return {"ok": True, "added": added, "total": entry_count(db, giveaway.id)}


def entry_count(db: Session, giveaway_id: int, *, eligible_only: bool = True) -> int:
    q = select(func.count()).select_from(GiveawayEntry).where(
        GiveawayEntry.giveaway_id == giveaway_id)
    if eligible_only:
        q = q.where(GiveawayEntry.eligible.is_(True))
    return int(db.scalar(q) or 0)


def list_entries(db: Session, giveaway_id: int) -> list[GiveawayEntry]:
    return list(db.scalars(select(GiveawayEntry).where(
        GiveawayEntry.giveaway_id == giveaway_id).order_by(GiveawayEntry.created_at)).all())


def leaderboard(db: Session, giveaway_id: int) -> list[dict]:
    """Entrants ranked by number of eligible entries (chances)."""
    rows = db.execute(
        select(GiveawayEntry.username, func.count())
        .where(GiveawayEntry.giveaway_id == giveaway_id, GiveawayEntry.eligible.is_(True))
        .group_by(GiveawayEntry.username)
        .order_by(func.count().desc())
    ).all()
    return [{"username": u, "entries": int(n)} for u, n in rows]


def set_eligibility(db: Session, entry_id: int, eligible: bool) -> None:
    e = db.get(GiveawayEntry, entry_id)
    if e is not None:
        e.eligible = eligible
        db.commit()


# --- Draw ---------------------------------------------------------------

def draw_winner(db: Session, giveaway: Giveaway) -> dict:
    """Pick one random winner weighted by entries (each eligible entry is a
    ticket). Returns {ok, winner, pool} so the UI can animate a spin."""
    entries = db.scalars(select(GiveawayEntry).where(
        GiveawayEntry.giveaway_id == giveaway.id,
        GiveawayEntry.eligible.is_(True))).all()
    usernames = [e.username for e in entries]
    if not usernames:
        return {"ok": False, "error": "No eligible entries to draw from yet."}
    winner = random.choice(usernames)  # noqa: S311 — not security-sensitive
    giveaway.winner_username = winner
    giveaway.drawn_at = _now()
    giveaway.status = "closed"
    giveaway.updated_at = _now()
    db.commit()
    # A de-duped pool of names for the spin animation.
    pool = sorted(set(usernames))
    return {"ok": True, "winner": winner, "pool": pool, "tickets": len(usernames)}


def close(db: Session, giveaway: Giveaway) -> None:
    giveaway.status = "closed"
    giveaway.updated_at = _now()
    db.commit()


def run_due(db: Session) -> tuple[bool, str, dict]:
    """Scheduled maintenance: refresh entries for live giveaways, and when a
    giveaway's window ends, draw a winner + close it. For weekly giveaways,
    spin up next week's draft (image generated, queued for publish approval)."""
    now = _now()
    synced = drawn = created = 0
    for g in db.scalars(select(Giveaway).where(
            Giveaway.status.in_(("scheduled", "live")))).all():
        sync_published_media(db, g)
        if g.media_external_id and collect_entries(db, g).get("ok"):
            synced += 1
    for g in db.scalars(select(Giveaway).where(
            Giveaway.status == "live", Giveaway.ends_at.is_not(None))).all():
        if g.ends_at and g.ends_at <= now and not g.winner_username:
            collect_entries(db, g)  # final sync before the draw
            if draw_winner(db, g).get("ok"):
                drawn += 1
                if g.recurrence == "weekly":
                    ng = create_giveaway(
                        db, name=g.name, flavor=g.flavor, variant=g.variant,
                        rules_text=g.rules_text, days=7, weekly=True,
                        user_id=g.created_by_user_id)
                    if generate_post(db, ng, user_id=g.created_by_user_id).get("ok"):
                        send_to_approval(db, ng, user_id=g.created_by_user_id)
                        created += 1
    return True, (f"Giveaways: synced {synced}, drawn {drawn}, "
                  f"next-week drafts {created}."), {"synced": synced, "drawn": drawn,
                                                    "created": created}
