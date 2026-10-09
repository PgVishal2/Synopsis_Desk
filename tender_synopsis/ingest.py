"""Read tender files (PDF / DOCX / TXT / images) into page-numbered text.

- Text PDFs: text is extracted page by page (PyMuPDF).
- Scanned pages (little or no text): OCR.Space when configured, then Tesseract.
- Image pages that carry only a digital-signature stamp, and pages that are mostly one big
  image (scanned tables pasted into a text PDF), are OCR'd as well.
- Scanned pages whose built-in scanner text is scrambled (common in tables) are read again
  with OCR instead of trusting that text.
- Sideways (rotated) pages are straightened before reading so tables keep their rows.
- Digital-signature stamp text ("Signature Not Verified / Digitally signed by ...") is removed.
- Images (screenshots, photos, scans: PNG, JPG, WEBP, BMP, TIFF incl. multi-page) are
  straightened, converted to greyscale, resized to an OCR-friendly size and read with the same
  OCR as scanned pages - no AI "vision" call, so they cost no AI credits.
- Pictures pasted inside Word files (e.g. a scanned annexure) are read the same way.
- Last resort for pages OCR cannot read reliably (handwriting, phone photos, poor scans):
  the page image is read by the AI (vision), for a few pages per tender only - see
  AI_IMAGE_READING / AI_IMAGE_MAX_PAGES.
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
    ai_usage: dict = field(default_factory=dict)              # tokens spent reading images by AI
    ai_pages: int = 0                                         # pages read by AI this run
    ai_budget: int = 0                                        # pages AI may still read this run

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


def _ocr_png(png: bytes, lang: str, cloud: bool = True, psm: str = "3") -> tuple[str, str | None]:
    cloud_error = None
    if cloud and _cloud_allowed():
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
        res = subprocess.run([executable, str(img), "stdout", "-l", lang, "--psm", psm],
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
    _cached(path, result, _read_pdf, b"ocr-space-tesseract-fallback-v6-stamps-rotation-images-rescan")


def read_image(path: Path, result: TenderText) -> None:
    """Reads an image file with OCR; cached like PDFs."""
    _cached(path, result, _read_image, b"image-v2-psm4")


def _cached(path: Path, result: TenderText, reader, tag: bytes) -> None:
    import hashlib
    import json
    tag += b"-ai" if _ai_budget() else b""             # AI-read text differs from OCR-only text
    digest = hashlib.sha256(path.read_bytes() + b"\0" + tag).hexdigest()[:32]
    cache = CACHE_DIR / f"{digest}.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        # re-clean on load so improvements to cleaning apply to cached OCR results too
        result.pages += [Page(path.name, p["n"], _clean(p.get("t") or ""), p["ocr"])
                         for p in data["pages"]]
        result.warnings += data["warnings"]
        return
    before_p, before_w = len(result.pages), len(result.warnings)
    reader(path, result)
    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(json.dumps({
        "pages": [{"n": p.number, "t": p.text, "ocr": p.ocr} for p in result.pages[before_p:]],
        "warnings": result.warnings[before_w:]}), encoding="utf-8")


IMAGE_PAGE_SHARE = 0.35   # an image covering this much of a page may hide a scanned table
IMAGE_PAGE_MAX_TEXT = 1500  # ...but only OCR it when the page has little real text of its own
FULL_PAGE_SCAN = 0.85     # one image covering the whole page = a scan (its text is the scanner's)
JUMBLED_SHARE = 0.10      # share of stray single letters that marks a scrambled text layer
REDO_ALL_SHARE = 0.2      # this share of scrambled pages = re-read every scanned page of the file


def text_layer_jumbled(text: str) -> bool:
    """True when a scanner's built-in text is scrambled ('S art d a o I d own o a d'), which
    happens in tables. Normal text has under 4% stray single letters; scrambled pages 10-30%."""
    words = re.findall(r"[A-Za-z]+", text)
    if len(words) < 40:
        return False
    stray = sum(1 for w in words if len(w) == 1 and w.lower() not in ("a", "i"))
    return stray / len(words) >= JUMBLED_SHARE


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

    def render(i: int) -> bytes:
        """Page image for OCR, made only when needed (a 300-dpi page is ~4 MB; a long
        scanned tender held in memory at once could exhaust a small server)."""
        return doc[i - 1].get_pixmap(dpi=OCR_DPI).tobytes("png")

    texts, scanned, merge, failed = {}, {}, set(), set()
    original = {}                              # scanner's own text, kept in case OCR fails
    full_scan, jumbled = set(), set()          # whole-page scans / those with scrambled text
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
        share = _image_share(page)
        if real >= MIN_TEXT_CHARS and share >= FULL_PAGE_SCAN:
            full_scan.add(i)
            if text_layer_jumbled(texts[i]):
                jumbled.add(i)
        if real < MIN_TEXT_CHARS:
            scanned[i] = None
        elif real < IMAGE_PAGE_MAX_TEXT and share >= IMAGE_PAGE_SHARE:
            scanned[i] = None
            merge.add(i)                       # keep the page's own text and add the OCR text
    # Scanned PDFs carry the scanner's own text. Where it is scrambled, read the page again.
    # If many pages are scrambled, the scanner's text is poor throughout (dropped letters too),
    # so every scanned page of this file is read again.
    redo = full_scan if full_scan and len(jumbled) >= REDO_ALL_SHARE * len(full_scan) else jumbled
    for i in redo:
        original[i] = texts[i]
        merge.discard(i)                       # replace the scanner's text, don't add to it
        scanned[i] = None
    if scanned:  # OCR scanned pages in parallel
        lang = _available_ocr_lang()
        # Keep cloud requests bounded to avoid bursts against OCR.Space free-tier limits.
        # Cloud hosts often report many CPUs but give few; each 300-dpi page image is ~25 MB.
        default = 2 if _cloud_allowed() else min(os.cpu_count() or 2, 3)
        workers = max(1, int(os.getenv("OCR_WORKERS", default)))
        order = sorted(scanned)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(order), workers * 2):    # a few page images at a time
                batch = order[start:start + workers * 2]
                futures = {i: pool.submit(_ocr_png, render(i), lang) for i in batch}
                for i, fut in futures.items():
                    try:
                        ocr_text, fallback = fut.result()
                        texts[i] = (texts[i].rstrip() + "\n\n[Text read from image on this page]\n"
                                    + ocr_text) if i in merge else ocr_text
                        if fallback:
                            result.warnings.append(f"{path.name} p{i}: {fallback}")
                    except Exception as e:  # keep going; flag it
                        if i in original:      # keep the scanner's own text rather than nothing
                            texts[i] = original[i]
                            scanned.pop(i)
                            result.warnings.append(
                                f"{path.name} p{i}: page text looks scrambled and could not be "
                                f"read again ({e}) - check this page.")
                        elif i not in merge:
                            texts[i] = ""      # whole page unreadable: AI may still read it below
                            failed.add(i)
                        else:
                            scanned.pop(i)
                            result.warnings.append(f"{path.name} p{i}: image on page not read ({e})")
        # pages OCR could not read reliably (handwriting, phone photo, poor scan): AI reads a few
        for i in sorted(scanned):
            if i in merge:
                continue
            text = strip_signature_stamps(texts[i])
            if i not in failed and len(text.strip()) >= 40 and not ocr_looks_unreliable(text):
                continue                       # clear text: no need to look at the image again
            img = _pil(render(i))
            if not ((i in failed and _ink(img) > 0.03)
                    or ocr_looks_unreliable(strip_signature_stamps(texts[i]), img)):
                if i in failed:
                    result.warnings.append(f"{path.name} p{i}: blank page or no text found.")
                continue
            ai = _ai_read(img, result, f"{path.name} p{i}")
            if ai:
                texts[i] = ai
            elif i in failed:
                result.warnings.append(f"{path.name} p{i}: OCR failed - page not read.")
    for i in sorted(texts):
        result.pages.append(Page(path.name, i, _clean(texts[i]), i in scanned))
    if rotated:
        result.warnings.append(f"{path.name}: {rotated} sideways page(s) straightened before reading.")
    n_ocr = sum(1 for p in result.pages if p.file == path.name and p.ocr)
    if n_ocr:
        result.warnings.append(f"{path.name}: {n_ocr} scanned page(s) read with OCR - "
                               "check figures carefully.")


# ---- Images ------------------------------------------------------------------------------
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
IMG_MAX_SIDE = 3500       # ~A4 at 300 dpi; larger photos are shrunk (no accuracy gain beyond)
IMG_MIN_WIDTH = 1800      # small screenshots are enlarged so OCR sees bigger letters
IMG_MIN_SIDE = 300        # smaller than this is a logo / icon, not a document
IMG_MAX_FRAMES = 60       # pages read from one multi-page TIFF
DOCX_MAX_IMAGES = 15      # pictures OCR'd from one Word file
_ROTATIONS = (90, 270, 180)


def _prepare_image(img):
    """Upright, greyscale, OCR-sized copy of one image frame (Pillow image) or None."""
    from PIL import Image, ImageOps
    try:
        img = ImageOps.exif_transpose(img)        # phone photos taken sideways
    except Exception:
        pass
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(bg, img)
    img = img.convert("L")
    w, h = img.size
    if min(w, h) < IMG_MIN_SIDE:
        return None
    if max(w, h) > IMG_MAX_SIDE:
        img.thumbnail((IMG_MAX_SIDE, IMG_MAX_SIDE), Image.LANCZOS)
    elif w < IMG_MIN_WIDTH:
        f = min(2.5, IMG_MIN_WIDTH / w)
        img = img.resize((int(w * f), int(h * f)), Image.LANCZOS)
    return img


def _png(img) -> bytes:
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _ocr_image(img, lang: str) -> tuple[str, str | None]:
    """OCR one prepared image. If almost nothing is read, the picture may be sideways without
    orientation info: retry turned 90 / 270 / 180 degrees with local Tesseract only (free)."""
    # psm 4 keeps each table row on one line (label next to its value) in screenshots
    text, note = _ocr_png(_png(img), lang, psm="4")
    if len(text.strip()) >= 40 or not _tesseract_executable():
        return text, note
    best = text
    for angle in _ROTATIONS:
        try:
            t, _ = _ocr_png(_png(img.rotate(angle, expand=True)), lang, cloud=False, psm="4")
        except Exception:
            continue
        if len(t.strip()) > len(best.strip()):
            best = t
        if len(best.strip()) >= 40:
            break
    return best, note


# ---- Hard-to-read pages: OCR quality check, then AI reading for a few pages --------------
_GOOD_WORD = re.compile(r"[(\[\"'‘“]?[A-Za-z0-9₹ऀ-ॿ][A-Za-z0-9₹ऀ-ॿ.,/:%×&'’-]*[)\].,:;।'’\"”]*")


def _pil(png: bytes):
    import io
    from PIL import Image
    return Image.open(io.BytesIO(png))


def _ink(img) -> float:
    """Share of dark pixels in a greyscale image (how much is written on the page)."""
    hist = img.convert("L").histogram()
    return sum(hist[:128]) / max(1, sum(hist))


def ocr_looks_unreliable(text: str, img=None) -> bool:
    """True when OCR output looks like noise: handwriting, blurred photos, poor scans.
    A blank page is not unreliable - there is simply nothing on it."""
    t = text.strip()
    words = [w for w in t.split() if re.search(r"\w", w)]
    if len(t) < 40:
        return img is not None and _ink(img) > 0.03       # writing on the page, almost no text
    good = sum(1 for w in words if _GOOD_WORD.fullmatch(w) and len(re.sub(r"\W", "", w)) >= 2
               or w.isdigit())
    chars = re.sub(r"\s", "", t)
    alnum = sum(c.isalnum() for c in chars) / max(1, len(chars))
    short = sum(len(re.sub(r"\W", "", w)) <= 2 for w in words) / max(1, len(words))
    return good / max(1, len(words)) < 0.55 or alnum < 0.65 or (len(words) > 20 and short > 0.5)


def _ai_budget() -> int:
    if os.getenv("AI_IMAGE_READING", "on").lower() in ("0", "off", "false", "no"):
        return 0
    return max(0, int(os.getenv("AI_IMAGE_MAX_PAGES", "8")))


def _jpeg_for_ai(img) -> bytes:
    """Small greyscale JPEG: enough detail for the AI, few tokens."""
    import io
    im = img.convert("L")
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _ai_read(img, result: TenderText, where: str) -> str | None:
    """Read one page image with the AI. Returns the text, or None (budget used up / failed)."""
    if result.ai_budget <= 0:
        result.warnings.append(f"{where}: OCR could not read this reliably (handwriting / photo "
                               "/ poor scan) and the AI reading limit for this tender was reached "
                               "- check this page yourself or upload a clearer copy.")
        return None
    from . import llm
    result.ai_budget -= 1
    try:
        text, usage = llm.vision_transcribe(_jpeg_for_ai(img))
    except Exception as e:                                  # noqa: BLE001 - keep the run going
        result.warnings.append(f"{where}: OCR could not read this reliably and the AI could not "
                               f"read it either ({str(e)[:120]}) - upload a clearer scan or photo.")
        return None
    result.ai_usage = llm._merge_usage(result.ai_usage, usage)
    result.ai_pages += 1
    result.warnings.append(f"{where}: OCR could not read this reliably (handwriting / photo / "
                           "poor scan), so the AI read it - check its figures.")
    return "[Read by AI from a hard-to-read image - check figures]\n" + text


def _read_image(path: Path, result: TenderText) -> None:
    from PIL import Image, ImageSequence
    try:
        im = Image.open(path)
        frames = [f.copy() for f in ImageSequence.Iterator(im)][:IMG_MAX_FRAMES]
    except Exception as e:
        result.warnings.append(f"{path.name}: image could not be opened ({e}).")
        return
    lang = _available_ocr_lang()
    n_read = 0
    for i, frame in enumerate(frames, start=1):
        img = _prepare_image(frame)
        if img is None:
            result.warnings.append(f"{path.name} p{i}: image too small to read, skipped.")
            continue
        try:
            text, note = _ocr_image(img, lang)
        except Exception as e:
            text, note = "", f"OCR failed ({e})"
        if ocr_looks_unreliable(text, img):
            text = _ai_read(img, result, f"{path.name} p{i}") or text
        if note:
            result.warnings.append(f"{path.name} p{i}: {note}")
        if not text.strip():
            continue
        result.pages.append(Page(path.name, i, _clean(text), True))
        n_read += 1
    if n_read:
        result.warnings.append(f"{path.name}: image read with OCR ({n_read} page(s)) - "
                               "check figures carefully.")


def _docx_images(d) -> list[bytes]:
    """Picture files inside a Word document, largest first, duplicates removed."""
    seen, out = set(), []
    for rel in d.part.rels.values():
        if "image" not in rel.reltype:
            continue
        try:
            blob = rel.target_part.blob
        except Exception:
            continue
        if blob[:64] in seen:
            continue
        seen.add(blob[:64])
        out.append(blob)
    return sorted(out, key=len, reverse=True)[:DOCX_MAX_IMAGES]


def _docx_image_text(path: Path, d, result: TenderText) -> list[str]:
    """OCR the pictures pasted in a Word file (scanned annexures, tables as images)."""
    import io
    try:
        from PIL import Image
    except ImportError:
        return []
    texts, lang = [], None
    for blob in _docx_images(d):
        try:
            img = _prepare_image(Image.open(io.BytesIO(blob)))
        except Exception:
            continue
        if img is None:
            continue                               # logos, icons, signatures
        lang = lang or _available_ocr_lang()
        try:
            t, _ = _ocr_image(img, lang)
        except Exception:
            t = ""
        if ocr_looks_unreliable(t, img):
            t = _ai_read(img, result, f"{path.name} (picture {len(texts) + 1})") or t
        if len(t.strip()) >= 20:
            texts.append(t)
    if texts:
        result.warnings.append(f"{path.name}: {len(texts)} picture(s) inside the document read "
                               "with OCR - check figures carefully.")
    return texts


def shrink_image_upload(path: Path) -> None:
    """Storage saver for uploaded photos: shrink very large images (and save as greyscale JPEG
    when that is much smaller). Keeps every detail OCR needs; screenshots are left alone."""
    if path.suffix.lower() not in IMAGE_EXTS or path.stat().st_size < 1_500_000:
        return
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            if getattr(im, "n_frames", 1) > 1:
                return                              # multi-page TIFF: keep as is
            img = ImageOps.exif_transpose(im).convert("L")
        if max(img.size) > IMG_MAX_SIDE:
            img.thumbnail((IMG_MAX_SIDE, IMG_MAX_SIDE), Image.LANCZOS)
        tmp = path.with_suffix(".tmp.jpg")
        img.save(tmp, "JPEG", quality=88, optimize=True)
        if tmp.stat().st_size < path.stat().st_size * 0.7:
            tmp.replace(path)                       # same name, so links / roles still match
        else:
            tmp.unlink()
    except Exception:
        pass                                        # never block an upload over this


def read_docx(path: Path, result: TenderText) -> None:
    """Word file; cached because pictures inside it may need OCR."""
    _cached(path, result, _read_docx, b"docx-v2-pictures")


def _read_docx(path: Path, result: TenderText, words_per_page: int = 450) -> None:
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
        n += 1
    pics = _docx_image_text(path, d, result)
    if pics:
        result.pages.append(Page(path.name, n, _clean(
            "[Text read from pictures in this document]\n\n" + "\n\n---\n\n".join(pics)), True))


_DOC_LINK = re.compile(r"\.pdf\b|download|biddoc|upload|\.docx?\b|\.xlsx?\b|\.zip\b", re.I)


def _linked_docs(path: Path) -> list[str]:
    """Portal bids (GeM, IREPS, CPPP...) often keep the scope, terms or BOQ in linked files
    that are not part of the PDF. The app does not download them, so the user is told."""
    try:
        with pymupdf.open(path) as doc:
            links = [l["uri"] for page in doc for l in page.get_links()
                     if l.get("uri") and _DOC_LINK.search(l["uri"])]
    except Exception:                                      # noqa: BLE001 - optional hint only
        return []
    return list(dict.fromkeys(links))


def _missing_links(links: dict[str, list[str]], uploaded: list[str]) -> list[str]:
    """Warnings for linked documents whose file was not uploaded alongside."""
    stems = [Path(n).stem.lower() for n in uploaded]
    out = []
    for name, urls in links.items():
        missing = []
        for u in urls:
            m = re.search(r"([\w-]{6,})\.(?:pdf|docx?|xlsx?|zip)\b", u, re.I)
            key = (m.group(1) if m else "").lower()
            if not key or not any(key in st or st in key for st in stems if len(st) >= 6):
                missing.append(u)
        if missing:
            out.append(f"{name} links to {len(missing)} other document(s) (for example scope of "
                       "work, payment terms, additional conditions or BOQ) that were not uploaded. "
                       "Download them from the portal and add them to this synopsis, otherwise "
                       "those details will be missing.")
    return out


def read_files(paths: list[str | Path]) -> TenderText:
    """Read one tender made of one or more files (main doc, corrigenda, annexures)."""
    result = TenderText(ai_budget=_ai_budget())
    links: dict[str, list[str]] = {}
    for p in map(Path, paths):
        ext = p.suffix.lower()
        if ext == ".pdf":
            read_pdf(p, result)
            links[p.name] = _linked_docs(p)
        elif ext == ".docx":
            read_docx(p, result)
        elif ext in IMAGE_EXTS:
            read_image(p, result)
        elif ext in (".txt", ".md"):
            result.pages.append(Page(p.name, 1, _clean(p.read_text(errors="ignore"))))
        else:
            result.warnings.append(f"{p.name}: unsupported file type, skipped")
    result.warnings += _missing_links(links, [Path(p).name for p in paths])
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
