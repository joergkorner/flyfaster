"""Fly Faster — single-page service.

GET  /            explainer page (methods + sample radars) with the upload form
POST /submit      zip + email -> validate -> queue -> confirmation
GET  /job/{id}    tiny status page (queued/running/done/error)
GET  /healthz     liveness probe
"""
from __future__ import annotations
import re, threading
from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from . import config, jobqueue, worker
from .validation import validate_zip, ValidationError

BASE = Path(__file__).resolve().parent
app = FastAPI(title=config.BRAND_NAME, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
JOB_ID_RE = re.compile(r"^[0-9]{14}-[A-Za-z0-9_\-]{4,16}$")


@app.on_event("startup")
def _startup():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "jobs").mkdir(parents=True, exist_ok=True)
    print(f"[app] {config.BRAND_NAME} v{config.VERSION} — data dir: {config.DATA_DIR}")
    if config.WORKER_MODE == "thread":
        threading.Thread(target=worker.loop, daemon=True).start()


def _ctx(request: Request, **kw):
    return {"request": request, "brand": config.BRAND_NAME, "version": config.VERSION,
            "max_mb": config.MAX_UPLOAD_MB, **kw}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


@app.post("/submit", response_class=HTMLResponse)
async def submit(request: Request, email: str = Form(...), archive: UploadFile = File(...)):
    email = email.strip()
    if not EMAIL_RE.match(email):
        return templates.TemplateResponse(request, "index.html",
            _ctx(request, error="That email address does not look valid.", keep_email=email),
            status_code=400)
    data = await archive.read()
    try:
        summary = validate_zip(data)
    except ValidationError as e:
        return templates.TemplateResponse(request, "index.html",
            _ctx(request, error=str(e), keep_email=email), status_code=400)

    job_id = jobqueue.create_job(email, data, summary)
    pos = jobqueue.queue_position(job_id)
    return templates.TemplateResponse(request, "submitted.html",
        _ctx(request, job_id=job_id, email=email, summary=summary, position=pos))


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str):
    meta = jobqueue.read_meta(job_id) if JOB_ID_RE.match(job_id) else None
    if not meta:
        return templates.TemplateResponse(request, "status.html",
            _ctx(request, meta=None, job_id=job_id), status_code=404)
    pos = jobqueue.queue_position(job_id) if meta.get("status") == "queued" else 0
    return templates.TemplateResponse(request, "status.html",
        _ctx(request, meta=meta, job_id=job_id, position=pos))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
