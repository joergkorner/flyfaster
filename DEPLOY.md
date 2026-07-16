# Deploying Fly Faster

Target: Railway (container + persistent volume) with Gmail for sending.
Budget ~30–45 minutes, most of it clicking. Nothing here locks you in — the
app is a plain Docker container and the data is plain files, so it moves to
any host with `docker run` + `rsync`.

## 1. Gmail App Password (~5 min)

1. Google Account → **Security** → turn on **2-Step Verification** (required).
2. Security → **App passwords** → create one, name it "flyfaster".
3. Copy the 16-character password. This is `GMAIL_APP_PASSWORD` — the account
   password itself is never used and never works here.

Deliverability tip: send a first test to yourself and mark it "not spam" if
needed. At club/comp volume a personal Gmail is fine (~500 mails/day cap).

## 2. GitHub (~5 min)

Push this repo to GitHub (public if you want the tool to be open source):

```bash
git init && git add . && git commit -m "Fly Faster v1"
git remote add origin git@github.com:YOU/flyfaster.git
git push -u origin main
```

## 3. Railway (~15 min)

1. railway.app → **New Project** → **Deploy from GitHub repo** → pick the repo.
   Railway detects the Dockerfile and builds.
2. Service → **Settings → Volumes** → add a volume, mount path **`/data`**.
3. Service → **Variables** → set:

   | Variable | Value |
   |---|---|
   | `DATA_DIR` | `/data` |
   | `EMAIL_BACKEND` | `gmail` |
   | `GMAIL_USER` | yourname@gmail.com |
   | `GMAIL_APP_PASSWORD` | the app password |
   | `PUBLIC_URL` | (fill in after step 4) |

4. **Settings → Networking → Generate Domain** → you get
   `https://something.up.railway.app`. Put it into `PUBLIC_URL` (used in the
   email footer). A custom domain can be added there later in one step.
5. Redeploy. Open the URL, upload a small test zip to your own address.

Every `git push` now redeploys automatically. Queued jobs survive restarts
(they live on the volume); a job that was mid-run during a deploy stays
"running" — requeue it by editing its `job.json` status back to `queued`, or
just ask the pilot to resubmit.

## 4. The worker

By default the worker runs **inside the web container** (a polling thread,
`WORKER_MODE=thread`) — simplest, one service, fine at launch scale since only
one job runs at a time anyway.

If you later want the classic cron shape: set `WORKER_MODE=external` on the
web service and add a second Railway service from the same repo with a cron
schedule running `python -m app.worker once`.

## 5. Smoke test checklist

- [ ] `GET /healthz` returns `ok`
- [ ] Landing page shows the three sample radars
- [ ] Uploading a zip with a wrong folder name shows a helpful error
- [ ] A valid small zip → confirmation page with day/file counts + job link
- [ ] Email arrives with `fly-faster-day-view.html` + `fly-faster-pilot-view.html`
- [ ] `/job/<id>` shows "Done"

## 6. Costs & scaling

- Railway: ~$5/month at hobby scale (CPU only while analysing).
- A 10-pilot day takes ~1–2 min; a full season archive can take tens of
  minutes — the 45-min `RUN_TIMEOUT_S` covers it; raise it for huge archives.
- If demand grows: raise Railway resources (one click), or move the same
  container to a Hetzner VPS. Storage is transient (uploads deleted after
  processing), so the volume stays small.

## 7. Moving away from Gmail later

Swap `EMAIL_BACKEND` in `app/mailer.py` for a transactional provider
(Postmark, Resend, SES) — it is one function; the rest of the app does not
know how mail is sent.
