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
        slip = _scale_slip(missing, _numbers(kf.quote))
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
        near = "\n".join(tender.page_text(p, r.file or None) for p in (r.page - 1, r.page, r.page + 1))
        if max(_best_match(r.quote, near), _best_match(r.quote, tender.full_text)) < QUOTE_OK:
            issues.append(Issue("error", name, f"Source quote not found - verify: {r.text[:80]}",
                                r.page or None))
            continue
        if r.amount_inr:
            amt = str(int(round(r.amount_inr)))
            slip = _scale_slip({amt}, _numbers(r.quote))
            if slip:
                issues.append(Issue("error", name, f"Amount looks wrong by a factor of 10 or "
                                    f"more: the document says {_inr(slip[1])}, the synopsis "
                                    f"says {_inr(slip[0])}. Check the lakh / crore conversion.",
                                    r.page or None))
            elif amt not in _numbers(r.quote):
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
    return issues


_FIN = re.compile(r"turnover|net[ -]?worth|solvency|banker'?s? cert|financial eligibility"
                  r"|similar work.{0,40}(?:₹|rs\.?|value)", re.I)
_YELLOW = re.compile(r"==(.+?)==")
_MONEYISH = re.compile(r"₹|\brs\.?\s?\d|\blakh|\blac\b|\bcrore|\bnil\b|stipulated|as per (?:the )?(?:auction )?catalogue", re.I)
_THAT = re.compile(r"^(That|Which)\s+(?=[a-z])")


def ensure_cyan(syn: Synopsis) -> int:
    """House style: financial thresholds in cyan (++). Models often use yellow or nothing."""
    changed = 0

    def cy(v: str, mode: str = "item") -> str:
        """mode: row = whole key-facts row; item = one bullet/cell; summary = yellow parts only."""
        nonlocal changed
        if not v or not (mode == "row" or _FIN.search(v)):
            return v
        new = _YELLOW.sub(lambda m: f"++{m.group(1)}++" if mode == "row" or _FIN.search(m.group(1))
                          or _FIN.search(v) and _MONEYISH.search(m.group(1)) else m.group(0), v)
        # Wrap the whole bullet rather than one amount: in slab lists the first amount is
        # often the bracket (e.g. revenue), not the threshold itself.
        if "++" not in new and "==" not in new and mode != "summary" \
                and (mode == "row" or _MONEYISH.search(new)):
            new = f"++{new.strip()}++"
        changed += new != v
        return new

    rows = []
    for row in syn.key_facts_table:
        if row and _FIN.search(row[0]):
            row = [cy(c, "row") for c in row]
        rows.append(row)
    syn.key_facts_table = rows
    for sec in syn.sections:
        for b in sec.blocks:
            b.text = cy(b.text)
            b.rows = [[cy(c) for c in row] for row in b.rows]
            for it in b.items:
                it.text = cy(it.text)
                it.sub_items = [cy(x) for x in it.sub_items]
    syn.summary = cy(syn.summary, "summary")
    return changed


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


def run_checks(syn: Synopsis, tender: TenderText) -> list[Issue]:
    repaired = fix_markup(syn)
    tidy_text(syn)
    cyan = ensure_cyan(syn)
    pre = [Issue("info", "formatting", f"{repaired} broken highlight/bold marker(s) repaired "
                 "automatically.")] if repaired else []
    if cyan:
        pre.append(Issue("info", "formatting", f"{cyan} turnover / net worth figure(s) given the "
                         "cyan highlight (house style)."))
    return pre + _run_checks(syn, tender)


def _run_checks(syn: Synopsis, tender: TenderText) -> list[Issue]:
    quotes = (check_quotes(syn, tender) if tender.pages else
              [Issue("info", "sources", "Tender not supplied - source quotes not checked.")])
    if tender.pages:
        quotes += check_requirements(syn, tender)
    issues = quotes + check_empty(syn) + check_dates(syn) + check_markup(syn) + check_coverage(syn)
    issues += [Issue("warning", "ingest", w) for w in tender.warnings]
    issues += [Issue("warning", "model_note", n) for n in syn.review_notes]
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(issues, key=lambda i: order[i.severity])
