"""
email_sender.py — Unified Email Sender for All VoltEdge Reports
---------------------------------------------------------------
Single function used by pre-market brief, mid-session pulse,
post-market report, and feedback loop. Always logs outcome.
"""
import os
import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def validate_email_config() -> str:
    """
    Validate email configuration at startup.
    Returns a human-readable status line for the runner banner.
    """
    enabled = os.getenv("REPORT_EMAIL_ENABLED")
    if enabled != "1":
        return f"Email: DISABLED (REPORT_EMAIL_ENABLED='{enabled or 'unset'}')"

    to_addr = os.getenv("REPORT_EMAIL_TO")
    smtp_host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com")
    smtp_port = os.getenv("REPORT_SMTP_PORT", "587")
    smtp_user = os.getenv("REPORT_SMTP_USER")
    smtp_pass = os.getenv("REPORT_SMTP_PASSWORD")

    missing = []
    if not to_addr:
        missing.append("REPORT_EMAIL_TO")
    if not smtp_user:
        missing.append("REPORT_SMTP_USER")
    if not smtp_pass:
        missing.append("REPORT_SMTP_PASSWORD")

    if missing:
        return f"Email: MISCONFIGURED — missing: {', '.join(missing)}"

    return (
        f"Email: ENABLED TO={to_addr} SMTP={smtp_host}:{smtp_port} "
        f"USER={'set' if smtp_user else 'MISSING'} PASS={'set' if smtp_pass else 'MISSING'}"
    )


def send_report_email(
    subject: str,
    body_md: str,
    attachment_path: Optional[str] = None,
) -> bool:
    """
    Send an email report. Used by all VoltEdge reports.

    Returns True if sent successfully, False otherwise.
    Always logs the outcome — never silently returns.
    """
    enabled = os.getenv("REPORT_EMAIL_ENABLED")
    if enabled != "1":
        logger.info(f"[Email] Skipped '{subject}' — REPORT_EMAIL_ENABLED='{enabled or 'unset'}'")
        return False

    to_addr = os.getenv("REPORT_EMAIL_TO")
    smtp_host = os.getenv("REPORT_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("REPORT_SMTP_PORT", "587"))
    smtp_user = os.getenv("REPORT_SMTP_USER")
    smtp_pass = os.getenv("REPORT_SMTP_PASSWORD")

    if not all([to_addr, smtp_user, smtp_pass]):
        logger.error(
            f"[Email] FAILED '{subject}' — credentials missing: "
            f"TO={'set' if to_addr else 'MISSING'}, "
            f"USER={'set' if smtp_user else 'MISSING'}, "
            f"PASS={'set' if smtp_pass else 'MISSING'}"
        )
        return False

    # Convert markdown to HTML
    try:
        import markdown as md_lib
        html = md_lib.markdown(body_md, extensions=["tables", "nl2br"])
    except ImportError:
        html = f"<pre>{body_md}</pre>"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.set_content(body_md)
    msg.add_alternative(html, subtype="html")

    # Attach markdown file if provided and exists
    if attachment_path and os.path.exists(attachment_path):
        try:
            with open(attachment_path, "rb") as fh:
                msg.add_attachment(
                    fh.read(),
                    maintype="text",
                    subtype="markdown",
                    filename=os.path.basename(attachment_path),
                )
        except Exception as e:
            logger.warning(f"[Email] Attachment failed for {attachment_path}: {e}")

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)
        logger.info(f"[Email] Sent '{subject}' to {to_addr}")
        return True
    except Exception as e:
        logger.error(f"[Email] FAILED '{subject}' to {to_addr}: {type(e).__name__}: {e}")
        return False
