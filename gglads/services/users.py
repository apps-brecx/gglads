"""User management — list, invite, accept invite, change role, deactivate.

Roles in use across the app:
  admin    — full access; can manage users, integrations, push to Google Ads.
  operator — can run AI generation, approve drafts, manage products and
             campaigns, but can't manage users / integrations.
  worker   — assigned products' SEO/ads tasks; can tick them done. No admin.
  viewer   — read-only.

Invite flow:
  1. Admin POSTs email + role to /users/invite. We create a User row with
     password_hash=NULL, a fresh 32-byte hex invite_token, and a 7-day
     expiry. We return the invite URL (/invite/<token>) to the admin —
     they share it with the invitee (Slack / email / whatever).
  2. The invitee opens /invite/<token>, sets a name + password. We hash
     the password, clear the token, mark them active. Done.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gglads.auth.password import hash_password
from gglads.models.shopify_product import ShopifyProduct
from gglads.models.user import User

logger = logging.getLogger("gglads.users")


ROLES: list[tuple[str, str]] = [
    ("admin", "Admin"),
    ("operator", "Operator"),
    ("worker", "Worker"),
    ("viewer", "Viewer"),
]
ROLE_SLUGS = {r for r, _ in ROLES}

INVITE_LIFETIME_DAYS = 7


def role_label(slug: str) -> str:
    for s, label in ROLES:
        if s == slug:
            return label
    return slug.replace("_", " ").title()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


# ---------------------------------------------------------------------------
# List + decorate
# ---------------------------------------------------------------------------

def list_users(db: Session) -> list[dict]:
    """All users with display info + count of products they're assigned to.
    Sorted by created_at desc so newly-invited users surface at the top."""
    users = db.execute(
        select(User).order_by(User.created_at.desc())
    ).scalars().all()
    if not users:
        return []
    # One grouped query to attach product counts.
    assigned_rows = db.execute(
        select(
            ShopifyProduct.assigned_to_user_id,
            func.count(ShopifyProduct.id).label("n"),
        )
        .where(ShopifyProduct.assigned_to_user_id.is_not(None))
        .group_by(ShopifyProduct.assigned_to_user_id)
    ).all()
    assigned_by_uid: dict[int, int] = {r.assigned_to_user_id: int(r.n) for r in assigned_rows}
    out: list[dict] = []
    for u in users:
        out.append({
            "id": u.id,
            "email": u.email,
            "name": u.name or "",
            "role": u.role,
            "role_label": role_label(u.role),
            "is_active": u.is_active,
            "created_at": u.created_at,
            "last_login_at": u.last_login_at,
            "assigned_products": assigned_by_uid.get(u.id, 0),
            "has_password": bool(u.password_hash),
            "invite_pending": bool(u.invite_token) and u.password_hash is None,
            "invite_expires_at": u.invite_token_expires_at,
            "invite_token": u.invite_token,  # admin uses this to copy the URL
        })
    return out


# ---------------------------------------------------------------------------
# Invite
# ---------------------------------------------------------------------------

def invite_user(
    db: Session,
    email: str,
    role: str,
    invited_by_user_id: int,
    name: str | None = None,
    invite_base_url: str | None = None,
) -> tuple[bool, str, User | None]:
    email = _normalize_email(email)
    if not _valid_email(email):
        return False, "That doesn't look like a valid email.", None
    if role not in ROLE_SLUGS:
        return False, f"Unknown role: {role}", None
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        # Re-issue the invite if the existing row has no password yet.
        if existing.password_hash is None:
            existing.invite_token = secrets.token_hex(32)
            existing.invite_token_expires_at = datetime.now(timezone.utc) + timedelta(
                days=INVITE_LIFETIME_DAYS
            )
            existing.invited_by_user_id = invited_by_user_id
            existing.role = role
            existing.is_active = True
            if name and not existing.name:
                existing.name = name.strip()[:255]
            db.commit()
            detail = "Re-issued invite (user existed but hadn't set a password yet)."
            email_detail = _send_invite_email(
                db, existing, invited_by_user_id, invite_base_url
            )
            if email_detail:
                detail += " " + email_detail
            return True, detail, existing
        return False, "A user with that email already exists and has logged in.", None

    user = User(
        email=email,
        name=(name.strip()[:255] if name else None),
        password_hash=None,
        role=role,
        is_active=True,
        invite_token=secrets.token_hex(32),
        invite_token_expires_at=datetime.now(timezone.utc)
        + timedelta(days=INVITE_LIFETIME_DAYS),
        invited_by_user_id=invited_by_user_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    detail = "Invite created."
    email_detail = _send_invite_email(db, user, invited_by_user_id, invite_base_url)
    if email_detail:
        detail += " " + email_detail
    else:
        detail += " Share the link below with the new user."
    return True, detail, user


def _send_invite_email(
    db: Session,
    user: User,
    invited_by_user_id: int,
    invite_base_url: str | None,
) -> str | None:
    """Best-effort invite-email send. Returns a short status message for
    the admin (or None when SMTP isn't configured)."""
    from gglads.services import email as email_svc
    if not email_svc.is_configured(db):
        return None
    if not invite_base_url:
        return "(SMTP configured but no base URL to build the invite link from — set it via the calling route.)"
    invite_url = f"{invite_base_url.rstrip('/')}/invite/{user.invite_token}"

    inviter = db.get(User, invited_by_user_id) if invited_by_user_id else None
    inviter_name = (inviter.name or inviter.email) if inviter else None
    expires_iso = (
        user.invite_token_expires_at.strftime("%Y-%m-%d %H:%M")
        if user.invite_token_expires_at else None
    )
    subject, html, text = email_svc.build_invite_email(
        invite_url=invite_url,
        invitee_email=user.email,
        role_label=role_label(user.role),
        invited_by_name=inviter_name,
        expires_at_iso=expires_iso,
    )
    ok, msg = email_svc.send_email(db, user.email, subject, html, text)
    if ok:
        return f"Email sent to {user.email}."
    return f"⚠ Couldn't send email ({msg}). Share the link below manually."


def find_by_invite_token(db: Session, token: str) -> User | None:
    if not token:
        return None
    u = db.scalar(select(User).where(User.invite_token == token))
    if u is None:
        return None
    if u.invite_token_expires_at and u.invite_token_expires_at < datetime.now(timezone.utc):
        return None
    return u


def accept_invite(
    db: Session, token: str, name: str, password: str
) -> tuple[bool, str, User | None]:
    if not token:
        return False, "Missing invite token.", None
    if len(password) < 8:
        return False, "Password must be at least 8 characters.", None
    name = (name or "").strip()
    if not name:
        return False, "Name is required.", None
    u = find_by_invite_token(db, token)
    if u is None:
        return False, "This invite is invalid or has expired. Ask an admin to re-issue.", None
    u.name = name[:255]
    u.password_hash = hash_password(password)
    u.invite_token = None
    u.invite_token_expires_at = None
    u.is_active = True
    db.commit()
    db.refresh(u)
    return True, "Welcome aboard — you're signed in.", u


def reissue_invite(
    db: Session,
    user_id: int,
    invited_by_user_id: int | None = None,
    invite_base_url: str | None = None,
) -> tuple[bool, str, str | None]:
    """Admin action: refresh the invite token + expiry for a pending invite.
    Returns the new token in the result tuple. Also re-sends the email
    when SMTP is configured."""
    u = db.get(User, user_id)
    if u is None:
        return False, "User not found.", None
    u.invite_token = secrets.token_hex(32)
    u.invite_token_expires_at = datetime.now(timezone.utc) + timedelta(
        days=INVITE_LIFETIME_DAYS
    )
    db.commit()
    detail = "Invite re-issued."
    email_detail = _send_invite_email(
        db, u, invited_by_user_id or 0, invite_base_url
    )
    if email_detail:
        detail += " " + email_detail
    return True, detail, u.invite_token


# ---------------------------------------------------------------------------
# Role + active management
# ---------------------------------------------------------------------------

def update_role(db: Session, user_id: int, role: str) -> tuple[bool, str]:
    if role not in ROLE_SLUGS:
        return False, f"Unknown role: {role}"
    u = db.get(User, user_id)
    if u is None:
        return False, "User not found."
    # Don't let the last admin demote themselves into oblivion.
    if u.role == "admin" and role != "admin":
        remaining_admins = db.scalar(
            select(func.count(User.id))
            .where(User.role == "admin")
            .where(User.is_active.is_(True))
            .where(User.id != u.id)
        ) or 0
        if remaining_admins == 0:
            return False, "Can't demote the last active admin."
    u.role = role
    db.commit()
    return True, f'Updated role to {role_label(role)}.'


def set_active(db: Session, user_id: int, active: bool) -> tuple[bool, str]:
    u = db.get(User, user_id)
    if u is None:
        return False, "User not found."
    if not active and u.role == "admin":
        remaining_admins = db.scalar(
            select(func.count(User.id))
            .where(User.role == "admin")
            .where(User.is_active.is_(True))
            .where(User.id != u.id)
        ) or 0
        if remaining_admins == 0:
            return False, "Can't deactivate the last active admin."
    u.is_active = bool(active)
    db.commit()
    return True, ("Activated." if active else "Deactivated.")
