# Synopsis Desk

Upload the documents of any tender (NIT, RFP, EOI, GeM bid, e-auction catalogue, corrigenda,
special conditions, site lists, policies – text, scanned or as screenshots / photos) and get a ready-to-share **Word
synopsis** in your house style, plus a **review sheet** that shows where every key figure came
from and what a person should check before bidding.

```
Tender files ─► Read (text + OCR) ─► Label each file ─► Condense if very long ─► AI writes structured data
            ─► Automatic checks ─► Word synopsis + review sheet ─► Edit in the browser if needed
```

Works for any tender type: works / EPC, supply, services, consultancy, manpower, GeM (service,
product, BOQ, custom), EOI, empanelment, rate contract, PPP / concession, licence / advertising
rights, e-auction, lease / allotment, IBC Form G, disinvestment, multi-volume RFPs.

---

## 1. What you get

| Output | What it is |
|---|---|
| **Synopsis (.docx)** | Key facts table, numbered sections chosen for the tender type, *Documents to Submit* checklist (detailed mode), *Quick Decision* table and a *✅ In Summary* |
| **Review sheet (.md)** | Errors ❌ / warnings ⚠️ / info ℹ️, every key fact with file, page and exact quote, the documents read and their roles, the AI provider used and tokens spent |
| **Data (.json)** | The synopsis as editable data – edit and rebuild the Word file without another AI call |

**Two lengths**

| Mode | Length | Content |
|---|---|---|
| **Compact** | at most 3–4 pages | Only the must-haves: key facts, all dates, the full eligibility table, scoring table (when marks decide), money & terms, lots (if any), scope in one short table, Quick Decision. The review sheet warns if a model writes longer |
| **Detailed** | about 5–6 pages | Full eligibility, scope & deliverables, evaluation marks, payment, penalties, securities, other terms, Documents to Submit, Quick Decision |

Both modes are **table-first**: anything with 3 or more similar items becomes a table; bullets
hold one short fact each; each fact appears once; sources are shown briefly as "(p.12)".

**Highlights:** 🟨 yellow = items that decide bid / no-bid (last date / due date, pre-bid,
**minimum turnover**, EMD, contract period, JV rule, conflicts, relaxations) · 🟦 cyan = other
financial thresholds (net worth, solvency, project-value experience) · **bold** = key figures.
Turnover, deadline, JV and the cyan items are fixed automatically if the AI forgets.

**Quick Decision** – plain Yes / No answers with the figure: turnover required? MSE / Startup
relaxation? EMD? security? experience? JV allowed? contract period? evaluation? If the closing date
has passed, the app itself adds *"Bid still open? – No"*.

---

## 2. Using the website

1. Log in with the master password.
2. **New synopsis** → drop **all** files of the tender together (PDF, Word .docx, images or .txt) →
   choose *Compact* or *Detailed* → **Generate**. Progress is shown live.
3. Read the checks on the result page (❌ first, then ⚠️).
4. **Edit before sharing** – change the title, top table, summary or notes; or use
   *Advanced: edit everything as data* to paste corrected data. Saving rebuilds the Word file and
   re-runs the checks **without** an AI call.
5. Download the Word file / review sheet. **History** keeps every past synopsis.

**Tips for good results**
- Upload every file of the tender in one go: main NIT / catalogue, corrigenda, special conditions,
  site lists, BOQ, standard conditions, policies. Each file gets a role automatically
  (corrigendum > main tender > special conditions & lists > standard conditions > policy).
- Portal bids (GeM, IREPS, CPPP…) keep the scope, ATC, payment terms or BOQ in **linked files**.
  The review sheet lists any linked file you have not uploaded – download and add them.
- Give downloaded files clear names (`ScopeOfWork.pdf`, `ATC.pdf`, `LotDetails.pdf`) – names help
  the app label them.
- Portal pages you only have as a screenshot (e.g. IREPS lot details) can be uploaded as
  images (PNG / JPG). Name them clearly, e.g. `LotDetails.png`. Photos of paper: straight-on, good light.

---

## 3. How accuracy is protected

