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
TXT_DATE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")[a-z]*,?\s+(\d{4})\b")


def _dates_to_tokens(s: str) -> str:
    """'28.02.2027' and '28th February 2027' both become 'date20270228'."""
    s = NUM_DATE.sub(lambda m: f" date{int(m[3]):04d}{int(m[2]):02d}{int(m[1]):02d} ", s)
    return TXT_DATE.sub(lambda m: f" date{int(m[3]):04d}{MONTHS[m[2]]:02d}{int(m[1]):02d} ", s)


def _numbers(s: str) -> set[str]:
    """Significant numbers in a string, commas removed; '4 crore' -> '40000000'."""
    s = s.replace(",", "").lower()
    # common OCR slips next to Indian units: "I crore" / "l crore" -> "1 crore", "laes" -> "lacs"
    s = re.sub(r"\b[il|]\s+(crores?|cr|lakhs?|lacs?)\b", r"1 \1", s)
    s = re.sub(r"\b(la[ce]s|laes|lacks|lkh)\b", "lacs", s)
    s = _dates_to_tokens(s)
    out = set(re.findall(r"date\d{8}", s))
    s = re.sub(r"date\d{8}", " ", s)

    def to_rupees(m):
        return str(int(round(float(m[1]) * UNITS[m[2]])))
    s = re.sub(r"(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?)\b", to_rupees, s)
    for n in re.findall(r"\d+(?:\.\d+)?", s):
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n.lstrip("0") or "0")
    return out


def _best_match(quote: str, text: str) -> int:
    """Fuzzy substring match; falls back to word coverage for table layouts where
    PDF extraction interleaves columns (all quote words present nearby = match)."""
    if not quote.strip() or not text.strip():
        return 0
    q, t = _norm(quote), _norm(text)
    score = int(fuzz.partial_ratio(q, t))
    if score < QUOTE_OK:
        words = [w for w in re.findall(r"[a-z0-9.%]+", q) if len(w) > 1]
        page_words = set(re.findall(r"[a-z0-9.%]+", t))
        if words and sum(w in page_words for w in words) / len(words) >= 0.95:
            score = max(score, QUOTE_OK)
    return score


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
            whole = _best_match(kf.quote, tender.full_text)
            if whole >= QUOTE_OK:
                issues.append(Issue("info", kf.field, f"Quote found, but not on page {kf.page}.",
                                    kf.page))
            else:
                issues.append(Issue("error", kf.field,
                                    f"Source quote not found in tender (match {max(score, whole)}%). "
                                    f"Verify '{kf.value}' manually.", kf.page))
                continue
        missing = {n for n in _numbers(kf.value) - _numbers(kf.quote) if len(n) > 1}
        if missing and "computed" in kf.value.lower():
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
        near = "\n".join(tender.page_text(p, r.file or None) for p in (r.page - 1, r.page, r.page + 1))
        if max(_best_match(r.quote, near), _best_match(r.quote, tender.full_text)) < QUOTE_OK:
            issues.append(Issue("error", name, f"Source quote not found - verify: {r.text[:80]}",
                                r.page or None))
            continue
        if r.amount_inr:
            amt = str(int(round(r.amount_inr)))
            if amt not in _numbers(r.quote):
                issues.append(Issue("warning", name, f"Amount {amt} not found in its quote "
                                    f"'{r.quote[:80]}' - check the conversion.", r.page or None))
    return issues


def _pretty(n: str) -> str:
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
        issues.append(Issue("info", "submission_deadline",
                            f"Deadline {p.submission_deadline} has already passed."))
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
    return issues


def run_checks(syn: Synopsis, tender: TenderText) -> list[Issue]:
    repaired = fix_markup(syn)
    pre = [Issue("info", "formatting", f"{repaired} broken highlight/bold marker(s) repaired "
                 "automatically.")] if repaired else []
    return pre + _run_checks(syn, tender)


def _run_checks(syn: Synopsis, tender: TenderText) -> list[Issue]:
    quotes = (check_quotes(syn, tender) if tender.pages else
              [Issue("info", "sources", "Tender not supplied - source quotes not checked.")])
    if tender.pages:
        quotes += check_requirements(syn, tender)
    issues = quotes + check_dates(syn) + check_markup(syn) + check_coverage(syn)
    issues += [Issue("warning", "ingest", w) for w in tender.warnings]
    issues += [Issue("warning", "model_note", n) for n in syn.review_notes]
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(issues, key=lambda i: order[i.severity])
