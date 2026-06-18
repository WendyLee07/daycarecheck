"""Transactional email via MailerSend.

Mirrors the ClawForce ``mail_service`` pattern (same provider, same env var
names, dev-friendly logging fallback). Stripped to the two email types
Daycarecheck actually sends:

  - ``send_report_ready_email``  → "your report is ready" with a link
  - ``send_no_agent_email``      → "24h passed, no Manus agent picked it up"

The MailerSend SDK is synchronous; we wrap it with ``run_in_executor`` so
FastAPI handlers can ``await`` without blocking the event loop.

If ``MAILERSEND_API_KEY`` is unset (local dev, port-forward, missing
secret) the function logs a structured line and returns successfully —
it never crashes the calling request.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


MAILERSEND_API_KEY    = os.environ.get("MAILERSEND_API_KEY") or None
MAILERSEND_FROM_EMAIL = os.environ.get("MAILERSEND_FROM_EMAIL", "noreply@agentic-commons.org")
MAILERSEND_FROM_NAME  = os.environ.get("MAILERSEND_FROM_NAME", "Daycare Check")


@dataclass(frozen=True)
class EmailSendResult:
    success: bool
    skipped_reason: str | None = None  # set when MAILERSEND_API_KEY not configured
    message_id: str | None = None


def _send_via_mailersend(
    *, to_email: str, subject: str, html: str, text: str,
) -> EmailSendResult:
    """Synchronous send. Called from async via run_in_executor."""
    if not MAILERSEND_API_KEY:
        log.info(
            "mailersend_skipped_no_api_key to=%s subject=%r", to_email, subject,
        )
        return EmailSendResult(success=True, skipped_reason="no_api_key")
    try:
        # Import inside the function so the module is loadable without the
        # mailersend SDK installed (e.g. running an unrelated unit test).
        from mailersend import EmailBuilder, MailerSendClient
    except ImportError:
        log.warning(
            "mailersend_skipped_sdk_missing to=%s subject=%r", to_email, subject,
        )
        return EmailSendResult(success=True, skipped_reason="sdk_missing")

    try:
        ms = MailerSendClient(api_key=MAILERSEND_API_KEY)
        email = (
            EmailBuilder()
            .from_email(MAILERSEND_FROM_EMAIL, MAILERSEND_FROM_NAME)
            .to(to_email, to_email)
            .subject(subject)
            .html(html)
            .text(text)
            .build()
        )
        response = ms.emails.send(email)
        return EmailSendResult(
            success=True,
            message_id=str(response.get("id", "")) if isinstance(response, dict) else None,
        )
    except Exception as exc:
        log.warning(
            "mailersend_send_failed to=%s subject=%r err=%s",
            to_email, subject, exc,
        )
        return EmailSendResult(success=False)


async def _send_async(*, to_email: str, subject: str, html: str, text: str) -> EmailSendResult:
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _send_via_mailersend(
            to_email=to_email, subject=subject, html=html, text=text,
        ),
    )


# ── Public surface: two transactional templates ──────────────────────────


async def send_report_ready_email(
    *,
    to_email: str,
    daycare_name: str,
    report_url: str,
) -> EmailSendResult:
    """Sent when a daycare diligence task transitions to COMPLETED.

    Lean HTML — single CTA button styling, no images, inline CSS so Gmail
    doesn't strip it.
    """
    subject = f"Your daycare report is ready: {daycare_name}"
    text = (
        f"Your background-check report for {daycare_name} is ready.\n\n"
        f"View it here: {report_url}\n\n"
        f"-- Daycare Check (Agentic Commons)"
    )
    html = f"""\
<!doctype html><html><body style="font-family:Inter,system-ui,sans-serif;\
color:#1c1917;background:#fbf8f3;padding:32px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #eae6df;\
border-radius:12px;padding:32px;">
    <h2 style="margin:0 0 8px;font-weight:700;color:#1c1917;">Your daycare report is ready</h2>
    <p style="margin:0 0 24px;color:#44403c;line-height:1.6;">
      We've finished compiling the public-record background check for
      <strong>{daycare_name}</strong>.
    </p>
    <p style="margin:0 0 24px;">
      <a href="{report_url}"
         style="display:inline-block;background:#508a6c;color:#fff;text-decoration:none;\
padding:12px 24px;border-radius:8px;font-weight:600;">
        View report
      </a>
    </p>
    <p style="margin:0;color:#78716c;font-size:14px;line-height:1.6;">
      Reports stay live at the link above. You can also bookmark it, share it
      with your partner, or send it to the next parent on the waiting list —
      every report is CC0 public domain.
    </p>
    <hr style="border:none;border-top:1px solid #eae6df;margin:32px 0 16px;">
    <p style="margin:0;color:#a8a29e;font-size:12px;">
      — Daycare Check, by <a href="https://agentic-commons.org" style="color:#508a6c;">Agentic Commons</a>
    </p>
  </div>
</body></html>"""
    return await _send_async(
        to_email=to_email, subject=subject, html=html, text=text,
    )


async def send_no_agent_email(
    *,
    to_email: str,
    daycare_name: str,
    retry_url: str | None = None,
) -> EmailSendResult:
    """Sent when a queued task expires after 24h with no agent picking it up.

    We don't pretend it succeeded — we tell the user honestly that no agent
    was available, and offer a one-click resubmit.
    """
    subject = f"No agent available — please retry: {daycare_name}"
    retry_line_text = f"\nResubmit: {retry_url}\n" if retry_url else ""
    retry_line_html = (
        f'<p style="margin:0 0 24px;"><a href="{retry_url}" '
        f'style="display:inline-block;background:#508a6c;color:#fff;text-decoration:none;'
        f'padding:12px 24px;border-radius:8px;font-weight:600;">Try again</a></p>'
        if retry_url else ""
    )
    text = (
        f"We weren't able to complete your background-check request for "
        f"{daycare_name}.\n\n"
        f"No qualified research agent picked it up within 24 hours. This usually "
        f"clears up within an hour or two — please resubmit when convenient.\n"
        f"{retry_line_text}\n"
        f"-- Daycare Check (Agentic Commons)"
    )
    html = f"""\
<!doctype html><html><body style="font-family:Inter,system-ui,sans-serif;\
color:#1c1917;background:#fbf8f3;padding:32px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #eae6df;\
border-radius:12px;padding:32px;">
    <h2 style="margin:0 0 8px;font-weight:700;color:#1c1917;">No agent available</h2>
    <p style="margin:0 0 24px;color:#44403c;line-height:1.6;">
      We couldn't complete your background-check request for
      <strong>{daycare_name}</strong>. No qualified research agent picked it
      up within 24 hours.
    </p>
    <p style="margin:0 0 24px;color:#44403c;line-height:1.6;">
      This usually clears up within an hour or two — please resubmit when
      convenient.
    </p>
    {retry_line_html}
    <hr style="border:none;border-top:1px solid #eae6df;margin:32px 0 16px;">
    <p style="margin:0;color:#a8a29e;font-size:12px;">
      — Daycare Check, by <a href="https://agentic-commons.org" style="color:#508a6c;">Agentic Commons</a>
    </p>
  </div>
</body></html>"""
    return await _send_async(
        to_email=to_email, subject=subject, html=html, text=text,
    )
