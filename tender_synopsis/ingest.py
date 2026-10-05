"""Read tender files (PDF / DOCX / TXT) into page-numbered text.

- Text PDFs: text is extracted page by page (PyMuPDF).
- Scanned pages (little or no text): OCR.Space when configured, then Tesseract.
- Image pages that carry only a digital-signature stamp, and pages that are mostly one big
  image (scanned tables pasted into a text PDF), are OCR'd as well.
- Sideways (rotated) pages are straightened before reading so tables keep their rows.
- Digital-signature stamp text ("Signature Not Verified / Digitally signed by ...") is removed.
- Each file gets a role (main tender, corrigendum, special conditions, site list, standard
  conditions, general policy...) used to rank documents - see ROLES.
- Bilingual (Hindi + English) pages: Devanagari is dropped when the page is
  mostly English, because PDF extraction often garbles it (e.g. GeM bids).
  Hindi-dominant pages are kept as-is.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Garbled Hindi label before an English label, e.g. "  (  )/Bid Offer" -> "Bid Offer"
JUNK_PREFIX = re.compile(r"^(?:[^A-Za-z0-9\n]|\b[A-Za-z]\b)*?/(?=[A-Z])")
MIN_TEXT_CHARS = 60      # below this a PDF page is treated as scanned
OCR_DPI = 300


@dataclass
class Page:
    file: str
    number: int          # 1-based page number within its file
    text: str
    ocr: bool = False


# role -> (precedence: lower wins, label shown to the AI and in the review sheet)
ROLES = {
    "corrigendum": (1, "Corrigendum / addendum - overrides everything earlier"),
    "main": (2, "Main tender / NIT / RFP / auction catalogue"),
    "special": (3, "Special / additional / particular conditions for this tender"),
    "list": (3, "Site / item / quantity list, schedule, BOQ or feasibility sheet"),
    "standard": (5, "Standard / general conditions of contract (generic)"),
    "policy": (6, "General policy or guidelines (background only)"),
    "other": (4, "Other supporting document"),
}
_ROLE_RULES = [   # (role, pattern on file name, pattern on the first page)
    ("corrigendum", r"corrig|addend|amendment", r"\bcorrigendum\b|\baddendum\b"),
    ("policy", r"polic|guideline", r"^\W*(?:[\w&,.-]+\s+){0,6}(policy|guidelines)\b"),
    ("standard", r"standard.?cond|general.?cond|\bgcc\b|\bscc\b",
     r"(standard|general) conditions of contract"),
    ("special", r"special|\bspl|additional|particular", r"(special|additional|particular) conditions"),
    ("main", r"lot.?detail|catalog", r"(?!x)x"),
    ("list", r"list|schedule|feasib|\bboq\b|annex|\blot\b",
     r"\b(feasible|bill of quantities|schedule of)\b"),
    ("main", r"\bnit\b|notice|tender|\brfp\b|\brfq\b|\beoi\b|\bbid\b|\bgem\b|catalog",
     r"notice inviting|request for proposal|expression of interest|invitation for|"
     r"bid document|tender document|e-?auction catalogue|lot details|bid details"),
]


def classify(file: str, first_text: str) -> str:
    """Best guess of a file's role from its name, then its first page."""
    name = re.sub(r"[_\-.]+", " ", Path(file).stem.lower())
    head = (first_text or "")[:1500].lower()
    for role, by_name, _ in _ROLE_RULES:
        if re.search(by_name, name):
            return role
    for role, _, by_text in _ROLE_RULES:
        if re.search(by_text, head, re.M):
            return role
    return "other"


