"""Central configuration, all env-driven. Nothing else needs editing to deploy."""
import os
from pathlib import Path

VERSION = "1.2"
BRAND_NAME = os.environ.get("BRAND_NAME", "Fly Faster")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # e.g. https://flyfaster.up.railway.app (used in emails)

# Storage: on Railway attach a volume and set DATA_DIR to its mount path (e.g. /data)
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()

# ---- submission limits (abuse protection, generous by default) --------------
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "300"))
MAX_DAYS_PER_ZIP = int(os.environ.get("MAX_DAYS_PER_ZIP", "60"))
MAX_FILES_PER_ZIP = int(os.environ.get("MAX_FILES_PER_ZIP", "800"))

# ---- analysis ----------------------------------------------------------------
ANALYSIS_SCRIPT = Path(os.environ.get(
    "ANALYSIS_SCRIPT",
    Path(__file__).resolve().parent.parent / "analysis" / "thermal_strategy.py",
)).resolve()
RUN_TIMEOUT_S = int(os.environ.get("RUN_TIMEOUT_S", "2700"))   # 45 min hard stop per job
REPORT_AUTHOR = os.environ.get("REPORT_AUTHOR", "")            # '' hides the credit line

# ---- worker (the "cron") ------------------------------------------------------
# In-process worker polls the queue this often. Set WORKER_MODE=external to run
# the worker separately (python -m app.worker) from a real cron/scheduler instead.
WORKER_MODE = os.environ.get("WORKER_MODE", "thread")          # thread | external
WORKER_POLL_S = int(os.environ.get("WORKER_POLL_S", "20"))

# ---- email (Gmail) ------------------------------------------------------------
# EMAIL_BACKEND=console writes the outgoing mail to the job dir instead of
# sending — the local/dev mode. For real sending: gmail + an App Password
# (Google account -> Security -> 2-Step Verification -> App passwords).
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "console")     # console | gmail | brevo
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
MAIL_SENDER = os.environ.get("MAIL_SENDER", "")                # verified sender for brevo (falls back to GMAIL_USER)
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", BRAND_NAME)
# If attachments exceed this, they are sent as a single .zip instead (Gmail cap is 25 MB)
ATTACH_ZIP_OVER_MB = float(os.environ.get("ATTACH_ZIP_OVER_MB", "18"))

# ---- retention ----------------------------------------------------------------
# Successful jobs: uploaded tracks are deleted right after the email is sent.
# Failed jobs are kept this many days for debugging, then deleted.
KEEP_FAILED_DAYS = int(os.environ.get("KEEP_FAILED_DAYS", "7"))
KEEP_DONE_META_DAYS = int(os.environ.get("KEEP_DONE_META_DAYS", "30"))  # job.json only, no tracks
