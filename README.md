# Tender Synopsis Generator (v2)

Upload a tender (PDF or DOCX, including scanned PDFs, corrigenda and annexures) and get a
ready-to-share Word synopsis in the house style, with a review sheet that lists anything to check.

```
Tender files → Read (text + OCR) → Condense if long → AI extracts structured JSON → Checks
            → Word synopsis + review sheet → Bid / no-bid check against your company profile
```

Works for any kind of tender: works, supply, services, consultancy, GeM, EOI, empanelment, PPP,
licence / advertising rights, auction, lease, IBC Form G, disinvestment, multi-volume RFPs.

### What's new in v2
| Gap in v1 | Fix |
|---|---|
| Long / multi-volume tenders overflowed the model | `condense.py`: key pages (first pages, corrigenda, highest-scoring pages for dates, EMD, eligibility, scope…) go in word for word; other relevant pages are read in parts as notes with page + quote; irrelevant pages (drawings, BOQ rows, blank formats) are skipped and listed in the review sheet |
| Fixed lists of tender types / sectors / Prabhatam verticals | Free-text classification with suggestions; organisation name, header and verticals in `organisation.json` |
| No eligibility assessment | AI lists every requirement (amount, count, years, keywords, page, quote); `assess.py` compares them **on this computer** with `company_profile.json` → `*_bid_check.docx/.md` per group company |
| One provider = one point of failure | Up to 3 **fallback providers** (`LLM2_` … `LLM4_` in `.env`, e.g. Groq, OpenRouter, Mistral): any failure moves the request to the next one; providers out of daily quota or with a bad key are skipped for the rest of the run; the review sheet says which provider was used |
| Several documents for one tender (NIT, special conditions, site lists, standard conditions, policies) were treated as equal | Each file gets a **role**; the AI is told corrigendum > main tender/catalogue > special conditions & site lists > standard conditions > policy, uses policies only as background, writes "As per lot details (not uploaded)" instead of "Not prescribed", and flags mismatches between site lists. Long tenders read tender-specific pages in full first. The review sheet and app list each file with its role |
| Scanned tables inside text PDFs, sideways pages, e-signature stamps | Pages whose only text is a signature stamp, or that are mostly one big image, are OCR'd; sideways pages are straightened; "Signature Not Verified / Digitally signed by…" text is removed. A warning appears if no main tender / NIT / catalogue was uploaded |
| Free models broke the strict JSON format | Fallback chain (strict schema → JSON mode → plain text), lenient parsing, automatic repair rounds with the validation errors, waits and retries on rate limits |

## Web version (Flask, for Railway)
A team web app with a master-password login, history and live progress lives in `webapp/` +
`wsgi.py`. Run locally: `pip install -r requirements-web.txt` then `python -m flask --app wsgi run`.
Deploy: see **DEPLOY_RAILWAY.md**.

## 1. Setup (once)

1. Install **Python 3.10+**.
2. (Optional) Install **Tesseract OCR** as the local fallback for scanned PDFs:
   - Windows: install from https://github.com/UB-Mannheim/tesseract/wiki (tick *Hindi* under additional languages).
   - Ubuntu: `sudo apt install tesseract-ocr tesseract-ocr-hin`
3. In this folder:
   ```
   pip install -r requirements.txt
   copy .env.example .env        (Mac/Linux: cp .env.example .env)
   ```
