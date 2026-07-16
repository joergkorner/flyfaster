# Fly Faster

Upload a zip of paraglider IGC tracks, get back two HTML reports by email:

- **Day view** — every pilot compared with the field, day by day: speed levers
  radar, glide-speed distribution (wind removed), day wind reconstructed from
  everyone's circling, and a statistics section.
- **Pilot view** — each pilot's profile across days, showing how it develops.

Everything is measured from the tracks alone and is **day-relative**: pilots are
only ever compared with others who flew the same day. The full method is
documented in `analysis/METHODS_EN.md` and inside every report.

## How it works

```
Landing page (explainer + form)
        │  zip + email
        ▼
File-based queue (DATA_DIR/jobs/<id>/)
        │  worker (in-process thread, or external cron)
        ▼
analysis/thermal_strategy.py   ← the actual science, runs as a subprocess
        │  two HTML reports
        ▼
Gmail SMTP ──► pilot's inbox        (uploaded tracks deleted afterwards)
```

No database, no accounts. Job state is a `job.json` per job directory.

## Zip format

One folder per flying day named `YEAR_MONTH_DAY` (e.g. `2026_07_09`), each
containing that day's `.igc` files. A race day carries its start time:
`2026_07_09_UTC1000` — everything before 10:00 UTC is then ignored. A second
folder for the same day (`2026_07_09_2`) is fine. One wrapping root folder in
the zip is fine. `__MACOSX` junk is ignored.

## Run locally

```bash
docker compose up --build      # http://localhost:8000
```

Emails are written to `/data/jobs/<id>/email.eml` (console backend) — open
them with any mail client or extract the attachments. Or without Docker:

```bash
pip install -r requirements.txt
EMAIL_BACKEND=console uvicorn app.main:app --reload
```

## Run the analysis directly (no web)

The analysis script is a normal CLI and lives unmodified in `analysis/`:

```bash
python analysis/thermal_strategy.py path/to/day-folders --author ''
```

## Configuration

Everything is env-driven — see `.env.example`. The two that matter in
production: `EMAIL_BACKEND=gmail` with `GMAIL_USER`/`GMAIL_APP_PASSWORD`
(a Google **App Password**), and `DATA_DIR` pointing at a persistent volume.

## Deploying

See [DEPLOY.md](DEPLOY.md) — written for Railway (container + volume),
~30 minutes end to end.

## Repo layout

```
analysis/thermal_strategy.py   analysis engine (CLI, runs standalone)
analysis/METHODS_EN.md         the methods document
app/main.py                    FastAPI: landing page, /submit, /job/<id>
app/validation.py              zip structure checks
app/jobqueue.py                file-based queue
app/worker.py                  processes jobs (thread or `python -m app.worker once`)
app/mailer.py                  Gmail SMTP / console backend
app/templates, app/static      the one-page site
```

## Notes & limits

- Consumer Gmail: ~25 MB per mail (large reports are auto-zipped) and roughly
  500 mails/day — fine to start; `mailer.py` is the seam for a transactional
  provider later.
- Reports are generated in English. All display strings in the analysis script
  were translated from German with numeric output verified identical; a
  language selector is a planned follow-up (extract strings + `--lang` flag).
- Privacy: uploads are deleted right after the reports are sent; failed jobs
  are kept `KEEP_FAILED_DAYS` (default 7) for debugging, then deleted.
