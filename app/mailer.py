"""Send the two report HTMLs by email.

Backends:
  console — writes the composed message to jobs/{id}/email.eml (local dev; default)
  gmail   — smtp.gmail.com:465 with GMAIL_USER + GMAIL_APP_PASSWORD
            (App Password, not the account password; requires 2-step verification)

Gmail notes: ~25 MB attachment cap (we zip the reports above ATTACH_ZIP_OVER_MB)
and roughly 500 recipients/day on a consumer account — plenty to start, and the
backend seam is where a transactional provider would slot in later.
"""
from __future__ import annotations
import io, smtplib, zipfile
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from . import config


def _build(to_addr: str, job_id: str, meta: dict, attachments: list[tuple[str, bytes]]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((config.MAIL_FROM_NAME, config.GMAIL_USER or "dev@localhost"))
    msg["To"] = to_addr
    msg["Subject"] = f"{config.BRAND_NAME} — your reports ({meta.get('n_days', '?')} day(s), {meta.get('n_files', '?')} flights)"
    site = f"\n\n{config.PUBLIC_URL}" if config.PUBLIC_URL else ""
    msg.set_content(
        f"Hi,\n\n"
        f"your analysis is done: {meta.get('n_files', '?')} flights across {meta.get('n_days', '?')} day folder(s).\n\n"
        f"Attached:\n"
        f"  - day view: every pilot compared with the field, day by day\n"
        f"  - pilot view: each pilot's profile across the days\n\n"
        f"Open them in any browser. Everything is day-relative — see the methods "
        f"section inside the report.\n\n"
        f"Your uploaded tracks have been deleted from our server.\n\n"
        f"Happy flying!{site}\n"
    )
    total = sum(len(b) for _, b in attachments)
    if total > config.ATTACH_ZIP_OVER_MB * 1024 * 1024:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, blob in attachments:
                z.writestr(name, blob)
        attachments = [(f"fly-faster-reports-{job_id}.zip", buf.getvalue())]
    for name, blob in attachments:
        maintype, subtype = ("application", "zip") if name.endswith(".zip") else ("text", "html")
        msg.add_attachment(blob, maintype=maintype, subtype=subtype, filename=name)
    return msg


def send_reports(to_addr: str, job_id: str, meta: dict, files: list[Path]):
    attachments = [(p.name, p.read_bytes()) for p in files]
    msg = _build(to_addr, job_id, meta, attachments)
    if config.EMAIL_BACKEND == "gmail":
        if not (config.GMAIL_USER and config.GMAIL_APP_PASSWORD):
            raise RuntimeError("EMAIL_BACKEND=gmail but GMAIL_USER / GMAIL_APP_PASSWORD are not set")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
            s.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            s.send_message(msg)
    else:  # console
        out = config.DATA_DIR / "jobs" / job_id / "email.eml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(bytes(msg))


def send_failure(to_addr: str, job_id: str, reason: str):
    msg = EmailMessage()
    msg["From"] = formataddr((config.MAIL_FROM_NAME, config.GMAIL_USER or "dev@localhost"))
    msg["To"] = to_addr
    msg["Subject"] = f"{config.BRAND_NAME} — your upload could not be processed"
    msg.set_content(
        f"Hi,\n\nunfortunately your upload (job {job_id}) could not be processed.\n\n"
        f"Reason: {reason}\n\n"
        f"Common causes: altitude data broken in every file, or folders not "
        f"following the naming (YEAR_MONTH_DAY, e.g. 2026_07_09). Feel free to fix "
        f"and upload again.\n"
    )
    if config.EMAIL_BACKEND == "gmail":
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
            s.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            s.send_message(msg)
    else:
        out = config.DATA_DIR / "jobs" / job_id / "email.eml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(bytes(msg))