4. Open `.env` and paste your API key into `LLM_API_KEY`. Optionally fill one to three fallback
   providers (`LLM2_…`, `LLM3_…`, `LLM4_…` – see the FALLBACK block in `.env.example`); the
   **Test AI connection** button checks every provider. For a **free** start use Google Gemini
   (Option A in `.env.example`; key from https://aistudio.google.com/apikey, no card needed).
5. For scanned PDFs, add an OCR.Space API key to `.env` as `OCR_SPACE_API_KEY=your-key`.
   Get a key at https://ocr.space/ocrapi. OCR.Space is tried first; if it fails or is not
   configured, the app uses Tesseract locally when available. Set `OCR_SPACE_LANGUAGE=eng`
   to select the cloud OCR language; OCR.Space accepts one language code per request.
   Page images are compressed to stay under OCR.Space's 1 MB free-plan limit;
   `OCR_SPACE_ENGINE` (default 2) picks the OCR.Space engine.

**Windows shortcut:** double-click `setup_windows.bat` once, then `run_windows.bat` every time.

## 2. Use it

**Web app (for the team):**
```
streamlit run app.py
```
Opens in the browser → upload tender file(s) → **Generate** → read the checks → edit if needed → **Download .docx**.

**Command line:**
```
python -m tender_synopsis generate tender.pdf corrigendum.pdf -o output --mode detailed
python -m tender_synopsis generate tender.pdf --mode compact
python -m tender_synopsis rerender output/tender_synopsis.json --tender tender.pdf   # after editing the JSON
python -m tender_synopsis text tender.pdf                                           # see what the AI reads
```

Each run writes: `*_synopsis.docx` (to share), `*_synopsis_review.md` (checks and
sources), `*_synopsis.json` (editable data) and, when a company profile exists,
`*_synopsis_bid_check.docx` / `.md` (internal eligibility check).

```
python -m tender_synopsis assess output/tender_synopsis.json            # re-run the bid check only
python -m tender_synopsis generate tender.pdf --profile other_profile.json
```

## 2a. Bid / no-bid check (optional)

1. Copy `company_profile.example.json` to `company_profile.json` and fill in each group company:
   turnover and profit by year, net worth, incorporation date, registrations, offices,
   employees, past works (value, completion date, keywords).
2. That's it – every synopsis now comes with a bid check. In the web app you can also upload a
   profile file in the sidebar.

**Privacy:** the AI only reads the tender. The comparison with company figures is plain Python on
this computer; `company_profile.json` is never sent to any AI provider (and is in `.gitignore`).

Statuses: ✅ met · ❌ not met · ⚠️ check (e.g. "similar work" wording needs judgement) ·
❔ data missing in profile · ℹ️ info (JV rule, EMD, exemptions). Alternatives such as
"1 work of 80% / 2 of 60% / 3 of 40%" pass if any one passes. Not automated: escalation of past
project values, percentage-based thresholds without a stated base – these show as ⚠️.

## 3. What the synopsis contains

- **Auto-detected profile:** title, one-line topic, document type (NIT, GeM, RFP, EOI, PPP, licence
  / rights, e-auction, IBC Form G… or any other accurate label), sector, commercial model
  (L1 / QCBS / H1 / zero-cost PPP), authority, reference number, portal, dates, value, location and
  which of your **verticals** (from `organisation.json`) it fits.
- **Key facts table**, then numbered sections chosen for the tender type (core sections always,
  extra sections for GeM, EOI, PPP, advertising rights, IBC and so on) and a **✅ In Summary** paragraph.
- **Highlights:** yellow = critical items (deadline, EMD, JV rule, contract period…),
  cyan = financial eligibility thresholds (turnover, net worth…).

## 4. How accuracy is protected

| Check | What it does |
|---|---|
| Source quote | Every key date and amount carries a page number and exact quote; the code confirms the quote exists in the tender |
| Number match | The numbers in each value must appear in its quote (₹4 Crore = 4,00,00,000; dates in any format) |
| Second AI check | A second, cheap call re-checks critical figures against their source pages |
| Dates | Valid dates; deadline after issue date; flags past deadlines |
| Model notes | The AI must report conflicts, typos, corrigenda and missing information |
| OCR warning | Scanned pages are flagged so figures get a human look |

Nothing is guessed: missing items show as "Not prescribed" / "To be notified".

## 5. Customise without coding much

| To change | Edit |
|---|---|
| Organisation name, header line, verticals | `organisation.json` |
| Company figures for the bid check | `company_profile.json` (local only) |
| Long-tender limits, retries, JSON mode | `.env` (see the optional block in `.env.example`) |
| Writing style / highlight rules | `tender_synopsis/prompts.py` → `HOUSE_RULES` |
| Sections per tender type | `tender_synopsis/prompts.py` → `SECTION_LIBRARY` |
| Style references | Drop approved synopses (.docx) into `style_examples/` (up to 4 are used) |
| AI model / provider | `.env` → `LLM_MODEL`, `LLM_BASE_URL` (any OpenAI-compatible API) |
| Word look (fonts, colours, header) | `tender_synopsis/render.py` (top of file) |

## 6. Cost and speed (GPT-6 Luna)

About ₹1–3 per tender (50-page tender ≈ 25k tokens in). Text PDFs take under a minute; scanned
PDFs need about 2 seconds per page for OCR (results are cached).

## 7. Folder layout

```
app.py                     web app
tender_synopsis/
  ingest.py                PDF/DOCX reading, OCR, Hindi clean-up, cache
  condense.py              long-tender handling (key pages + notes)
  assess.py                local bid / no-bid check against company_profile.json
  config.py                organisation settings
  prompts.py               house rules + section library
  schema.py                JSON structure the AI must return
  llm.py                   provider-agnostic AI call
  verify.py                accuracy checks
  render.py                Word output
  pipeline.py              end-to-end flow
style_examples/            team's approved synopses (style references)
samples/                   test tenders and example outputs
tests/                     offline tests: python -m pytest tests
```

## 8. Known limits

- Always have a person read the review sheet before sharing; the checks catch most errors, not all.
- Long tenders: the review sheet lists skipped pages – glance at them if something seems missing.
- The bid check is only as good as `company_profile.json`; "similar work" is matched by keywords.
- GeM bids reference separate attachments (specifications, ATC); upload them with the bid.
- Very poor scans may OCR badly; the review sheet flags them.

## 9. Troubleshooting

| Message | Fix |
|---|---|
| "Please pass a valid API key" / "rejected the API key" | Double-click `check_key_windows.bat` (or click **Test AI connection** in the app). It shows the first/last 4 characters and length of the key being used. Gemini keys start with `AIza` and are 39 characters. Keep exactly one `LLM_API_KEY=` line in `.env`. |
| "Gemini keys start with 'AIza'…" for a key starting `AQ.` | Fixed in this version – `AQ.` is Google's new key format and is accepted. If Gemini still rejects it, set `GEMINI_KEY_HEADER=bearer` (or `header`) in `.env`, or create an `AIza` key in Google Cloud Console → APIs & Services → Credentials, restricted to the Generative Language API. |
| "No API key found" | The key line is missing or still has the placeholder text. |
| "Model … not found" | Put a current model name after `LLM_MODEL=` (copy it from AI Studio / OpenRouter). |
| "Free-tier limit reached" | The app already waits and retries per-minute limits; daily limits reset the next day. |
| "All AI providers failed" | Each provider's reason is listed – fix the first one (usually a key or model name), or wait for the daily limit to reset. |
| "did not return a valid synopsis" | Retry, set `LLM_JSON_MODE=json`, or use a stronger model. |

The `.env` file is re-read on every run, so you don't need to restart the app after editing it.
