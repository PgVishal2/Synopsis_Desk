"""Bid / no-bid eligibility check against the group's own company profiles.

Privacy: the AI only reads the TENDER and lists its requirements (schema.Requirement).
The comparison with company figures happens here, in plain Python, on this computer.
company_profile.json is never sent to any AI provider.

Each requirement gets one status:
  MET            the profile clearly satisfies it
  NOT MET        the profile clearly fails it
  CHECK          partly matched or needs judgement (e.g. "similar work" wording)
  DATA MISSING   the profile has no figure for it - add it to company_profile.json
  INFO           not a pass/fail item (JV rule, EMD, exemptions) - shown for awareness
Alternatives sharing an option_group pass if any one passes.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from rapidfuzz import fuzz

from .schema import Requirement, Synopsis

ROOT = Path(__file__).resolve().parent.parent
MET, NOT_MET, CHECK, MISSING, INFO = "MET", "NOT MET", "CHECK", "DATA MISSING", "INFO"
ICON = {MET: "✅", NOT_MET: "❌", CHECK: "⚠️", MISSING: "❔", INFO: "ℹ️"}
RANK = {NOT_MET: 0, MISSING: 1, CHECK: 2, MET: 3, INFO: 4}


@dataclass
class Row:
    requirement: Requirement
    status: str
    detail: str


@dataclass
class Assessment:
    company: str
    verdict: str                      # Eligible / Needs review / Not eligible
    rows: list[Row] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        return {s: sum(r.status == s for r in self.rows) for s in ICON}


# ---------------------------------------------------------------- profile helpers

def profile_path() -> Path:
    return Path(os.getenv("COMPANY_PROFILE") or ROOT / "company_profile.json")


def load_profiles(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else profile_path()
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    comps = data.get("companies", data if isinstance(data, list) else [data])
    return [c for c in comps if isinstance(c, dict) and c.get("name")]


def _date(s) -> date | None:
    if not s:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except ValueError:
            pass
    return None


def _by_fy(d: dict | None, n: int) -> list[tuple[str, float]]:
    """Last n financial years, newest first. Keys like '2024-25' or 'FY2024-25'."""
    if not d:
        return []
    items = []
    for k, v in d.items():
        m = re.search(r"(\d{4})", str(k))
        if m and v is not None:
            items.append((int(m[1]), str(k), float(v)))
    items.sort(reverse=True)
    return [(k, v) for _, k, v in items[:n]]


def inr(x: float | None) -> str:
    if x is None:
        return "—"
    if abs(x) >= 1e7:
        return f"₹{x / 1e7:,.2f} Cr"
    if abs(x) >= 1e5:
        return f"₹{x / 1e5:,.2f} lakh"
    return f"₹{x:,.0f}"


def _words(r: Requirement) -> list[str]:
    return [k.lower().strip() for k in r.keywords if k and len(k.strip()) > 1]


def _matches(text: str, words: list[str]) -> bool:
    t = text.lower()
    return any(w in t or fuzz.partial_ratio(w, t) >= 90 for w in words)


# ---------------------------------------------------------------- rules per category

def _turnover(r, c):
    n = int(r.period_years or r.count or 3)
    years = _by_fy(c.get("turnover_by_fy"), n)
    if not years or r.amount_inr is None:
        return (MISSING if not years else CHECK,
                "Add turnover_by_fy to the profile." if not years else
                "Threshold not a fixed amount - check manually.")
    avg = sum(v for _, v in years) / len(years)
    t = r.text.lower()
    each_year = "each" in t or "every" in t or "in any" in t
    value, label = (min(v for _, v in years), "lowest year") if each_year else (avg, "average")
    detail = (f"{label} {inr(value)} over {', '.join(k for k, _ in years)} "
              f"vs required {inr(r.amount_inr)}")
    if len(years) < n:
        return CHECK, detail + f" (only {len(years)} of {n} years in profile)"
    return (MET if value >= r.amount_inr else NOT_MET), detail


def _net_worth(r, c):
    nw = c.get("net_worth_inr")
    if nw is None:
        return MISSING, "Add net_worth_inr to the profile."
    if r.amount_inr is None:
        if "positive" in r.text.lower():
            return (MET if nw > 0 else NOT_MET), f"net worth {inr(nw)} (must be positive)"
        return CHECK, f"net worth {inr(nw)}; threshold not a fixed amount"
    return (MET if nw >= r.amount_inr else NOT_MET), \
        f"net worth {inr(nw)} (as of {c.get('net_worth_as_of', '?')}) vs required {inr(r.amount_inr)}"


def _profit(r, c):
    n = int(r.period_years or 3)
    years = _by_fy(c.get("profit_by_fy"), n)
    if not years:
        return MISSING, "Add profit_by_fy to the profile."
    losses = [k for k, v in years if v < 0]
    detail = f"profit in {len(years) - len(losses)} of last {len(years)} years" + \
        (f" (loss in {', '.join(losses)})" if losses else "")
    if not losses:
        return MET, detail
    return CHECK, detail + " - compare with the exact loss condition"


def _solvency(r, c):
    v = c.get("bank_solvency_inr")
    if v is None:
        return MISSING, ("No banker's / solvency certificate figure in profile"
                         + (f"; required {inr(r.amount_inr)}" if r.amount_inr else "") + ".")
    if r.amount_inr is None:
        return CHECK, f"available {inr(v)}"
    return (MET if v >= r.amount_inr else NOT_MET), f"available {inr(v)} vs required {inr(r.amount_inr)}"


def _existence(r, c):
    inc = _date(c.get("incorporated"))
    need = r.period_years or r.count
    if not inc:
        return MISSING, "Add incorporated (DD.MM.YYYY) to the profile."
    yrs = (date.today() - inc).days / 365.25
    if not need:
        return CHECK, f"in existence {yrs:.1f} years"
    return (MET if yrs >= need else NOT_MET), f"in existence {yrs:.1f} years vs required {need:g}"


def _similar_work(r, c):
    works = c.get("works") or []
    if not works:
        return MISSING, "Add past works (value, completion date, keywords) to the profile."
    window = r.period_years or 7
    today = date.today()
    recent = [w for w in works
              if (d := _date(w.get("completed"))) is None or (today - d).days <= window * 365.25]
    need = r.count or 1
    big = [w for w in recent if r.amount_inr is None or (w.get("value_inr") or 0) >= r.amount_inr]
    words = _words(r)
    similar = [w for w in big if not words or _matches(
        " ".join([w.get("name", ""), w.get("client", "")] + list(w.get("keywords", []))), words)]
    req = f"{need} work(s)" + (f" ≥ {inr(r.amount_inr)}" if r.amount_inr else "") + \
        f" in last {window:g} years"
    names = "; ".join(f"{w.get('name')} ({inr(w.get('value_inr'))})" for w in similar[:4])
    if len(similar) >= need:
        return MET, f"needs {req}; matching: {names}"
    if len(big) >= need:
        return CHECK, (f"needs {req}; {len(big)} work(s) meet the value but wording match "
                       f"is unclear ({', '.join(words) or 'no keywords'}) - judge similarity")
    return NOT_MET, f"needs {req}; profile has {len(similar)} matching / {len(big)} by value"


ENTITY_WORDS = ["company", "llp", "partnership", "proprietor", "society", "trust",
                "cooperative", "co-operative", "firm", "psu", "government"]


def _entity(r, c):
    et = (c.get("entity_type") or "").lower()
    if not et:
        return MISSING, "Add entity_type to the profile."
    allowed = list(dict.fromkeys([w for w in ENTITY_WORDS if w in r.text.lower()] + _words(r)))
    if not allowed:
        return CHECK, f"profile: {c.get('entity_type')}"
    ok = any(w in et for w in allowed) or ("firm" in allowed and "partnership" in et)
    return (MET if ok else CHECK), f"profile: {c.get('entity_type')}; allowed: {', '.join(allowed)}"


def _registration(r, c):
    regs = c.get("registrations") or []
    words = _words(r) or [r.text.lower()[:40]]
    hit = [g for g in regs if _matches(g, words) or any(_matches(w, [g.lower()]) for w in words)]
    if hit:
        return MET, f"profile has: {', '.join(hit)}"
    return MISSING, f"not found in profile registrations (looked for: {', '.join(words)})"


def _location(r, c):
    locs = c.get("office_locations") or []
    if not locs:
        return MISSING, "Add office_locations to the profile."
    words = _words(r)
    hit = [l for l in locs if words and _matches(l, words)]
    return (MET if hit else CHECK), f"offices: {', '.join(locs)}"


def _manpower(r, c):
    e = c.get("employees")
    if e is None:
        return MISSING, "Add employees to the profile."
    if not r.count:
        return CHECK, f"{e} employees"
    return (MET if e >= r.count else NOT_MET), f"{e} employees vs required {r.count}"


def _blacklist(r, c):
    if c.get("blacklisted") is None:
        return MISSING, "Add blacklisted: true/false to the profile."
    return (NOT_MET if c["blacklisted"] else MET), "declared " + \
        ("blacklisted" if c["blacklisted"] else "not blacklisted")


def _msme(r, c):
    tags = [t for t, on in (("MSE/MSME", c.get("msme")), ("Startup", c.get("startup"))) if on]
    return INFO, ("company is " + " & ".join(tags) + " - exemption may apply" if tags
                  else "company not marked MSME/startup - no exemption")


RULES = {"turnover": _turnover, "net_worth": _net_worth, "profitability": _profit,
         "solvency_or_bank_certificate": _solvency, "years_in_existence": _existence,
         "similar_work": _similar_work, "entity_type": _entity,
         "registration_or_licence": _registration, "certification": _registration,
         "office_location": _location, "manpower": _manpower, "not_blacklisted": _blacklist,
         "msme_startup_exemption": _msme}


def assess_company(syn: Synopsis, company: dict) -> Assessment:
    rows = []
    for r in syn.requirements:
        rule = RULES.get(r.category.strip().lower())
        if rule:
            try:
                status, detail = rule(r, company)
            except (TypeError, ValueError) as e:
                status, detail = CHECK, f"could not evaluate ({e}) - check the profile values"
        elif r.category in ("jv_consortium", "emd"):
            status, detail = INFO, "for awareness"
        else:
            status, detail = CHECK, "check manually"
        if not r.mandatory and status in (NOT_MET, MISSING):
            status = CHECK
        rows.append(Row(r, status, detail))

    # option groups: any one alternative passing is enough
    groups: dict[str, list[Row]] = {}
    for row in rows:
        if row.requirement.option_group:
            groups.setdefault(row.requirement.option_group.lower(), []).append(row)
    effective = [row.status for row in rows if not row.requirement.option_group]
    for g in groups.values():
        effective.append(max((x.status for x in g), key=lambda s: RANK[s] if s != INFO else -1))

    if NOT_MET in effective:
        verdict = "Not eligible"
    elif MISSING in effective or CHECK in effective:
        verdict = "Needs review"
    else:
        verdict = "Eligible"
    return Assessment(company["name"], verdict, rows)


def assess(syn: Synopsis, profiles: list[dict] | None = None) -> list[Assessment]:
    """All group companies, best fit first. Empty list if no profile file exists."""
    profiles = load_profiles() if profiles is None else profiles
    order = {"Eligible": 0, "Needs review": 1, "Not eligible": 2}
    res = [assess_company(syn, c) for c in profiles]
    return sorted(res, key=lambda a: (order[a.verdict], a.counts[NOT_MET], a.counts[MISSING]))


# ---------------------------------------------------------------- output

def to_markdown(syn: Synopsis, results: list[Assessment]) -> str:
    out = [f"# Bid / No-bid check – {syn.profile.synopsis_title}", "",
           "_Internal. Compared locally with company_profile.json (not sent to the AI). "
           "Confirm every ❌ / ⚠️ / ❔ against the tender before deciding._", ""]
    if not results:
        out.append("No company profile found. Copy `company_profile.example.json` to "
                   "`company_profile.json` and fill in the figures to get this check.")
    for a in results:
        c = a.counts
        out += [f"## {a.company} — **{a.verdict}**",
                f"✅ {c[MET]} met · ❌ {c[NOT_MET]} not met · ⚠️ {c[CHECK]} check · "
                f"❔ {c[MISSING]} data missing · ℹ️ {c[INFO]} info", "",
                "| | Requirement | Option | Assessment | Page |", "|---|---|---|---|---|"]
        for row in a.rows:
            r = row.requirement
            out.append(f"| {ICON[row.status]} | {_md(r.text)} | {r.option_group or ''} | "
                       f"{_md(row.detail)} | {r.page or ''} |")
        out.append("")
    if not syn.requirements:
        out.append("The AI listed no requirements for this tender - check eligibility manually.")
    return "\n".join(out)


def _md(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ")


def to_docx(syn: Synopsis, results: list[Assessment], path: Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    from .render import NAVY, _base_styles, _table, add_rich_text
    doc = Document()
    _base_styles(doc)
    p = doc.add_paragraph()
    run = p.add_run("Bid / No-bid check – " + syn.profile.synopsis_title)
    run.bold, run.font.size, run.font.color.rgb = True, Pt(15), NAVY
    add_rich_text(doc.add_paragraph(), "Internal. Compared on this computer with the company "
                  "profile (not sent to the AI). Confirm every ❌ / ⚠️ / ❔ before deciding.")
    if not results:
        doc.add_paragraph("No company profile found - create company_profile.json.")
    for a in results:
        h = doc.add_paragraph()
        add_rich_text(h, f"{a.company} — =={a.verdict}==", bold=True, size=12)
        c = a.counts
        doc.add_paragraph(f"✅ {c[MET]} met · ❌ {c[NOT_MET]} not met · ⚠️ {c[CHECK]} check · "
                          f"❔ {c[MISSING]} data missing · ℹ️ {c[INFO]} info")
        rows = [[ICON[r.status], r.requirement.text, r.requirement.option_group, r.detail,
                 str(r.requirement.page or "")] for r in a.rows]
        _table(doc, ["", "Requirement", "Option", "Assessment", "Page"], rows,
               [1.0, 6.2, 1.8, 6.8, 1.2])
    path = Path(path)
    doc.save(path)
    return path
