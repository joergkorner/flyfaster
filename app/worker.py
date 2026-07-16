"""The worker: picks queued jobs, runs the analysis script, emails the reports.

Two ways to run it:
  - default: started as a daemon thread by the web app (WORKER_MODE=thread)
  - external: `python -m app.worker once` from a real cron/scheduler
    (set WORKER_MODE=external so the web app does not also start one)
"""
from __future__ import annotations
import shutil, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
from . import config, jobqueue, mailer
from .validation import day_file_entries


def _extract(zip_path: Path, dest: Path) -> int:
    """Extract usable IGC files using the exact same rules as the upload validator."""
    n = 0
    with zipfile.ZipFile(zip_path) as zf:
        entries, _stray, _bad, _root = day_file_entries(zf)
        for name, day, fname in entries:
            info = zf.getinfo(name)
            if info.file_size > 80 * 1024 * 1024:   # single-track sanity cap
                continue
            out = dest / day / fname
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            n += 1
    return n


def process_job(job_id: str):
    meta = jobqueue.read_meta(job_id)
    email = meta.get("email", "")
    jdir = jobqueue.job_dir(job_id)
    try:
        with tempfile.TemporaryDirectory(prefix="ffjob_") as tmp:
            tmp = Path(tmp)
            data = tmp / "data"
            n = _extract(jdir / "upload.zip", data)
            if n == 0:
                raise RuntimeError("no usable IGC files found in the archive")

            cmd = [sys.executable, str(config.ANALYSIS_SCRIPT), str(data),
                   "--author", config.REPORT_AUTHOR]
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                                  timeout=config.RUN_TIMEOUT_S)
            day_html = tmp / "schnell-fliegen.html"
            pilots_html = tmp / "schnell-fliegen-piloten.html"
            if proc.returncode != 0 or not day_html.exists():
                tail = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
                raise RuntimeError("analysis failed: " + " | ".join(tail[-3:])[:500])

            out = jdir / "out"; out.mkdir(exist_ok=True)
            files = []
            shutil.copyfile(day_html, out / "fly-faster-day-view.html")
            files.append(out / "fly-faster-day-view.html")
            if pilots_html.exists():
                shutil.copyfile(pilots_html, out / "fly-faster-pilot-view.html")
                files.append(out / "fly-faster-pilot-view.html")

            mailer.send_reports(email, job_id, meta, files)

        # success: drop the tracks immediately; reports only if really sent
        (jdir / "upload.zip").unlink(missing_ok=True)
        if config.EMAIL_BACKEND == "gmail":
            shutil.rmtree(jdir / "out", ignore_errors=True)
        jobqueue.finish(job_id, "done", "reports sent")
        print(f"[worker] {job_id} done -> {email}")
    except subprocess.TimeoutExpired:
        jobqueue.finish(job_id, "error", f"analysis exceeded {config.RUN_TIMEOUT_S // 60} min")
        _try_failure_mail(email, job_id, "the analysis took too long")
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"[:500]
        jobqueue.finish(job_id, "error", reason)
        _try_failure_mail(email, job_id, str(e)[:300])
        print(f"[worker] {job_id} ERROR: {reason}", file=sys.stderr)


def _try_failure_mail(email: str, job_id: str, reason: str):
    try:
        if email:
            mailer.send_failure(email, job_id, reason)
    except Exception as e:
        print(f"[worker] failure-mail failed for {job_id}: {e}", file=sys.stderr)


def tick() -> bool:
    """Process at most one job. Returns True if one was processed."""
    job_id = jobqueue.claim_next()
    if not job_id:
        return False
    process_job(job_id)
    return True


def loop():
    print(f"[worker] polling every {config.WORKER_POLL_S}s")
    last_cleanup = 0.0
    while True:
        try:
            while tick():
                pass
            if time.time() - last_cleanup > 3600:
                jobqueue.cleanup(); last_cleanup = time.time()
        except Exception as e:
            print(f"[worker] loop error: {e}", file=sys.stderr)
        time.sleep(config.WORKER_POLL_S)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":   # for external cron
        jobqueue.cleanup()
        while tick():
            pass
    else:
        loop()
