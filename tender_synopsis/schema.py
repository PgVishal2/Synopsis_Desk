"""Structured output the LLM must return (JSON). Kept flat for strict JSON-schema mode.

Inline markup inside any text string:
  **text**   bold
  ==text==   yellow highlight  (critical: deadlines, EMD, key conditions)
  ++text++   cyan highlight    (financial eligibility thresholds)
Markers can be nested, e.g. ==**20 years**==

Document type, sector and verticals are free text (with suggestions in the prompt), so any
kind of tender fits. Every model is lenient when reading: missing fields, nulls, numbers
given as text, plain strings instead of items, and old field names are repaired before
validation, so slightly-off JSON from free models still loads.
"""
from __future__ import annotations

import types
import typing
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _empty_for(annotation):
    """Neutral value for a missing field, based on its type."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType) and type(None) in args:
        return None
    if origin is list or annotation is list:
        return []
    if origin is Literal:
        return args[0]
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is str:
        return ""
    return None


class Lenient(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    @model_validator(mode="before")
    @classmethod
    def _fill_missing(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for name, f in cls.model_fields.items():
            if data.get(name) is None:
                empty = _empty_for(f.annotation)
                if empty is not None or name not in data:
                    data[name] = empty
            elif typing.get_origin(f.annotation) is list and not isinstance(data[name], list):
                data[name] = [data[name]]
        return data


class Profile(Lenient):
    synopsis_title: str = Field(description="e.g. 'Tender Synopsis – Procurement of Sweeping Trucks'")
    topic_one_line: str = Field(description="What the work is, in one plain line")
    document_type: str = Field(description="Kind of document - see suggestions in the instructions; "
                               "any accurate label is allowed")
    sector: str = Field(description="Sector of the work - see suggestions; any accurate label is allowed")
    commercial_model: str = Field(description="e.g. 'L1 - lowest price', 'QCBS 70:30', "
                                  "'H1 - highest monthly licence fee', 'Zero cost to Govt (PPP)', "
                                  "'Empanelment - no award'")
    authority: str
    parent_body: str = Field(description="Ministry / department / status, or 'Not stated'")
    reference_no: str
    portal: str = Field(description="GeM, CPPP eTenders, Delhi eProc, email/physical, etc.")
    date_of_issue: str = Field(description="DD.MM.YYYY - publishing date if given, else letter date")
    submission_deadline: str = Field(description="DD.MM.YYYY HH:MM, or 'To be notified'")
    estimated_value: str = Field(description="₹ amount or 'Not disclosed'")
    location: str
    relevant_verticals: list[str] = Field(description="Organisation verticals this fits "
                                          "(names from the configured list, or 'Other')")
    relevance_note: str = Field(description="One line on why/for whom in the organisation this matters")

    @model_validator(mode="before")
    @classmethod
    def _old_names(cls, data):
        if isinstance(data, dict) and "prabhatam_verticals" in data:   # v1 JSON files
            data = dict(data)
            data.setdefault("relevant_verticals", data.pop("prabhatam_verticals"))
        return data


class Item(Lenient):
    text: str
    sub_items: list[str]

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, data):
        return {"text": data, "sub_items": []} if isinstance(data, str) else data


BLOCK_SYNONYMS = {"list": "bullets", "bullet": "bullets", "ul": "bullets", "ol": "numbered",
                  "numbered_list": "numbered", "kv": "kv_table", "key_value": "kv_table",
                  "heading": "subheading", "text": "paragraph", "para": "paragraph"}


class Block(Lenient):
    type: Literal["kv_table", "table", "bullets", "numbered", "paragraph", "subheading"]
    text: str = Field(description="For paragraph/subheading; '' otherwise")
    headers: list[str] = Field(description="Column headers for table/kv_table; [] otherwise")
    rows: list[list[str]] = Field(description="Rows for table/kv_table (kv_table: [label, value]); [] otherwise")
    items: list[Item] = Field(description="For bullets/numbered; [] otherwise")

    @model_validator(mode="before")
    @classmethod
    def _fix_type(cls, data):
        if isinstance(data, dict):
            t = str(data.get("type") or "").strip().lower()
            data = dict(data, type=BLOCK_SYNONYMS.get(t, t or "paragraph"))
            if data["type"] not in ("kv_table", "table", "bullets", "numbered", "paragraph",
                                    "subheading"):
                data["type"] = "paragraph"
            rows = data.get("rows")
            if isinstance(rows, list):
                data["rows"] = [[str(c) for c in r] if isinstance(r, list) else [str(r)]
                                for r in rows]
        return data


class Section(Lenient):
    key: str = Field(description="Section-library key (e.g. 'key_dates') or 'custom'")
    heading: str
    blocks: list[Block]


class KeyFact(Lenient):
    field: str = Field(description="e.g. submission_deadline, emd, pbg, turnover, net_worth, "
                       "estimated_value, pre_bid, contract_period")
    value: str
    file: str = Field(description="Source file name, or '' if single file")
    page: int
    quote: str = Field(description="Exact short quote (5-30 words) copied from that page")


REQUIREMENT_CATEGORIES = (
    "turnover", "net_worth", "profitability", "solvency_or_bank_certificate",
    "years_in_existence", "similar_work", "entity_type", "registration_or_licence",
    "certification", "office_location", "manpower", "not_blacklisted", "jv_consortium",
    "msme_startup_exemption", "emd", "other")


_CATEGORY_HINTS = [   # (canonical category, words in category or text) - first match wins
    ("emd", ("emd", "earnest", "bid security", "lien")),
    ("jv_consortium", ("joint venture", "consortium", "jv")),
    ("msme_startup_exemption", ("msme", "mse ", "startup", "udyam")),
    ("turnover", ("turnover",)),
    ("net_worth", ("net worth", "networth")),
    ("solvency_or_bank_certificate", ("solvency", "banker")),
    ("profitability", ("profit", "loss")),
    ("not_blacklisted", ("blacklist", "debar", "banned", "suspension", "suspend")),
    ("years_in_existence", ("years of existence", "in existence", "incorporated", "operational for")),
    ("similar_work", ("similar work", "experience", "completed work", "past work")),
    ("certification", ("iso", "certificat")),
    ("registration_or_licence", ("registration", "registered", "licence", "license", "pan", "gst")),
    ("office_location", ("office in", "located in", "branch")),
    ("manpower", ("manpower", "staff", "employees", "personnel")),
    ("entity_type", ("company", "llp", "partnership", "proprietor", "legal entity", "society")),
]


def canonical_category(category: str, text: str) -> str:
    """Map free-form categories (e.g. 'financial', 'legal', 'em') onto the allowed list."""
    c = (category or "").strip().lower().replace(" ", "_")
    if c in REQUIREMENT_CATEGORIES:
        return c
    if c in ("em", "emd_bid_security"):
        return "emd"
    hay = f" {c.replace('_', ' ')} {(text or '').lower()} "
    for cat, words in _CATEGORY_HINTS:
        if any(w in hay for w in words):
            return cat
    return "other"


class Requirement(Lenient):
    """One eligibility condition, in a form code can compare with a company profile."""

    @model_validator(mode="before")
    @classmethod
    def _canon(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            data["category"] = canonical_category(str(data.get("category") or ""),
                                                  str(data.get("text") or ""))
        return data

    category: str = Field(description="One of: " + ", ".join(REQUIREMENT_CATEGORIES))
    text: str = Field(description="The condition in one plain line, as the tender states it")
    mandatory: bool = Field(description="true if failing it disqualifies the bidder")
    option_group: str = Field(description="Same label for alternatives where ANY ONE suffices "
                              "(e.g. 'experience' for 1x80% / 2x60% / 3x40% options); '' otherwise")
    amount_inr: float | None = Field(description="Money threshold in rupees as a plain number "
                                     "(₹120 Crore -> 1200000000), else null")
    count: int | None = Field(description="Number of works/projects/years/staff required, else null")
    period_years: float | None = Field(description="Look-back window or minimum age in years "
                                       "(e.g. 'last 7 years' -> 7, '5 years existence' -> 5), else null")
    keywords: list[str] = Field(description="For similar_work / registration / certification: "
                                "2-6 short words that identify a matching work or document")
    file: str = Field(description="Source file name, or '' if single file")
    page: int
    quote: str = Field(description="Exact short quote copied from that page")


class Synopsis(Lenient):
    profile: Profile
    key_facts_table: list[list[str]] = Field(description="[label, value] rows for the top table")
    sections: list[Section]
    summary: str = Field(description="2-4 sentence '✅ In Summary' paragraph")
    key_facts: list[KeyFact] = Field(description="Every critical date / amount / threshold, with source")
    requirements: list[Requirement] = Field(description="Every eligibility / qualification "
                                            "condition, one per entry, for the bid/no-bid check")
    review_notes: list[str] = Field(description="Ambiguities, conflicts, corrigenda, missing info "
                                    "a reviewer must check")

    @model_validator(mode="before")
    @classmethod
    def _rows_to_str(cls, data):
        if isinstance(data, dict) and isinstance(data.get("key_facts_table"), list):
            data = dict(data)
            data["key_facts_table"] = [[str(c) for c in r] if isinstance(r, list) else [str(r)]
                                       for r in data["key_facts_table"]]
        return data
