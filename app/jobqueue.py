"""File-based job queue. A job is a directory under DATA_DIR/jobs/:

    jobs/{id}/job.json     status + metadata
    jobs/{id}/upload.zip   the submitted archive (deleted after success)
    jobs/{id}/out/         generated reports (deleted after the email is sent)
    jobs/{id}/email.eml    console-backend copy of the outgoing mail (dev mode)

Statuses: queued -> running -> done | error. Atomic enough for one worker;
the worker claims a job by flipping status while holding an O_EXCL lock file.
"""
from __future__ import annotations
import json, secrets, datetime, shutil
from pathlib import Path
from . import config

JOBS_DIR = config.DATA_DIR / "jobs"


def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def read_meta(job_id: str) -> dict | None:
    p = job_dir(job_id) / "job.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def write_meta(job_id: str, meta: dict):
    d = job_dir(job_id); d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    tmp.replace(d / "job.json")


def create_job(email: str, zip_bytes: bytes, summary: dict) -> str:
    job_id = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_urlsafe(6)
    d = job_dir(job_id); d.mkdir(parents=True, exist_ok=True)
    (d / "upload.zip").write_bytes(zip_bytes)
    write_meta(job_id, {
        "id": job_id, "email": email, "created_at": _now(),
        "status": "queued", "message": "", **summary,
    })
    return job_id


def claim_next() -> str | None:
    """Return the id of the oldest queued job, atomically marked running."""
    if not JOBS_DIR.exists():
        return None
    for d in sorted(JOBS_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = read_meta(d.name)
        if not meta or meta.get("status") != "queued":
            continue
        lock = d / ".claim"
        try:
            lock.touch(exist_ok=False)
        except FileExistsError:
            continue
        meta["status"] = "running"; meta["started_at"] = _now()
        write_meta(d.name, meta)
        return d.name
    return None


def finish(job_id: str, status: str, message: str = ""):
    meta = read_meta(job_id) or {"id": job_id}
    meta["status"] = status
    meta["message"] = message
    meta["finished_at"] = _now()
    write_meta(job_id, meta)


def queue_position(job_id: str) -> int:
    """1-based position among queued jobs, 0 if not queued."""
    if not JOBS_DIR.exists():
        return 0
    queued = sorted(d.name for d in JOBS_DIR.iterdir()
                    if d.is_dir() and (read_meta(d.name) or {}).get("status") == "queued")
    return queued.index(job_id) + 1 if job_id in queued else 0


def cleanup():
    """Apply retention: drop old failed jobs entirely, strip done jobs to metadata."""
    if not JOBS_DIR.exists():
        return
    now = datetime.datetime.utcnow()
    for d in JOBS_DIR.iterdir():
        if not d.is_dir():
            continue
        meta = read_meta(d.name)
        if not meta:
            continue
        ts = meta.get("finished_at") or meta.get("created_at") or _now()
        try:
            age_days = (now - datetime.datetime.fromisoformat(ts.rstrip("Z"))).days
        except ValueError:
            age_days = 0
        if meta.get("status") == "error" and age_days > config.KEEP_FAILED_DAYS:
            shutil.rmtree(d, ignore_errors=True)
        elif meta.get("status") == "done" and age_days > config.KEEP_DONE_META_DAYS:
            shutil.rmtree(d, ignore_errors=True)