| Check | What it does |
|---|---|
| Source quotes | Every key date, amount and requirement carries a file, page and exact quote; the app confirms the quote exists in the documents |
| Number match | Numbers in each value must appear in its quote (₹4 crore = 4,00,00,000; any date format) |
| Table figures | Every amount, percentage and period written in the tables and bullets must exist somewhere in the documents (portal data-sheet formats like "Percentage(%) 5.00" included); otherwise ⚠️ "check it". Calculations must say "computed" |
| **Lakh / crore slip** | An amount 10×, 100× or 1000× off its source becomes a red ❌ (e.g. 290000000 written as ₹2.9 crore) |
| Second AI check | A short second call re-checks critical figures against their page (and the next page); "could not confirm" is a ⚠️, a different number is a ❌ |
| Conflicts between files | Different values in different files are shown side by side ("36 months (NIT) vs 1 year (GeM)"), never silently merged |
| No guessing | Anything the documents don't say is written "Not stated" – JV, exemptions or periods are never assumed; policy defaults are labelled as such |
| Fine print | The AI is told to compare figures inside forms / certificate formats with the eligibility clauses and to look for gaps in marks / slab tables |
| Dates | Valid calendar dates; deadline after issue date; closed bids flagged in the review sheet **and** the synopsis |
| Missing files | Warns when no main tender / catalogue was uploaded, and when a PDF links to documents that were not uploaded |
| OCR pages | Pages read by OCR are listed so their figures get a human look |
| Empty sections / broken highlights | Detected and repaired or flagged automatically |

> The synopsis is a fast, checked first draft. **A person should always read the ❌ and ⚠️ items
> before a bid decision.**

---

## 4. AI providers

Any OpenAI-compatible API works. One main provider plus up to three backups:

| Slot | Recommended | Role |
|---|---|---|
| `LLM_` | **DeepSeek V4.1 Flash** (paid, `deepseek-flash`, `https://api.deepseek.com`) | Main – used for every run |
| `LLM2_` | Google Gemini (free tier) | Backup 1 |
| `LLM3_` | OpenRouter free model | Backup 2 |
| `LLM4_` | optional | Backup 3 |

- A backup is used only if the provider before it fails (no balance, limit reached, outage). The
  review sheet shows which provider wrote the synopsis and why others were skipped.
- Free tiers often hit their limits on 40–90k-token tenders and are weaker on lakh / crore maths –
  keep a paid main provider for real work.