@dataclass
class TenderText:
    pages: list[Page] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)      # file name -> role key

    def role(self, file: str) -> str:
        return self.roles.get(file, "other")

    def documents_header(self) -> str:
        """List of files with their roles, most authoritative first (sent to the AI)."""
        files = list(dict.fromkeys(p.file for p in self.pages))
        if len(files) < 2:
            return ""
        files.sort(key=lambda f: ROLES[self.role(f)][0])
        rows = []
        for f in files:
            ps = [p for p in self.pages if p.file == f]
            rows.append(f"- {f} ({len(ps)} pages): {ROLES[self.role(f)][1]}")
        return ("DOCUMENTS IN THIS TENDER (most authoritative first):\n" + "\n".join(rows)
                + "\n\n")

    def as_prompt_text(self) -> str:
        """Text sent to the LLM, with page markers used for citations."""
        out = []
        multi = len({p.file for p in self.pages}) > 1
        for p in self.pages:
            tag = f"{p.file} | PAGE {p.number}" if multi else f"PAGE {p.number}"
            out.append(f"[[{tag}]]\n{p.text.strip()}")
        return self.documents_header() + "\n\n".join(out)

    def page_text(self, number: int, file: str | None = None) -> str:
        return "\n".join(p.text for p in self.pages
                         if p.number == number and (file is None or p.file == file))

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)


STAMP_START = re.compile(r"signature\s+not|digitally\s+signed|not\s+verified", re.I)
STAMP_LINE = re.compile(
    r"^\s*(signature\s+not(\s+verified)?|not\s+verified|verified|digitally\s+signed\s+by.*|"
    r"date:\s*\d{4}[.\-/]\d{2}[.\-/]\d{2}.*|\d{1,2}:\d{2}:\d{2}\s*(ist|[+-]\d{2}'?\d{2}'?)?|"
    r"reason:.*|location:.*|[A-Z][a-z]+(\s+[A-Z][a-z]+){0,3})\s*$", re.I)
STAMP_INLINE = re.compile(r"\s*\bSignature\s+Not(\s+Verified)?\b\s*", re.I)


def strip_signature_stamps(text: str) -> str:
    """Remove e-signature stamp blocks (lines near 'Signature Not Verified' / 'Digitally signed
    by'), and the stamp words that PDF extraction glues into ordinary lines."""
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if STAMP_START.search(ln)]
    drop = set()
    for s in starts:
        for j in range(max(0, s - 2), min(len(lines), s + 8)):
            short = re.sub(r"\s+", " ", lines[j]).strip()
            if len(short) < 70 and (STAMP_LINE.match(short) or re.match(r"(?i)reason:", short)):
                drop.add(j)
    for i, ln in enumerate(lines):           # stamp words glued into one line by extraction
        low = ln.lower()
        hits = sum(w in low for w in ("signature", "verified", "digitally", "signed", "reason:",
                                      "location:", "date:"))
        if (hits >= 3 or re.match(r"\s*reason:.*location:", low)) and len(ln) < 250:
            drop.add(i)
    out = [STAMP_INLINE.sub("  ", ln) if STAMP_START.search(ln) else ln
           for i, ln in enumerate(lines) if i not in drop]
    return "\n".join(out)


