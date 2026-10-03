"""Fit long tenders (multi-volume RFPs, GCC/SCC, BOQs, many corrigenda) into the model.

Short tender  -> sent whole, exactly as before.
Long tender   -> 1. "key pages" are sent word-for-word: the first pages of every file, every
                    page of a corrigendum/addendum, and the pages that score highest for
                    bid-relevant terms (dates, EMD, eligibility, turnover, scope, payment...).
                 2. the remaining relevant pages are read in parts by the AI, which returns
                    short notes with page numbers and exact quotes.
                 3. pages with no bid-relevant terms at all (drawings, BOQ rows, blank
                    formats) are skipped and listed in the review sheet.
Page numbers and quotes are kept, so the source checks in verify.py work the same way.

Settings (.env, all optional):
  LLM_MAX_INPUT_TOKENS   tender text budget per call      (default 90000)
  LLM_CHUNK_TOKENS       size of each note-taking part     (default 25000)
  LLM_MAX_CHUNKS         most note-taking calls per tender (default 10)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .ingest import Page, TenderText

TERMS = {
    # weight 3: decide bid / no-bid
    3: ["last date", "due date", "bid submission", "submission of bid", "closing date",
        "pre-bid", "pre bid", "earnest money", "emd", "bid security", "eligibility",
        "qualifying criteria", "qualification criteria", "pre-qualification", "turnover",
        "net worth", "similar work", "performance security", "performance bank guarantee",
        "pbg", "joint venture", "consortium", "estimated cost", "estimated value",
        "corrigendum", "addendum", "critical dates", "key dates", "data sheet", "bid data",
        "notice inviting", "reserve price", "licence fee", "license fee", "solvency",
        "scope of work", "terms of reference"],
    # weight 2: commercial and process
    2: ["tender fee", "document fee", "processing fee", "contract period", "completion period",
        "period of contract", "evaluation", "qcbs", "l1", "h1", "payment terms",
        "terms of payment", "liquidated damages", "penalty", "msme", "mse", "startup",
        "make in india", "local content", "blacklist", "debarred", "experience", "validity",
        "opening", "technical bid", "financial bid", "escalation", "security deposit",
        "deliverables", "milestone", "location", "site", "area", "quantity"],
    # weight 1: background
    1: ["contract", "bidder", "applicant", "agency", "authority", "work", "services", "supply"],
}
LOW_VALUE = ["format for", "proforma", "affidavit", "on the letterhead", "bill of quantities",
             "force majeure", "governing law", "arbitration", "declaration", "power of attorney"]
CORRIGENDUM = re.compile(r"corrigend|addend|amendment|clarification|pre-?bid (?:query|reply)", re.I)


@dataclass
class Condensed:
    text: str
    mode: str = "full"                       # "full" or "condensed"
    usage: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def tokens(text: str) -> int:
    """Rough token count (Hindi and tables tokenise worse than English prose)."""
    dev = len(re.findall(r"[ऀ-ॿ]", text))
    return int((len(text) - dev) / 3.6 + dev / 1.2) + 1


# Tender-specific documents beat generic ones when choosing what to read in full.
ROLE_WEIGHT = {"corrigendum": 2.0, "main": 1.5, "special": 1.3, "list": 1.3, "other": 1.0,
               "standard": 0.6, "policy": 0.35}


def score(page: Page) -> float:
    t = page.text.lower()
    s = sum(w * t.count(term) ** 0.5 for w, terms in TERMS.items() for term in terms)
    s -= 2 * sum(term in t for term in LOW_VALUE)
    digits = sum(c.isdigit() for c in t)
    if t and digits / len(t) > 0.35:          # BOQ / rate tables
        s *= 0.5
    if re.search(r"(₹|rs\.?|inr)\s*[\d,]+", t):
        s += 2
    if re.search(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b", t):
        s += 2
    return s


def _tag(p: Page, multi: bool) -> str:
    return f"{p.file} | PAGE {p.number}" if multi else f"PAGE {p.number}"


def _block(p: Page, multi: bool) -> str:
    return f"[[{_tag(p, multi)}]]\n{p.text.strip()}"


NOTES_PROMPT = """\
You read part of a long tender document and extract ONLY bid-relevant facts:
dates & times, fees, EMD, performance security, eligibility (legal / technical / financial),
JV rules, scope and quantities, contract period, payment terms, penalties, evaluation method,
exemptions, location, corrigendum changes. Ignore boilerplate, formats and blank forms.
Return JSON: {"notes": [{"page": int, "file": str, "topic": str, "fact": str, "quote": str}]}
- page / file from the [[...PAGE n]] marker of the page the fact is on.
- quote: exact words (5-30) copied from that page, containing every number in the fact.
- One fact per note, at most 40 notes. If nothing is relevant return {"notes": []}.
"""


def condense(tender: TenderText, budget: int | None = None) -> Condensed:
    budget = budget or int(os.getenv("LLM_MAX_INPUT_TOKENS", "90000"))
    full = tender.as_prompt_text()
    if tokens(full) <= budget:
        return Condensed(full)

    from . import llm
    multi = len({p.file for p in tender.pages}) > 1
    chunk_tokens = int(os.getenv("LLM_CHUNK_TOKENS", "25000"))
    max_chunks = int(os.getenv("LLM_MAX_CHUNKS", "10"))
    scored = {id(p): score(p) * ROLE_WEIGHT.get(tender.role(p.file), 1.0) for p in tender.pages}

    # 1. key pages, word for word (about 60% of the budget). Always: corrigenda, short site /
    #    item lists, and the opening pages of tender-specific documents (not generic policies).
    def must_keep(p: Page) -> bool:
        role = tender.role(p.file)
        if role == "corrigendum" or CORRIGENDUM.search(p.file) or CORRIGENDUM.search(p.text[:400]):
            return True
        if role == "list":
            return sum(1 for q in tender.pages if q.file == p.file) <= 5
        return p.number <= (1 if role in ("policy", "standard") else 3)
    must = [p for p in tender.pages if must_keep(p)]
    key, used = [], 0
    for p in must + sorted(tender.pages, key=lambda p: -scored[id(p)]):
        cost = tokens(p.text) + 10
        if p in key or (used + cost > budget * 0.6 and p not in must) \
                or used + cost > budget * 0.85:
            continue
        key.append(p)
        used += cost
    key_ids = {id(p) for p in key}

    # 2. other relevant pages -> notes, in parts
    rest = [p for p in tender.pages if id(p) not in key_ids]
    relevant = [p for p in rest if scored[id(p)] > 1]
    skipped = [p for p in rest if scored[id(p)] <= 1]
    chunks, cur, cur_tok = [], [], 0
    for p in relevant:                          # document order keeps context together
        t = tokens(p.text)
        if cur and cur_tok + t > chunk_tokens:
            chunks.append(cur)
            cur, cur_tok = [], 0
        cur.append(p)
        cur_tok += t
    if cur:
        chunks.append(cur)
    if len(chunks) > max_chunks:                # keep the parts with the most relevant pages
        ranked = sorted(chunks, key=lambda c: -sum(scored[id(p)] for p in c) / len(c))
        keep = {id(c) for c in ranked[:max_chunks]}
        for c in chunks:
            if id(c) not in keep:
                skipped += c
        chunks = [c for c in chunks if id(c) in keep]

    notes, usage = [], {}
    for c in chunks:
        data, u = llm.json_call(NOTES_PROMPT, "\n\n".join(_block(p, multi) for p in c))
        usage = llm._merge_usage(usage, u)
        for n in data.get("notes", []) if isinstance(data, dict) else []:
            if not isinstance(n, dict) or not n.get("fact"):
                continue
            tag = f"{n.get('file')} | PAGE {n.get('page')}" if multi and n.get("file") \
                else f"PAGE {n.get('page')}"
            notes.append(f"- [[{tag}]] {n.get('topic', '')}: {n['fact']} — \"{n.get('quote', '')}\"")

    key.sort(key=lambda p: (tender.pages.index(p)))
    text = (tender.documents_header() + "NOTE: this tender is long. KEY PAGES are given word for word; the other pages were "
            "read separately and are given as NOTES (with page numbers and exact quotes). "
            "Use both. Quotes in key_facts/requirements must be copied from either.\n\n"
            "=== KEY PAGES ===\n\n" + "\n\n".join(_block(p, multi) for p in key)
            + "\n\n=== NOTES FROM OTHER PAGES ===\n" + ("\n".join(notes) or "(none)"))
    skipped.sort(key=lambda p: tender.pages.index(p))
    warn = (f"Long tender ({len(tender.pages)} pages, ~{tokens(full):,} tokens): {len(key)} key "
            f"pages read in full, {sum(map(len, chunks))} pages read in {len(chunks)} part(s) as "
            f"notes, {len(skipped)} page(s) skipped as not bid-relevant"
            + (f" ({_page_list(skipped, multi)})" if skipped else "")
            + ". Check skipped pages if anything seems missing.")
    return Condensed(text, "condensed", usage, notes, [warn])


def _page_list(pages: list[Page], multi: bool, limit: int = 40) -> str:
    items = [(f"{p.file} p{p.number}" if multi else f"p{p.number}") for p in pages[:limit]]
    return ", ".join(items) + (f" … +{len(pages) - limit} more" if len(pages) > limit else "")
