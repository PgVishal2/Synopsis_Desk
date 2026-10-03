# Deploy the web app on Railway

The web app (Flask) runs the same engine as the Streamlit app. Streamlit (`app.py`) still works
locally; the web app is `wsgi.py` + the `webapp/` folder.

## 1. Put the project on GitHub
- Push this folder to a **private** GitHub repo.
- `.env` and `company_profile.json` are in `.gitignore`. Never commit keys.

## 2. Create the Railway service
1. railway.com → **New Project → Deploy from GitHub repo** → pick the repo.
2. Railway finds `railway.json` and builds the `Dockerfile` (Python + Tesseract with English
   and Hindi). The first build takes 3–5 minutes.

## 3. Add a volume (keeps the database and synopses)
Service → **Settings → Volumes → Add volume**, mount path **`/data`**.
Without it, the master password and history are wiped on every redeploy.

## 4. Add variables (Service → Variables)
| Variable | Value |
|---|---|
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | Main AI provider (same as your `.env`) |
| `LLM2_…`, `LLM3_…`, `LLM4_…` | Backup providers (optional) |
| `OCR_SPACE_API_KEY` | Cloud OCR (optional; Tesseract is always the backup) |
| `LLM_MAX_INPUT_TOKENS` | e.g. `250000` for 1M-context models |
| `MASTER_PASSWORD` | Optional: sets the first master password, so nobody else can create it |

Optional: `OCR_ENGINE` (`auto` / `tesseract`), `OCR_WORKERS` (default 2–3), `JOB_WORKERS`
(synopses at the same time, default 2), `KEEP_DAYS` (history kept, default 90),
`MAX_UPLOAD_MB` (default 100), `SESSION_HOURS` (default 12), `ENABLE_BID_CHECK` (default off).

## 5. Open it
Service → **Settings → Networking → Generate domain**. Open the link:
- First visit: **create the master password** (unless you set `MASTER_PASSWORD`).
  Do this right after the first deploy.
- Settings → **Test now** checks every AI provider.

## 6. Limit spending
Railway → Workspace → **Usage → set a usage limit** (e.g. $10) so a runaway job can't run up
the bill. Normal use fits in the Hobby plan's included $5.

## How OCR works in the cloud
1. Text pages are read directly (no OCR).
2. Scanned pages go to **OCR.space** first (if `OCR_SPACE_API_KEY` is set).
3. If OCR.space fails for a page (limit, outage, empty result), that page goes to **Tesseract**
   inside the server, so the run continues.
4. After 3 failures in a row, OCR.space is paused for 10 minutes and every page uses Tesseract,
   so one outage doesn't stall a 50-page scan. Settings → System status shows "Paused".
5. `OCR_ENGINE=tesseract` uses only Tesseract (no cloud).

## Updating
Push to GitHub and Railway rebuilds and redeploys. A synopsis that is running during a redeploy
is marked failed. Run it again.