def _clean(text: str) -> str:
    """Drop garbled Devanagari from mostly-English pages; strip signature stamps; tidy whitespace."""
    if not isinstance(text, str):
        text = ""
    text = strip_signature_stamps(text)
    letters = re.findall(r"[A-Za-zऀ-ॿ]", text)
    dev = sum(1 for c in letters if DEVANAGARI.match(c))
    if letters and dev / len(letters) < 0.6:
        text = DEVANAGARI.sub("", text)
        text = re.sub(r"[‌‍]", "", text)
    lines = [JUNK_PREFIX.sub("", re.sub(r"[ \t]{2,}", "  ", ln)).rstrip()
             for ln in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


OCR_SPACE_MAX_BYTES = 1_000_000        # free plan limit is 1 MB per file


def _small_jpeg(png: bytes, limit: int = OCR_SPACE_MAX_BYTES - 50_000) -> bytes:
    """Re-encode a page image as greyscale JPEG under the OCR.Space free-plan size limit."""
    pix = pymupdf.Pixmap(png)
    if pix.n - pix.alpha > 1:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    for quality in (80, 65, 50):
        data = pix.tobytes("jpeg", jpg_quality=quality)
        if len(data) <= limit:
            return data
    while pix.width > 900:                     # still too big: halve the resolution
        pix.shrink(1)
        data = pix.tobytes("jpeg", jpg_quality=65)
        if len(data) <= limit:
            return data
    return data


# ---- OCR strategy -----------------------------------------------------------------------
# OCR_ENGINE=auto (default): OCR.space first when OCR_SPACE_API_KEY is set, Tesseract as backup.
# OCR_ENGINE=tesseract: local Tesseract only.   OCR_ENGINE=ocrspace: same as auto.
# Circuit breaker: after OCR_SPACE_MAX_FAILS failures in a row (rate limit, outage, bad key),
# OCR.space is skipped for OCR_SPACE_COOLDOWN seconds and every page goes straight to
# Tesseract, so one cloud outage cannot stall a 50-page scan page by page.
import threading as _threading
import time as _time

_breaker = {"fails": 0, "open_until": 0.0, "last_error": ""}
_breaker_lock = _threading.Lock()
MIN_OCR_TEXT = 3             # an empty or near-empty cloud result counts as a failure


def _cloud_allowed() -> bool:
    if os.getenv("OCR_ENGINE", "auto").lower() == "tesseract":
        return False
    if not os.getenv("OCR_SPACE_API_KEY", "").strip():
        return False
    with _breaker_lock:
        return _time.time() >= _breaker["open_until"]


def _cloud_result(ok: bool, error: str = "") -> None:
    with _breaker_lock:
        if ok:
            _breaker["fails"] = 0
            return
        _breaker["fails"] += 1
        _breaker["last_error"] = error[:200]
        if _breaker["fails"] >= int(os.getenv("OCR_SPACE_MAX_FAILS", "3")):
            _breaker["open_until"] = _time.time() + int(os.getenv("OCR_SPACE_COOLDOWN", "600"))
            _breaker["fails"] = 0


def ocr_status() -> dict:
    """For the app's status panel."""
    exe = _tesseract_executable()
    langs = []
    if exe:
        try:
            out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True,
                                 timeout=20).stdout
            langs = [ln.strip() for ln in out.splitlines()[1:] if ln.strip()]
        except Exception:
            pass
    with _breaker_lock:
        paused = max(0, int(_breaker["open_until"] - _time.time()))
        last = _breaker["last_error"]
    return {"engine": os.getenv("OCR_ENGINE", "auto").lower(),
            "ocr_space": bool(os.getenv("OCR_SPACE_API_KEY", "").strip()),
            "ocr_space_paused_seconds": paused, "ocr_space_last_error": last,
            "tesseract": bool(exe), "tesseract_langs": langs}


def _ocr_space(png: bytes) -> str:
    import httpx

    jpeg = _small_jpeg(png)
    response = httpx.post(
        "https://api.ocr.space/parse/image",
        data={"language": os.getenv("OCR_SPACE_LANGUAGE", "eng"),
              "isOverlayRequired": "false", "OCREngine": os.getenv("OCR_SPACE_ENGINE", "2"),
              "scale": "true", "isTable": "true"},
        files={"file": ("page.jpg", jpeg, "image/jpeg")},
        headers={"apikey": os.environ["OCR_SPACE_API_KEY"]},
        timeout=int(os.getenv("OCR_SPACE_TIMEOUT", "60")),
    )
    response.raise_for_status()
    data = response.json()
    parsed = data.get("ParsedResults") or []
    text = "\n".join((item.get("ParsedText") or "") for item in parsed
                      if isinstance(item, dict)).strip()
    if data.get("IsErroredOnProcessing") or not text:
        raise RuntimeError(data.get("ErrorMessage") or "OCR.Space returned no text.")
    return text


def _tesseract_executable() -> str | None:
    executable = shutil.which("tesseract")
    if executable:
        return executable
    if os.name == "nt":
        default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe"
        if default.is_file():
            return str(default)
    return None


