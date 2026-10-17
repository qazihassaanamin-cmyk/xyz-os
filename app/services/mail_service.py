"""Outbound mail.

SMTP is optional. When it is not configured the message is written to the
application log so a development or single-server deployment still works —
invitation and reset links remain usable. Production should set the SMTP_*
variables in .env.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app, render_template

log = logging.getLogger(__name__)


class MailNotConfigured(Exception):
    """Raised internally when SMTP settings are absent."""


def _settings() -> dict:
    cfg = current_app.config
    return {
        "host": cfg.get("SMTP_HOST") or "",
        "port": int(cfg.get("SMTP_PORT") or 587),
        "username": cfg.get("SMTP_USERNAME") or "",
        "password": cfg.get("SMTP_PASSWORD") or "",
        "use_tls": bool(cfg.get("SMTP_USE_TLS", True)),
        "mail_from": cfg.get("MAIL_FROM") or "no-reply@tavryc.com",
    }


def send_email(subject: str, recipients: list[str], text_body: str, html_body: str | None = None) -> bool:
    """Send one message. Returns True on success, False when only logged."""
    settings = _settings()
    if not settings["host"]:
        _log_fallback(subject, recipients, text_body)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["mail_from"]
    message["To"] = ", ".join(recipients)
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        if settings["port"] == 465:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], context=context, timeout=15) as smtp:
                if settings["username"]:
                    smtp.login(settings["username"], settings["password"])
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings["host"], settings["port"], timeout=15) as smtp:
                if settings["use_tls"]:
                    smtp.starttls(context=context)
                if settings["username"]:
                    smtp.login(settings["username"], settings["password"])
                smtp.send_message(message)
    except Exception:
        log.exception("SMTP delivery failed for %s", recipients)
        _log_fallback(subject, recipients, text_body)
        return False
    return True


def _log_fallback(subject: str, recipients: list[str], text_body: str) -> None:
    """Deliver to the server log when SMTP is unavailable.

    This is a deliberate, documented fallback for a single-server internal
    deployment, not a silent failure: the log line is loud and includes the
    link so the operator can hand it to the member.
    """
    log.warning(
        "MAIL NOT SENT (SMTP not configured)\n"
        "  To: %s\n"
        "  Subject: %s\n"
        "  Body:\n%s",
        ", ".join(recipients),
        subject,
        text_body,
    )


def _render(subject: str, recipients: list[str], template: str, context: dict) -> bool:
    text_body = render_template(f"email/{template}.txt", **context)
    try:
        html_body = render_template(f"email/{template}.html", **context)
    except Exception:  # pragma: no cover - text-only templates are acceptable
        html_body = None
    return send_email(subject, recipients, text_body, html_body)


def send_invitation_email(user, link: str) -> bool:
    return _render(
        f"You have been invited to {current_app.config.get('COMPANY_NAME', 'Tavryc')}",
        [user.email],
        "invitation",
        {
            "link": link,
            "email": user.email,
            "invited_by": getattr(user, "invited_by", None),
            "expires_hours": int(current_app.config.get("INVITE_TOKEN_TTL_MINUTES", 2880)) // 60,
        },
    )


def send_reset_email(user, link: str) -> bool:
    return _render(
        "Reset your password",
        [user.email],
        "password_reset",
        {
            "link": link,
            "user": user,
            "expires_minutes": int(current_app.config.get("RESET_TOKEN_TTL_MINUTES", 60)),
        },
    )


def send_notification_email(user, subject: str, summary: str, link: str) -> bool:
    return _render(
        subject,
        [user.email],
        "notification",
        {"link": link, "summary": summary, "user": user},
    )
