"""Organisation settings (organisation.json) - name, header line and business verticals.

Kept outside the code so the tool works for any group or company: edit the JSON file.
This file is sent to the AI, so put nothing confidential in it. Confidential company
figures belong in company_profile.json, which never leaves the computer (see assess.py).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORG_FILE = ROOT / "organisation.json"

DEFAULT = {"name": "Our organisation", "brand_line": "Tender Synopsis", "verticals": {}}

# Suggestions only - the AI may use any other label when none fits.
DOC_TYPES = ["NIT / Open Tender", "GeM Bid", "RFQ", "RFP", "EOI", "Empanelment",
             "PPP / Concession", "E-Auction", "Licence / Rights Allotment",
             "IBC Resolution Plan (Form G)", "Corrigendum", "RFI", "Rate Contract",
             "Lease / Allotment", "Disinvestment / Asset Sale"]
SECTORS = ["Civil / Infrastructure Works", "Supply / Equipment", "Services", "Consultancy",
           "Advertising / Media / OOH", "Events / Outreach", "Real Estate / Land", "Telecom",
           "Aviation", "Agriculture / Allied", "Distressed Asset", "IT / Digital", "Healthcare",
           "Education / Skilling", "Energy / Power", "Water / Sanitation", "Manpower / Facility"]


@lru_cache(maxsize=1)
def _load(path: str, mtime: float) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return {**DEFAULT, **{k: v for k, v in data.items() if not k.startswith("_")}}


def org() -> dict:
    """Current organisation settings (re-read when the file changes)."""
    if not ORG_FILE.exists():
        return dict(DEFAULT)
    return _load(str(ORG_FILE), ORG_FILE.stat().st_mtime)


def verticals_text() -> str:
    v = org()["verticals"]
    if not v:
        return "(no verticals configured - use 'Other' and describe relevance in one line)"
    return "; ".join(f"{k} ({d})" if d else k for k, d in v.items())