def _ocr_png(png: bytes, lang: str) -> tuple[str, str | None]:
    cloud_error = None
    if _cloud_allowed():
        try:
            cloud_text = _ocr_space(png)
            if not isinstance(cloud_text, str) or len(cloud_text.strip()) < MIN_OCR_TEXT:
                raise RuntimeError("OCR.Space returned little or no text.")
            _cloud_result(True)
            return cloud_text.strip(), None
        except Exception as e:
            cloud_error = str(e)
            _cloud_result(False, cloud_error)

    executable = _tesseract_executable()
    if not executable:
        if cloud_error:
            raise RuntimeError(f"OCR.Space failed ({cloud_error}); Tesseract was not found.")
        raise RuntimeError("Tesseract OCR was not found. Add it to PATH or set OCR_SPACE_API_KEY.")
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "p.png"
        img.write_bytes(png)
        res = subprocess.run([executable, str(img), "stdout", "-l", lang, "--psm", "3"],
                             capture_output=True, text=True, timeout=300,
                             env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    if res.returncode:
        local_error = res.stderr.strip() or f"Tesseract exited with code {res.returncode}."
        if cloud_error:
            raise RuntimeError(f"OCR.Space failed ({cloud_error}); Tesseract failed ({local_error}).")
        raise RuntimeError(local_error)
    text = (res.stdout or "").strip()
    if not text:
        if cloud_error:
            raise RuntimeError(f"OCR.Space failed ({cloud_error}); Tesseract returned no text.")
        raise RuntimeError("Tesseract returned no text.")
    return text, (f"OCR.Space failed ({cloud_error}); Tesseract fallback used." if cloud_error else None)


def _available_ocr_lang() -> str:
    executable = _tesseract_executable()
    if not executable:
        return "eng"
    try:
        out = subprocess.run([executable, "--list-langs"], capture_output=True, text=True).stdout
        return "eng+hin" if "\nhin" in out else "eng"
    except (FileNotFoundError, subprocess.SubprocessError):
        return "eng"


CACHE_DIR = Path(tempfile.gettempdir()) / "tender_synopsis_cache"


def read_pdf(path: Path, result: TenderText) -> None:
    """Reads a PDF; OCR results are cached by file hash so re-runs are instant."""
    import hashlib
    import json
    digest = hashlib.sha256(path.read_bytes() + b"\0ocr-space-tesseract-fallback-v4-stamps-rotation-images").hexdigest()[:32]
    cache = CACHE_DIR / f"{digest}.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        # re-clean on load so improvements to cleaning apply to cached OCR results too
        result.pages += [Page(path.name, p["n"], _clean(p.get("t") or ""), p["ocr"])
                         for p in data["pages"]]
        result.warnings += data["warnings"]
        return
    before_p, before_w = len(result.pages), len(result.warnings)
    _read_pdf(path, result)
    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(json.dumps({
        "pages": [{"n": p.number, "t": p.text, "ocr": p.ocr} for p in result.pages[before_p:]],
        "warnings": result.warnings[before_w:]}), encoding="utf-8")


IMAGE_PAGE_SHARE = 0.35   # an image covering this much of a page may hide a scanned table
IMAGE_PAGE_MAX_TEXT = 1500  # ...but only OCR it when the page has little real text of its own


def _image_share(page) -> float:
    """Largest share of the page area covered by a single image."""
    area = abs(page.rect) or 1
    best = 0.0
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info.get("bbox", (0, 0, 0, 0))) & page.rect
            best = max(best, abs(r) / area)
    except Exception:
        pass
    return best


