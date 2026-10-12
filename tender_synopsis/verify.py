"""Checks that make the synopsis safe to share.

1. Quote check   - every key fact's quote must really exist on the cited page (fuzzy match,
                   tolerant of OCR noise / line breaks); searches the whole tender if the
                   page number is off.
2. Value check   - the numbers in the value must appear in the quote.
3. Date checks   - valid DD.MM.YYYY; deadline not before issue date.
4. Markup check  - highlight/bold markers are balanced.
5. Coverage      - core facts (deadline, EMD) present.
6. (optional) LLM second opinion on critical facts - see pipeline.llm_double_check.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime

from rapidfuzz import fuzz

from .ingest import TenderText
from .schema import Synopsis

QUOTE_OK = 88          # fuzzy score (0-100) needed for a quote to count as found


@dataclass
class Issue:
    severity: str      # "error" (must fix) | "warning" (check) | "info"
    field: str
    message: str
    page: int | None = None

    def to_dict(self):
        return asdict(self)


def _norm(s: str) -> str:
    s = s.lower()
    s = s.replace("–", "-").replace("—", "-").replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"').replace("₹", "rs.")
    s = re.sub(r"\brs\.?\s*", "rs ", s)
    return re.sub(r"\s+", " ", s).strip()


UNITS = {"crore": 10**7, "crores": 10**7, "cr": 10**7, "lakh": 10**5, "lakhs": 10**5,
         "lac": 10**5, "lacs": 10**5}


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"], 1)}
NUM_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")
MON3 = {m[:3]: i for m, i in MONTHS.items()}
_MON = r"(" + "|".join(MON3) + r")[a-z]*\.?"
TXT_DATE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(\d{4})\b")
TXT_DATE_MF = re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b")


def _dates_to_tokens(s: str) -> str:
    """'28.02.2027', '28th February 2027', '28 Feb 2027' and 'Feb 28, 2027' all become
    'date20270228'."""
    s = NUM_DATE.sub(lambda m: f" date{int(m[3]):04d}{int(m[2]):02d}{int(m[1]):02d} ", s)
    s = TXT_DATE.sub(lambda m: f" date{int(m[3]):04d}{MON3[m[2]]:02d}{int(m[1]):02d} ", s)
    return TXT_DATE_MF.sub(lambda m: f" date{int(m[3]):04d}{MON3[m[1]]:02d}{int(m[2]):02d} ", s)


# Page and clause references inside a value ("(p.15)", "ITC Cl.13", "S.No.7") are pointers,
# not figures from the tender - they must not be compared with the quote.
_REFS = re.compile(r"\(?\b(?:pp?|pages?|pg)\.?\s*\d+(?:\s*(?:[-–,&]|and)\s*\d+)*\)?"
                   r"|\b(?:cl(?:ause)?|item|s\.?\s*no|sl\.?\s*no|para|section|annex(?:ure)?|"
                   r"appendix|form|tech|fin)\.?\s*[-–]?\s*\d+[\w.()]*", re.I)


def _strip_refs(s: str) -> str:
    return _REFS.sub(" ", s)


_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10}
_UNIT = r"(crores?|cr|lakhs?|lacs?)\b"
FY = re.compile(r"\b((?:19|20)\d{2})\s*[-–/]\s*((?:19|20)?\d{2})\b")


def _fy_token(m) -> str:
    """'2022-23' and '2022-2023' are the same financial year -> 'fy20222023'."""
    start, end = int(m[1]), int(m[2][-2:])
    if (start + 1) % 100 != end:
        return m[0]                                    # not a financial year - leave as it is
    return f" fy{start}{start + 1} "


# "3:00 PM", "3.00 p.m.", "15:00 Hrs", "12.00 Noon", "15:00" - a "." only counts with a suffix
TIME = re.compile(r"\b(\d{1,2})(?::(\d{2})|\.(\d{2})(?=\s*(?:[ap]\.?m\b|noon|hrs?\b|hours)))"
                  r"\s*([ap])?\.?\s*m?\.?(?![a-z])\s*(noon)?")


def _time_token(m) -> str:
    """3:00 PM and 15:00 are the same time -> 'time1500'."""
    h, mins = int(m[1]), int(m[2] or m[3])
    if m[4] == "p" and h < 12:
        h += 12
    elif m[4] == "a" and h == 12:
        h = 0
    if h > 23 or mins > 59:
        return m[0]
    return f" time{h:02d}{mins:02d} "


def _ocr_digits(s: str) -> str:
    """In scanned text, 'O' / 'Z' inside a number are usually 0 / 2 ('2OZ6' -> '2026')."""
    return re.sub(r"[0-9oz]{4,}", lambda m: m[0].translate(str.maketrans("oz", "02"))
                  if sum(c.isdigit() for c in m[0]) >= 2 else m[0], s)


def _numbers(s: str, ocr: bool = False) -> set[str]:
    """Significant numbers in a string, commas removed; '4 crore' -> '40000000'.
    ocr=True also repairs common scan slips inside numbers (use it for quotes and page text)."""
    s = s.lower()
    s = re.sub(r"(?<=\d),(?=\d{2,3}\b)", "", s)        # 1,00,000 -> 100000 (Indian grouping)
    s = s.replace(",", " ")                            # '2022-23,2023-24' stays two numbers
    if ocr:
        s = _ocr_digits(s)
    # common OCR slips next to Indian units: "I crore" / "l crore" -> "1 crore", "laes" -> "lacs"
    s = re.sub(r"(?<![a-z0-9])[il|]\s+" + _UNIT, r"1 \1", s)
    s = re.sub(r"\b(la[ce]s|laes|lacks|lkh)\b", "lacs", s)
    # "Rs. 1.00 (one) Lakh" / "One (1) Lakh" / "one lakh" -> number right next to its unit
    s = re.sub(r"(\d)\)?\s*\([a-z ]+\)\s*" + _UNIT, r"\1 \2", s)
    s = re.sub(r"\b(" + "|".join(_WORD_NUM) + r")\s*\((\d+(?:\.\d+)?)\)\s*" + _UNIT, r"\2 \3", s)
    s = re.sub(r"\b(" + "|".join(_WORD_NUM) + r")\s+" + _UNIT,
               lambda m: f"{_WORD_NUM[m[1]]} {m[2]}", s)
    s = _dates_to_tokens(s)
    out = set(re.findall(r"date\d{8}", s))
    s = re.sub(r"date\d{8}", " ", s)
    s = TIME.sub(_time_token, s)
    out |= set(re.findall(r"time\d{4}", s))
    s = re.sub(r"time\d{4}", " ", s)
    s = FY.sub(_fy_token, s)
    out |= set(re.findall(r"fy\d{8}", s))
    s = re.sub(r"fy\d{8}", " ", s)

    def to_rupees(m):
        return str(int(round(float(m[1]) * UNITS[m[2]])))
    s = re.sub(r"(\d+(?:\.\d+)?)\s*" + _UNIT, to_rupees, s)
    for n in re.findall(r"\d+(?:\.\d+)?", s):
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n.lstrip("0") or "0")
    return out


def _best_match(quote: str, text: str) -> int:
    """Fuzzy substring match; falls back to word coverage for table layouts where
    PDF extraction interleaves columns (all quote words present nearby = match).
    A quote joined with '...' must match part by part (the weakest part counts)."""
    if not quote.strip() or not text.strip():
        return 0
    parts = [p for p in re.split(r"\.{3,}|…", quote) if len(p.strip()) >= 12]
    if len(parts) > 1:
        return min(_best_match(p, text) for p in parts)
    q, t = _norm(quote), _norm(text)
    score = int(fuzz.partial_ratio(q, t))
    if score < QUOTE_OK:
        words = [w for w in re.findall(r"[a-z0-9.%]+", q) if len(w) > 1]
        page_words = set(re.findall(r"[a-z0-9.%]+", t))
        if words and sum(w in page_words for w in words) / len(words) >= 0.95:
            score = max(score, QUOTE_OK)
    return score


def _best_anywhere(quote: str, tender: TenderText) -> int:
    """Best match over every 3-page window of every file. Matching against the whole tender
    at once would let a reworded quote 'match' words scattered over many pages."""
    best = 0
    by_file: dict[str, list] = {}
    for p in tender.pages:
        by_file.setdefault(p.file, []).append(p)
    for pages in by_file.values():
        pages.sort(key=lambda p: p.number)
        for i in range(len(pages)):
            window = "\n".join(p.text for p in pages[max(0, i - 1): i + 2])
            best = max(best, _best_match(quote, window))
            if best >= QUOTE_OK:
                return best
    return best


def _inr(n: str) -> str:
    """290000000 -> Rs 29,00,00,000 (Indian grouping), for messages people read."""
    try:
        v = int(float(n))
    except ValueError:
        return n
    t = str(v)
    head, tail = t[:-3], t[-3:]
    while len(head) > 2:
        tail, head = head[-2:] + "," + tail, head[:-2]
    return "₹" + (head + "," + tail if head else tail)


def _scale_slip(value_nums: set[str], quote_nums: set[str]) -> tuple[str, str] | None:
    """A value that is the quote's number x10 / x100 / x1000 (or the reverse) is almost always
    a lakh / crore conversion mistake, e.g. 290000000 written as Rs 2,90,00,000."""
    def as_int(n):
        try:
            return int(float(n))
        except ValueError:
            return 0
    for v in value_nums:
        a = as_int(v)
        if a < 1000:
            continue
        for q in quote_nums:
            b = as_int(q)
            if b >= 1000 and a != b and any(a == b * 10 ** k or b == a * 10 ** k for k in (1, 2, 3)):
                return v, q
    return None


_ABSENT = ("not prescribed", "not applicable", "not disclosed", "not stated", "not mentioned",
           "no provision", "silent")


def _scanned_near(tender: TenderText, page: int, file: str | None) -> bool:
    """Is the cited page (or a neighbour) a scan, or text that is visibly scrambled?"""
    from .ingest import text_layer_jumbled
    near = [p for p in tender.pages if p.number in (page - 1, page, page + 1)
            and (file is None or p.file == file)]
    return any(p.ocr or text_layer_jumbled(p.text) for p in near)


def _unmatched_quote(tender: TenderText, page: int, file: str | None, value: str,
                     field_name: str, plain: str) -> Issue:
    """Quote wording not found. On a scanned page, when every figure of the value IS on the
    page, the wording was probably garbled by the scan: a check-it warning, not an error."""
    near = "\n".join(tender.page_text(p, file) for p in (page - 1, page, page + 1))
    figures = {n for n in _numbers(value) if len(n) > 1}
    if figures and _scanned_near(tender, page, file) and figures <= _numbers(near, ocr=True):
        return Issue("warning", field_name,
                     f"Could not match the exact wording on page {page} (scanned page), but the "
                     f"figures are there - glance at page {page} to confirm: {plain}", page or None)
    return Issue("error", field_name, f"Source quote not found - verify: {plain}", page or None)


def check_quotes(syn: Synopsis, tender: TenderText) -> list[Issue]:
    issues = []
    for kf in syn.key_facts:
        if kf.page <= 0 or not kf.quote.strip() or _norm(kf.quote) in (
                "not prescribed", "not applicable", "not disclosed", "not stated"):
            continue                       # nothing quoted: the value says it is absent
        f = kf.file or None
        page_txt = "\n".join(tender.page_text(p, f) for p in (kf.page - 1, kf.page, kf.page + 1))
        score = _best_match(kf.quote, page_txt)
        if score < QUOTE_OK:
            whole = _best_anywhere(kf.quote, tender)
            if whole >= QUOTE_OK:
                issues.append(Issue("info", kf.field, f"Quote found, but not on page {kf.page}.",
                                    kf.page))
            else:
                issues.append(_unmatched_quote(tender, kf.page, f, kf.value, kf.field,
                                               f"'{kf.value}' (match {max(score, whole)}%)."))
                continue
        quote_nums = _numbers(kf.quote, ocr=True)
        missing = {n for n in _numbers(_strip_refs(kf.value)) - quote_nums if len(n) > 1}
        if missing and re.search(r"conflict|\bvs\.?\b", kf.value, re.I):
            # a conflict value names the other clause's figure too: it only has to exist
            # somewhere in the documents, not in this one quote
            missing -= _numbers(tender.full_text, ocr=True)
        slip = _scale_slip(missing, quote_nums)
        if slip:
            issues.append(Issue("error", kf.field,
                                f"Amount looks wrong by a factor of 10 or more: the document says "
                                f"{_inr(slip[1])}, the synopsis says {_inr(slip[0])}. "
                                "Check the lakh / crore conversion.", kf.page))
        elif missing and "computed" in kf.value.lower():
            issues.append(Issue("info", kf.field, f"'{kf.value}' is a calculated total - "
                                "re-add the list to confirm.", kf.page))
        elif missing:
            issues.append(Issue("warning", kf.field,
                                f"Value '{kf.value}' has numbers not in its quote: "
                                f"{', '.join(sorted(_pretty(n) for n in missing))}.", kf.page))
    return issues


def check_requirements(syn: Synopsis, tender: TenderText) -> list[Issue]:
    """Requirement quotes must exist, and the rupee amount must match the quote."""
    issues = []
    for i, r in enumerate(syn.requirements, 1):
        name = f"requirement {i} ({r.category})"
        if not r.quote.strip():
            issues.append(Issue("warning", name, f"No source quote for: {r.text[:80]}", r.page or None))
            continue
        if _norm(r.quote).startswith(_ABSENT):
            continue                       # "not stated in the documents" - nothing to find
        near = "\n".join(tender.page_text(p, r.file or None) for p in (r.page - 1, r.page, r.page + 1))
        if max(_best_match(r.quote, near), _best_anywhere(r.quote, tender)) < QUOTE_OK:
            issues.append(_unmatched_quote(tender, r.page, r.file or None,
                                           r.text + (f" {int(r.amount_inr)}" if r.amount_inr else ""),
                                           name, r.text[:80]))
            continue
        if r.amount_inr:
            amt = str(int(round(r.amount_inr)))
            slip = _scale_slip({amt}, _numbers(r.quote, ocr=True))
            if slip:
                issues.append(Issue("error", name, f"Amount looks wrong by a factor of 10 or "
                                    f"more: the document says {_inr(slip[1])}, the synopsis "
                                    f"says {_inr(slip[0])}. Check the lakh / crore conversion.",
                                    r.page or None))
            elif amt not in _numbers(r.quote, ocr=True):
                issues.append(Issue("warning", name, f"Amount {amt} not found in its quote "
                                    f"'{r.quote[:80]}' - check the conversion.", r.page or None))
    return issues


def _pretty(n: str) -> str:
    if n.startswith("fy"):
        return f"FY {n[2:6]}-{n[8:10]}"
    if n.startswith("time"):
        return f"{n[4:6]}:{n[6:8]}"
    return f"{n[10:12]}.{n[8:10]}.{n[4:8]}" if n.startswith("date") else n


DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")


def _first_date(s: str):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    try:
        return datetime(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return "invalid"


def check_dates(syn: Synopsis) -> list[Issue]:
    issues = []
    p = syn.profile
    issued, deadline = _first_date(p.date_of_issue), _first_date(p.submission_deadline)
    for name, d in (("date_of_issue", issued), ("submission_deadline", deadline)):
        if d == "invalid":
            issues.append(Issue("error", name, "Not a valid calendar date."))
    if isinstance(issued, datetime) and isinstance(deadline, datetime) and deadline < issued:
        issues.append(Issue("error", "submission_deadline", "Deadline is before the issue date."))
    if isinstance(deadline, datetime) and deadline < datetime.now():
        issues.append(Issue("warning", "submission_deadline",
                            f"The bid closing date ({p.submission_deadline}) has already passed. "
                            "Check the portal for an extension or a fresh tender before working on it."))
    return issues


def _all_strings(syn: Synopsis):
    yield from (v for row in syn.key_facts_table for v in row)
    for s in syn.sections:
        yield s.heading
        for b in s.blocks:
            yield b.text
            yield from (v for row in b.rows for v in row)
            for it in b.items:
                yield it.text
                yield from it.sub_items
    yield syn.summary


def _balance(s: str) -> str:
    """Drop the last unmatched **, == or ++ so a broken marker cannot spill highlighting."""
    for m in ("**", "==", "++"):
        if s.count(m) % 2:
            i = s.rfind(m)
            s = s[:i] + s[i + len(m):]
    return s


def fix_markup(syn: Synopsis) -> int:
    """Repair unbalanced highlight/bold markers in place. Returns how many strings changed."""
    fixed = 0

    def fx(v: str) -> str:
        nonlocal fixed
        new = _balance(v)
        fixed += new != v
        return new
    syn.key_facts_table = [[fx(c) for c in row] for row in syn.key_facts_table]
    for sec in syn.sections:
        sec.heading = fx(sec.heading)
        for b in sec.blocks:
            b.text = fx(b.text)
            b.rows = [[fx(c) for c in row] for row in b.rows]
            for it in b.items:
                it.text = fx(it.text)
                it.sub_items = [fx(x) for x in it.sub_items]
    syn.summary = fx(syn.summary)
    return fixed


def check_empty(syn: Synopsis) -> list[Issue]:
    """Headings with nothing under them usually mean the AI used an unexpected format."""
    issues = []
    for sec in syn.sections:
        has = any(b.text.strip() or b.rows or any(i.text.strip() for i in b.items)
                  for b in sec.blocks)
        empty_items = sum(1 for b in sec.blocks for i in b.items if not i.text.strip())
        if not has:
            issues.append(Issue("warning", "content", f"Section '{sec.heading}' is empty in the "
                                "synopsis - re-run or fill it in before sharing."))
        elif empty_items:
            issues.append(Issue("warning", "content", f"{empty_items} empty bullet(s) in "
                                f"'{sec.heading}' - check the Word file."))
    return issues


def check_markup(syn: Synopsis) -> list[Issue]:
    bad = [s for s in _all_strings(syn) if any(s.count(m) % 2 for m in ("**", "==", "++"))]
    return [Issue("warning", "formatting", f"Unbalanced highlight/bold marker in: '{s[:70]}'")
            for s in bad]


def check_coverage(syn: Synopsis) -> list[Issue]:
    fields = " ".join(k.field.lower() for k in syn.key_facts)
    issues = []
    for need, label in (("deadline", "submission deadline"), ("emd", "EMD / bid security")):
        if need not in fields and need not in _norm(" ".join(_all_strings(syn))):
            issues.append(Issue("warning", need, f"No {label} captured - confirm it is truly absent."))
    heads = " ".join(sec.heading.lower() + " " + sec.key.lower() for sec in syn.sections)
    if "date" not in heads and "schedule" not in heads:
        issues.append(Issue("warning", "content", "No 'Important Dates' section - check the "
                            "dates were not left out."))
    if not syn.requirements and "eligib" not in heads and "qualif" not in heads:
        issues.append(Issue("warning", "content", "No eligibility criteria captured - check the "
                            "tender for qualifying conditions."))
    return issues


# House-style highlight by TYPE of detail (the AI is told the same in prompts.py; this fixes
# what it forgets). Yellow (==) = decides bid / no-bid; cyan (++) = other financial thresholds.
_TURNOVER = re.compile(r"turnover", re.I)
_FIN = re.compile(r"net[ -]?worth|solvency|banker'?s? cert|similar work.{0,40}(?:₹|rs\.?|value)"
                  r"|project[- ]value", re.I)
_DEADLINE = re.compile(r"last date|due date|deadline|bid end|closing date|bid submission|"
                       r"submission (?:date|deadline)", re.I)
_JV = re.compile(r"\bjv\b|joint venture|consortium", re.I)
_MARKED = re.compile(r"(==|\+\+)(.+?)\1")
_MONEYISH = re.compile(r"₹|\brs\.?\s?\d|\blakh|\blac\b|\bcrore|\bnil\b|stipulated|as per (?:the )?(?:auction )?catalogue", re.I)
_THAT = re.compile(r"^(That|Which)\s+(?=[a-z])")


def _rule_for(text: str):
    """(marker, needs) for the first matching type; needs = value pattern a bullet must contain."""
    if _TURNOVER.search(text):
        return "==", _MONEYISH
    if _FIN.search(text):
        return "++", _MONEYISH
    return None


def ensure_highlights(syn: Synopsis) -> int:
    """Apply the house highlight colours the AI missed:
    minimum turnover, deadline, JV rule -> yellow; net worth / solvency / project value -> cyan."""
    changed = 0

    def recolour(v: str, marker: str, whole: bool, needs=None) -> str:
        nonlocal changed
        if not v:
            return v
        def seg(m):
            own = _rule_for(m.group(2))
            hit = whole or (own[0] == marker if own else bool(needs and needs.search(m.group(2))))
            return f"{marker}{m.group(2)}{marker}" if hit else m.group(0)
        new = _MARKED.sub(seg, v)
        if "==" not in new and "++" not in new and (whole or (needs and needs.search(new))):
            new = f"{marker}{new.strip()}{marker}"
        changed += new != v
        return new

    def row_rule(label: str):
        if _DEADLINE.search(label) or _JV.search(label):
            return "=="
        r = _rule_for(label)
        return r[0] if r else None

    def fix_rows(rows):
        out = []
        for row in rows:
            m = row_rule(row[0]) if row else None
            out.append([recolour(c, m, True) for c in row] if m else
                       [cell(c) for c in row])
        return out

    def cell(v: str, summary: bool = False) -> str:
        r = _rule_for(v or "")
        if not r:
            return v
        marker, needs = r
        if summary:                     # only recolour parts already highlighted
            return recolour(v, marker, False, None)
        return recolour(v, marker, False, needs)

    syn.key_facts_table = fix_rows(syn.key_facts_table)
    for sec in syn.sections:
        for b in sec.blocks:
            b.text = cell(b.text)
            b.rows = fix_rows(b.rows)
            for it in b.items:
                it.text = cell(it.text)
                it.sub_items = [cell(x) for x in it.sub_items]
    syn.summary = cell(syn.summary, summary=True)
    return changed


ensure_cyan = ensure_highlights          # older name


def tidy_text(syn: Synopsis) -> None:
    """Drop stray leading 'That ...' some models put on bullets."""
    def t(v: str) -> str:
        m = _THAT.match(v or "")
        return v[m.end()].upper() + v[m.end() + 1:] if m else v
    for sec in syn.sections:
        for b in sec.blocks:
            for it in b.items:
                it.text = t(it.text)
                it.sub_items = [t(x) for x in it.sub_items]


def mark_closed_bid(syn: Synopsis) -> bool:
    """If the closing date has passed, say so in the Word file itself (top of the Quick Decision
    table, or the summary) - not only on the review sheet."""
    deadline = _first_date(syn.profile.submission_deadline)
    if not isinstance(deadline, datetime) or deadline >= datetime.now():
        return False
    note = f"No - closed on {deadline:%d.%m.%Y}; check the portal for an extension or re-tender"
    for sec in syn.sections:
        for b in sec.blocks:
            if b.rows and b.headers and "question" in b.headers[0].lower():
                if not any("still open" in r[0].lower() for r in b.rows if r):
                    b.rows.insert(0, ["==Bid still open?==", f"=={note}=="])
                return True
    if "closed" not in syn.summary.lower():
        syn.summary = syn.summary.rstrip() + f" ==Note: the bid closed on {deadline:%d.%m.%Y}.=="
    return True


# ---- Figures inside tables and bullets ---------------------------------------------------------
# Key facts and requirements carry quotes; table cells and bullets do not. This check makes
# sure every money amount, percentage and period written there exists somewhere in the
# documents, so an invented or mistyped figure (e.g. "valid 90 days" when the tender says 60,
# a lakh / crore slip in a table) is flagged even without a quote.
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
              "eighteen": 18, "twenty": 20, "twenty four": 24, "thirty": 30, "forty five": 45,
              "sixty": 60, "ninety": 90, "one hundred twenty": 120, "one hundred eighty": 180}
_N = r"(\d+(?:\.\d+)?|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
DURATION = re.compile(_N + r"\s*(?:\(\s*[a-z0-9 ]+\s*\)\s*)?(?:calendar\s+|working\s+)?"
                      r"(day|week|month|year)s?\b")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\(\s*[a-z ]+\s*\)\s*)?(?:%|per\s*cent|percent)")


def _num(word: str) -> str:
    v = _NUM_WORDS.get(word)
    return str(v) if v is not None else word.rstrip("0").rstrip(".") if "." in word else word


AMOUNT = re.compile(r"(?:₹|\brs\.?|\binr)\s*[\d,]+(?:\.\d+)?(?:\s*/-)?(?:\s*\([a-z ]+\))?"
                    r"(?:\s*(?:crores?|cr|lakhs?|lacs?)\b)?"
                    r"|[\d,]+(?:\.\d+)?\s*(?:\([a-z ]+\)\s*)?(?:crores?|lakhs?|lacs?)\b"
                    r"|(?:₹|\brs|\binr)[\s.,]*[|il]\s+(?:crores?|lakhs?|lacs?)\b")   # scan: "Rs. | crore"


def _figures(s: str, ocr: bool = False) -> set[str]:
    """Money amounts (in rupees), percentages and periods in a piece of text, normalised.
    Only numbers written as money (₹ / Rs / INR / lakh / crore) count as amounts, so reference
    numbers, bid numbers and counts are never mistaken for amounts."""
    low = re.sub(r"\s+", " ", s.lower().replace("-", " ").replace("–", " "))
    if ocr:
        low = _ocr_digits(low)
    out = {f"{_num(m[1])} {m[2]}" for m in DURATION.finditer(low)}
    out |= {f"{_num(m[1])}%" for m in PERCENT.finditer(low)}
    for m in AMOUNT.finditer(s.lower()):
        out |= {n for n in _numbers(m[0], ocr=ocr) if n.isdigit() and int(n) >= 100}
    return out


def _cells(syn: Synopsis):
    """(where, text) for every table cell and bullet a reader sees, except source columns."""
    for row in syn.key_facts_table:
        if len(row) >= 2:
            yield f"Key facts - {row[0]}", " ".join(row[1:])
    for sec in syn.sections:
        for b in sec.blocks:
            src = {i for i, h in enumerate(b.headers or []) if re.search(r"source|page|ref", h, re.I)}
            for row in b.rows or []:
                text = " ".join(c for i, c in enumerate(row) if i not in src and i > 0)
                if row:
                    yield f"{sec.heading} - {row[0]}", text
            for it in b.items or []:
                yield sec.heading, " ".join([it.text] + list(it.sub_items or []))


def _loosely_present(fig: str, low: str) -> bool:
    """Forms and portal data sheets put the unit in a label and the number after it
    ('ePBG Percentage(%) 5.00', 'Duration of ePBG required (Months). 14', '45 (Days)')."""
    num, _, unit = fig.partition(" ")
    if fig.endswith("%"):
        num, unit = fig[:-1], "%"
    n = re.escape(num) + r"(?:\.0+)?\b"
    u = {"%": r"(?:%|per\s*cent|percent(?:age)?)", "day": r"days?", "week": r"weeks?",
         "month": r"months?", "year": r"years?"}.get(unit)
    if not u:
        return False
    return bool(re.search(u + r"\)?\.?\s*:?[^.;]{0,80}?(?<![\d.])" + n, low)
                or re.search(r"(?<![\d.])" + n + r"[^.;]{0,25}?" + u, low))


def check_table_figures(syn: Synopsis, tender: TenderText) -> list[Issue]:
    in_tender = _figures(tender.full_text, ocr=True)
    low = re.sub(r"\s+", " ", _ocr_digits(tender.full_text.lower()))
    places: dict[str, list[str]] = {}
    for where, text in _cells(syn):
        if "computed" in text.lower():
            continue                                     # a labelled calculation
        for f in _figures(text):
            if f not in in_tender and not (f in places or _loosely_present(f, low)):
                places.setdefault(f, [])
            if f in places:
                places[f].append(re.sub(r"[=+*]", "", where)[:70])
    issues = []
    for f, where in places.items():
        uniq = list(dict.fromkeys(where))
        shown = (_inr(f) if f.isdigit() else
                 f + ("s" if f[-1].isalpha() and not f.startswith("1 ") else ""))
        more = f" (+{len(uniq) - 2} more)" if len(uniq) > 2 else ""
        issues.append(Issue("warning", "table figure",
                            f"'{shown}' is not in the documents - check it. Used in: "
                            f"{'; '.join(uniq[:2])}{more}. If it is a calculation, it "
                            "should say 'computed'."))
    return issues


# ---- Compact length -----------------------------------------------------------------------
COMPACT_MAX_WORDS = 1700        # about 5 Word pages of tables (measured: ~340 words per page)
WORDS_PER_PAGE = 340


def synopsis_words(syn: Synopsis) -> int:
    """Words a reader sees in the Word file (top table, sections, summary)."""
    parts = [" ".join(r) for r in syn.key_facts_table]
    for sec in syn.sections:
        parts.append(sec.heading)
        for b in sec.blocks:
            parts += [b.text or "", " ".join(b.headers or [])]
            parts += [" ".join(r) for r in b.rows or []]
            parts += [" ".join([it.text] + list(it.sub_items or [])) for it in b.items or []]
    parts.append(syn.summary or "")
    return len(re.findall(r"\S+", re.sub(r"[=+*]", "", " ".join(parts))))


def check_compact_length(syn: Synopsis) -> list[Issue]:
    words = synopsis_words(syn)
    if words <= COMPACT_MAX_WORDS:
        return []
    pages = max(5, round(words / WORDS_PER_PAGE))
    # a check, not a cut: nothing is removed automatically, because a missing detail costs more
    return [Issue("warning", "length", f"This compact synopsis is about {pages} pages "
                  f"({words:,} words) - longer than compact should be (3-4, at most 5). Look for "
                  "repeated rows or long wording to trim on the results page; nothing was "
                  "removed automatically.")]


def run_checks(syn: Synopsis, tender: TenderText) -> list[Issue]:
    repaired = fix_markup(syn)
    tidy_text(syn)
    mark_closed_bid(syn)
    cyan = ensure_highlights(syn)
    pre = [Issue("info", "formatting", f"{repaired} broken highlight/bold marker(s) repaired "
                 "automatically.")] if repaired else []
    if cyan:
        pre.append(Issue("info", "formatting", f"{cyan} item(s) given the house-style "
                         "highlight (turnover, deadline, JV, net worth…)."))
    return pre + _run_checks(syn, tender)


def _run_checks(syn: Synopsis, tender: TenderText) -> list[Issue]:
    quotes = (check_quotes(syn, tender) if tender.pages else
              [Issue("info", "sources", "Tender not supplied - source quotes not checked.")])
    if tender.pages:
        quotes += check_requirements(syn, tender) + check_table_figures(syn, tender)
    issues = quotes + check_empty(syn) + check_dates(syn) + check_markup(syn) + check_coverage(syn)
    issues += [Issue("warning", "ingest", w) for w in tender.warnings]
    issues += [Issue("warning", "model_note", n) for n in syn.review_notes]
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(issues, key=lambda i: order[i.severity])
