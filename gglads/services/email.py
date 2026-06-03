"""Outbound email via SMTP.

Configuration lives in the `smtp` integration (services/integrations.py):
  host, port, username, password, from_email, from_name, use_tls.

The single entrypoint is send_email() — it returns (ok, detail). Failures
never raise: callers (invite flow etc.) treat email as best-effort, log a
warning, and fall back to surfacing the invite URL in the UI so the admin
can still share it manually.

Compatible with Gmail / SendGrid / Postmark / Mailgun / Amazon SES /
self-hosted Postfix — anything that speaks SMTP.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from gglads.services import integrations as integrations_svc

logger = logging.getLogger("gglads.email")


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "yes", "true", "on", "y"}


def is_configured(db: Session) -> bool:
    cfg = integrations_svc.get_config(db, "smtp")
    return all(
        (cfg.get(k) or "").strip()
        for k in ("host", "port", "from_email")
    )


def send_email(
    db: Session,
    to: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
) -> tuple[bool, str]:
    """Send one email. Returns (ok, detail). Never raises."""
    cfg = integrations_svc.get_config(db, "smtp")
    host = (cfg.get("host") or "").strip()
    port_s = (cfg.get("port") or "").strip()
    username = (cfg.get("username") or "").strip()
    password = (cfg.get("password") or "").strip()
    from_email = (cfg.get("from_email") or "").strip()
    from_name = (cfg.get("from_name") or "").strip()
    use_tls = _truthy(cfg.get("use_tls"))

    if not host or not port_s or not from_email or not to:
        return False, "SMTP not configured (need host, port, from_email)."
    try:
        port = int(port_s)
    except ValueError:
        return False, f"SMTP port is not a number: {port_s!r}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = to
    msg.set_content(text_body or _strip_tags(html_body))
    msg.add_alternative(html_body, subtype="html")

    try:
        if port == 465:
            # Implicit TLS — typical for port 465.
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                if username:
                    s.login(username, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                if use_tls:
                    s.starttls()
                    s.ehlo()
                if username:
                    s.login(username, password)
                s.send_message(msg)
    except smtplib.SMTPException as exc:
        logger.warning("SMTP send failed: %s", exc)
        return False, f"SMTP error: {type(exc).__name__}: {exc}"
    except OSError as exc:
        logger.warning("SMTP network error: %s", exc)
        return False, f"Network error reaching {host}:{port}: {exc}"
    except Exception as exc:  # noqa: BLE001 — never let email failures crash callers
        logger.exception("Unexpected SMTP error")
        return False, f"{type(exc).__name__}: {exc}"

    return True, "Sent."


def _strip_tags(html: str) -> str:
    """Very small fallback so plain-text recipients see something readable."""
    import re

    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Templates (kept here so callers stay one-liners)
# ---------------------------------------------------------------------------

def build_invite_email(
    invite_url: str,
    invitee_email: str,
    role_label: str,
    invited_by_name: str | None,
    expires_at_iso: str | None,
) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body) for an invitation email."""
    subject = "You've been invited to gglads"
    inviter_line = (
        f"<p>{invited_by_name} invited you to join the gglads workspace as <strong>{role_label}</strong>.</p>"
        if invited_by_name
        else f"<p>You've been invited to the gglads workspace as <strong>{role_label}</strong>.</p>"
    )
    expiry_line = (
        f"<p style='color:#5d6470;font-size:12px;'>This link expires on {expires_at_iso} UTC.</p>"
        if expires_at_iso else ""
    )
    html_body = f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#f3f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1a1d24;">
  <table cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e2e6ec;border-radius:12px;padding:32px;">
    <tr><td>
      <h1 style="margin:0 0 12px;font-size:22px;letter-spacing:-0.01em;">gglads</h1>
      {inviter_line}
      <p>Click the button below to set up your password and sign in.</p>
      <p style="margin:24px 0;">
        <a href="{invite_url}"
           style="display:inline-block;background:#4f6dd9;color:#ffffff;text-decoration:none;font-weight:600;padding:12px 22px;border-radius:8px;">
          Accept invite
        </a>
      </p>
      <p style="color:#5d6470;font-size:12px;word-break:break-all;">Or paste this URL into your browser:<br>{invite_url}</p>
      {expiry_line}
    </td></tr>
  </table>
</body></html>
"""
    text_body = (
        f"You've been invited to gglads as {role_label}.\n\n"
        f"Accept your invite: {invite_url}\n\n"
        + (f"This link expires on {expires_at_iso} UTC.\n" if expires_at_iso else "")
    )
    return subject, html_body, text_body
