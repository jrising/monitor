"""
Sending notifications. Email for now; another channel (ntfy, Slack, ...) would be another function here
plus a key in the feed list's `alerts:` section.

Settings (environment or monitor.env):
  MONITOR_SMTP_HOST, MONITOR_SMTP_PORT   e.g. smtp.dreamhost.com and 587 (STARTTLS, the default) or 465 (SSL)
  MONITOR_SMTP_USER, MONITOR_SMTP_PASSWORD
  MONITOR_MAIL_FROM                      defaults to MONITOR_SMTP_USER
Without MONITOR_SMTP_HOST, the local `sendmail` program is used if there is one.
"""
from __future__ import annotations

import os
import shutil
import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


class NotifyError(RuntimeError):
    pass


def email_configured() -> str:
    """How email would be sent ('smtp host:port', 'sendmail'), or '' if it can't be."""
    host = os.environ.get("MONITOR_SMTP_HOST")
    if host:
        return f"SMTP {host}:{os.environ.get('MONITOR_SMTP_PORT', '587')}"
    return "sendmail" if _sendmail() else ""


def _sendmail():
    return shutil.which("sendmail") or next((p for p in ("/usr/sbin/sendmail", "/usr/lib/sendmail")
                                             if os.path.exists(p)), None)


def send_email(to: list[str], subject: str, body: str) -> None:
    """Send a plain-text email. Raises NotifyError with a readable reason on failure."""
    host = os.environ.get("MONITOR_SMTP_HOST")
    sender = os.environ.get("MONITOR_MAIL_FROM") or os.environ.get("MONITOR_SMTP_USER") or ""
    if not sender and not host:
        sender = f"monitor@{os.uname().nodename}"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(sender.split("@")[-1] or None) if "@" in sender else None)
    msg.set_content(body)
    try:
        if host:
            port = int(os.environ.get("MONITOR_SMTP_PORT", "587"))
            user, password = os.environ.get("MONITOR_SMTP_USER"), os.environ.get("MONITOR_SMTP_PASSWORD")
            ctx = ssl.create_default_context()
            if port == 465:
                smtp = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
            else:
                smtp = smtplib.SMTP(host, port, timeout=20)
                smtp.starttls(context=ctx)
            with smtp:
                if user:
                    smtp.login(user, password or "")
                smtp.send_message(msg)
        else:
            sm = _sendmail()
            if not sm:
                raise NotifyError("no way to send email: set MONITOR_SMTP_HOST (and user/password) in monitor.env")
            p = subprocess.run([sm, "-t", "-oi"], input=msg.as_bytes(), capture_output=True, timeout=30)
            if p.returncode != 0:
                raise NotifyError(f"sendmail failed: {p.stderr.decode(errors='replace').strip()[:300]}")
    except NotifyError:
        raise
    except (OSError, smtplib.SMTPException) as e:
        raise NotifyError(f"{type(e).__name__}: {e}"[:300]) from None