- Do not use providers whose free access is "trial / prototyping only" (e.g. NVIDIA's free API)
  on the live website.
- **Settings → Test now** checks every configured provider.

**Cost (DeepSeek V4.1 Flash):** about **₹2–4.5 per 50–60 page tender**; small single NITs well
under ₹1. Off-peak hours (weekdays outside 6:30–9:30 AM and 11:30 AM–3:30 PM IST, and all
weekends) cost half. Re-running the same files is cheaper (cached input). OCR costs nothing extra.

---

## 5. OCR (scanned pages)

1. Text pages are read directly – no OCR.
2. Scanned pages, pages that are mostly an image, and pages whose only text is a digital-signature
   stamp go to **OCR.space** (if `OCR_SPACE_API_KEY` is set). Scanned PDFs often carry the
   scanner's own hidden text; where that text is scrambled (common in tables) the page is read
   again by OCR, and if many pages are scrambled every scanned page of that file is re-read.
3. If OCR.space fails for a page, **Tesseract** (English + Hindi, installed in the server image)
   reads it instead. After 3 failures in a row OCR.space is paused for 10 minutes.
4. Sideways pages are straightened; "Digitally signed by… / Signature Not Verified" text is removed.
5. `OCR_ENGINE=tesseract` uses Tesseract only. OCR results are cached.

**Images** (PNG, JPG, WEBP, BMP, TIFF incl. multi-page) and **pictures inside Word files** use the
same OCR – no AI "vision" call, so they cost no AI credits. Before OCR each image is turned
upright (phone orientation; sideways pictures are retried turned, locally), converted to
greyscale and resized to an OCR-friendly size (small screenshots enlarged, huge photos reduced);
table rows are kept on one line. Logos and icons are skipped. Uploaded photos over 1.5 MB are
stored as a smaller greyscale copy to save space.

**Hard-to-read pages (handwriting, phone photos, poor scans)** – a 3-step ladder:

| Step | What happens | Cost |
|---|---|---|
| 1. OCR.space | Cloud OCR reads the page | free tier |
| 2. Tesseract | Reads it if OCR.space fails (and retries sideways pictures) | free, on the server |
| 3. AI reading | If the OCR text looks like noise (quality check on every OCR'd page), the page image is sent – small and greyscale – to the AI, which copies exactly what is written and marks anything unreadable as *[illegible]* instead of guessing | about ₹0.1 per page with DeepSeek Flash |

Step 3 is limited to `AI_IMAGE_MAX_PAGES` pages per tender (default 8) and can be turned off with
`AI_IMAGE_READING=off`. Clean pages and blank pages never go to the AI. Pages read this way are
marked "Read by AI" on the review sheet, their quotes are checked like any other page, and the
tokens are included in the run's usage. The AI provider must accept images (DeepSeek V4.1 Flash
and Gemini do); text-only providers are skipped for this step without affecting the synopsis.

---

## 6. Deploying on Railway

Full steps: **DEPLOY_RAILWAY.md**. In short:

1. Push the project to a **private** GitHub repo (`.env` and `company_profile.json` are ignored).
2. Railway → New Project → Deploy from GitHub. The `Dockerfile` installs Python + Tesseract.
3. Add a **volume mounted at `/data`** – it holds the database and all synopses.
4. Add the variables below → Deploy → Settings → Networking → Generate domain.
5. Open the site: create the master password (or set `MASTER_PASSWORD` first).
6. Set a usage limit in Railway (e.g. $10). One app normally fits the Hobby plan's included usage.

To update: push to GitHub; Railway rebuilds. A synopsis running during a redeploy is marked failed –
run it again.

**Variables**

| Variable | Purpose |
|---|---|
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | Main AI provider |
| `LLM2_…`, `LLM3_…`, `LLM4_…` | Backup providers (same three variables each) |
| `LLM_MAX_INPUT_TOKENS` | Text sent in one call (e.g. `120000`; above it, long tenders are condensed) |
| `LLM_JSON_MODE` | Leave unset (auto) unless told otherwise |
| `LLM_PRICE_IN`, `LLM_PRICE_OUT`, `LLM_PRICE_CACHED` | Main provider's price in US$ per 1M tokens (input, output, cached input). Shows the ₹ cost of each synopsis (result page, History, review sheet) and the month's total on Home. `LLM2_PRICE_…` etc. for backups |
| `USD_INR` | Exchange rate used for the ₹ cost (default `88`) |
| `GEMINI_KEY_HEADER` | Only for Google keys starting `AQ.` if Gemini rejects them (`bearer` / `header`) |
| `OCR_SPACE_API_KEY` | Cloud OCR (optional – Tesseract is always the backup) |
| `OCR_ENGINE`, `OCR_WORKERS`, `OCR_SPACE_LANGUAGE`, `OCR_SPACE_ENGINE` | OCR tuning (optional) |
| `AI_IMAGE_READING`, `AI_IMAGE_MAX_PAGES` | AI reading of hard-to-read pages (default `on`, `8` pages per tender) |
| `MASTER_PASSWORD` | Sets the first master password |
| `SECRET_KEY` | Optional; otherwise generated and stored in the database |
| `DATA_DIR` | Storage folder (`/data` in the Docker image) |
| `JOB_WORKERS` | Synopses at the same time (`1` recommended) |
| `KEEP_DAYS` | Days of history kept (default 90) |
| `MAX_UPLOAD_MB` | Upload limit (default 100) |
| `SESSION_HOURS` | Login length (default 12) |
| `ENABLE_BID_CHECK` | Keep off on the cloud (see section 9) |

---

## 7. What is stored

All on the `/data` volume:

| Location | Contents |
|---|---|
| `/data/app.db` (SQLite) | Master-password **hash** and session key; one row per synopsis (date, title, file names, mode, status, provider, tokens, check counts) |
| `/data/jobs/<id>/input/` | Uploaded tender files |
| `/data/jobs/<id>/out/` | Word synopsis, review sheet, data (.json), checks and documents lists |

Not stored: API keys (only in Railway Variables) and company figures. See everything from the
**History** page; delete old jobs there or set `KEEP_DAYS`. Railway → Service → Volumes shows usage.

---

## 8. Customise

| To change | Edit |
|---|---|
| Organisation name, header line, verticals (used for "relevant to") | `organisation.json` – no code |
| Writing style, highlight rules, accuracy rules | `tender_synopsis/prompts.py` → `HOUSE_RULES` |
| Sections per tender type, Compact / Detailed lengths | `tender_synopsis/prompts.py` → `SECTION_LIBRARY`, `DETAIL_LEVELS` |
| Document types and sectors offered to the AI | `tender_synopsis/config.py` |
| Style references | Approved synopses (.docx) in `style_examples/` (up to 4 used) |
| Word look (fonts, colours, header) | `tender_synopsis/render.py` |
| Website colours | `webapp/static/app.css` |

---

## 9. Running locally (optional)

```
pip install -r requirements.txt            # Python 3.10+
copy .env.example .env                     # Mac/Linux: cp; then fill in LLM_API_KEY etc.
python -m flask --app wsgi run             # website  (pip install -r requirements-web.txt)
streamlit run app.py                       # older single-user app
```

For OCR on your own computer install Tesseract (Windows: UB-Mannheim build, tick *Hindi*;
Ubuntu: `sudo apt install tesseract-ocr tesseract-ocr-hin`). Windows shortcuts:
`setup_windows.bat` once, then `run_windows.bat`.

**Command line**
```
python -m tender_synopsis generate nit.pdf corrigendum.pdf -o output --mode detailed
python -m tender_synopsis generate nit.pdf --mode compact
python -m tender_synopsis rerender output/nit_synopsis.json --tender nit.pdf   # after editing the JSON
python -m tender_synopsis text nit.pdf                                        # see what the AI reads
python -m tender_synopsis check                                               # test the AI key
```

**Bid / no-bid check (local only):** copy `company_profile.example.json` to
`company_profile.json` and fill in your companies' figures. Each synopsis then gets a
`*_bid_check.docx` comparing every requirement with your figures (✅ met · ❌ not met · ⚠️ check ·
❔ data missing). This comparison is plain Python on your computer – company data is never sent
to an AI. It is **off on the website** (`ENABLE_BID_CHECK`) so company figures never go to the cloud.

**Tests:** `python -m pytest tests` (offline; the AI is mocked).

---

## 10. Folder layout

```
wsgi.py, webapp/            website (Flask): login, new synopsis, history, settings
  db.py  auth.py  jobs.py  views.py  templates/  static/
tender_synopsis/            the engine (used by the website, Streamlit app and command line)
  ingest.py                 PDF / DOCX / image reading, OCR, file roles, linked-file detection
  condense.py               very long tenders: key pages in full + notes from the rest
  prompts.py                house rules, section library, Compact / Detailed
  schema.py                 data structure the AI must return
  llm.py                    AI call with backup providers
  verify.py                 accuracy checks
  render.py                 Word output
  pipeline.py               end-to-end flow, review sheet
  assess.py                 local bid / no-bid check
  config.py                 organisation settings, document types, sectors
app.py                      Streamlit app (local)
organisation.json           organisation name and verticals
Dockerfile, railway.json    Railway deployment (Python + Tesseract)
DEPLOY_RAILWAY.md           deployment guide
style_examples/  samples/  tests/
```

---

## 11. Known limits

- Always have a person read the review sheet (❌ and ⚠️) before sharing or bidding.
- Upload formats: PDF, Word (.docx), images, text. **Excel (BOQ) files are not read yet** – open
  BOQs separately.
- Images: screenshots read very well by OCR; handwriting and poor photos are passed to the AI
  (a few pages per tender). Text the AI marks *[illegible]* still needs a clearer copy.
- Some Hindi PDFs use old fonts whose text comes out garbled; such pages may need re-scanning or
  an English copy.
- Linked portal documents are not downloaded automatically – the app tells you which are missing.
- Subtle fine-print issues (a figure inside a form template, gaps in a marks table) are asked for
  but not always caught.
- Very long tenders are condensed; the review sheet lists skipped pages.

---

## 12. Troubleshooting

| Message | Fix |
|---|---|
| "Fallback used – … Free-tier limit reached" | A free backup was out of quota; the next one was used. If the **main** provider is skipped, check its balance / key |
| Review sheet says a free model wrote the synopsis | The main provider failed – see its reason in the ⚠️ "Fallback used" lines |
| "All AI providers failed" | Each provider's reason is listed – fix the first (usually key, model name or balance) |
| "Model … not found" | Copy the exact model ID from the provider's dashboard into `LLM_MODEL` |
| Gemini rejects a key starting `AQ.` | Set `GEMINI_KEY_HEADER=bearer` (or `header`), or create an `AIza…` key |
| "did not return a valid synopsis" | Retry, or use a stronger main model |
| "Amount looks wrong by a factor of 10…" ❌ | Lakh / crore conversion slip – correct it in *Edit before sharing* |
| "links to N other document(s)… not uploaded" | Download those files from the portal and run again with them |
| Password and history vanish after a redeploy | The `/data` volume is missing – add it in Railway |
| Synopsis marked failed after an update | It was running during the redeploy – run it again |