def _read_pdf(path: Path, result: TenderText) -> None:
    doc = pymupdf.open(path)
    texts, scanned, merge = {}, {}, set()
    rotated = 0
    for i, page in enumerate(doc, start=1):
        if page.rotation:                      # sideways pages: straighten so tables keep rows
            try:
                page.remove_rotation()
                rotated += 1
            except Exception:
                pass
        texts[i] = page.get_text("text", sort=True) or ""
        real = len(strip_signature_stamps(texts[i]).strip())
        if real < MIN_TEXT_CHARS:
            scanned[i] = page.get_pixmap(dpi=OCR_DPI).tobytes("png")
        elif real < IMAGE_PAGE_MAX_TEXT and _image_share(page) >= IMAGE_PAGE_SHARE:
            scanned[i] = page.get_pixmap(dpi=OCR_DPI).tobytes("png")
            merge.add(i)                       # keep the page's own text and add the OCR text
    if scanned:  # OCR scanned pages in parallel
        lang = _available_ocr_lang()
        # Keep cloud requests bounded to avoid bursts against OCR.Space free-tier limits.
        # Cloud hosts often report many CPUs but give few; each 300-dpi page image is ~25 MB.
        default = 2 if _cloud_allowed() else min(os.cpu_count() or 2, 3)
        workers = max(1, int(os.getenv("OCR_WORKERS", default)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {i: pool.submit(_ocr_png, png, lang) for i, png in scanned.items()}
            for i, fut in futures.items():
                try:
                    ocr_text, fallback = fut.result()
                    texts[i] = (texts[i].rstrip() + "\n\n[Text read from image on this page]\n"
                                + ocr_text) if i in merge else ocr_text
                    if fallback:
                        result.warnings.append(f"{path.name} p{i}: {fallback}")
                except Exception as e:  # keep going; flag it
                    scanned.pop(i)
                    what = "image on page not read" if i in merge else "OCR failed"
                    result.warnings.append(f"{path.name} p{i}: {what} ({e})")
    for i in sorted(texts):
        result.pages.append(Page(path.name, i, _clean(texts[i]), i in scanned))
    if rotated:
        result.warnings.append(f"{path.name}: {rotated} sideways page(s) straightened before reading.")
    n_ocr = sum(1 for p in result.pages if p.file == path.name and p.ocr)
    if n_ocr:
        result.warnings.append(f"{path.name}: {n_ocr} scanned page(s) read with OCR - "
                               "check figures carefully.")


def read_docx(path: Path, result: TenderText, words_per_page: int = 450) -> None:
    """DOCX has no fixed pages, so text is split into ~450-word pseudo-pages."""
    import docx  # python-docx
    d = docx.Document(path)
    blocks: list[str] = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            cells = []
            for c in row.cells:
                if not cells or c.text != cells[-1]:
                    cells.append(c.text.strip())
            blocks.append(" | ".join(cells))
    page, count, n = [], 0, 1
    for b in blocks:
        page.append(b)
        count += len(b.split())
        if count >= words_per_page:
            result.pages.append(Page(path.name, n, _clean("\n".join(page))))
            page, count, n = [], 0, n + 1
    if page:
        result.pages.append(Page(path.name, n, _clean("\n".join(page))))


_DOC_LINK = re.compile(r"\.pdf\b|download|biddoc|upload|\.docx?\b|\.xlsx?\b|\.zip\b", re.I)


def _linked_docs_warning(path: Path, result: TenderText) -> None:
    """Portal bids (GeM, IREPS, CPPP...) often keep the scope, terms or BOQ in linked files
    that are not part of the PDF. The app does not download them, so tell the user."""
    try:
        with pymupdf.open(path) as doc:
            links = [l["uri"] for page in doc for l in page.get_links()
                     if l.get("uri") and _DOC_LINK.search(l["uri"])]
    except Exception:                                      # noqa: BLE001 - optional hint only
        return
    links = list(dict.fromkeys(links))
    if links:
        result.warnings.append(
            f"{path.name} links to {len(links)} other document(s) (for example scope of work, "
            "payment terms, additional conditions or BOQ) that are NOT included in the PDF. "
            "Download them from the portal and add them to this synopsis, otherwise those "
            "details will be missing.")


def read_files(paths: list[str | Path]) -> TenderText:
    """Read one tender made of one or more files (main doc, corrigenda, annexures)."""
    result = TenderText()
    for p in map(Path, paths):
        ext = p.suffix.lower()
        if ext == ".pdf":
            read_pdf(p, result)
            _linked_docs_warning(p, result)
        elif ext == ".docx":
            read_docx(p, result)
        elif ext in (".txt", ".md"):
            result.pages.append(Page(p.name, 1, _clean(p.read_text(errors="ignore"))))
        else:
            result.warnings.append(f"{p.name}: unsupported file type, skipped")
    for f in dict.fromkeys(p.file for p in result.pages):
        first = "\n".join(p.text for p in result.pages if p.file == f)[:1500]
        result.roles[f] = classify(f, first)
    files = list(result.roles)
    if len(files) > 1 and not any(r in ("main", "corrigendum") for r in result.roles.values()):
        result.warnings.append(
            "No main tender / NIT / auction catalogue found among the files (" +
            ", ".join(f"{f}: {result.role(f)}" for f in files) + "). Key values such as "
            "reserve price, EMD, contract period and dates are often only in that document or "
            "on the portal's lot details - upload it if available.")
    if not result.full_text.strip():
        result.warnings.append("No text could be read from the tender.")
    return result
