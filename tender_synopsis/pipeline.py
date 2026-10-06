"""End-to-end: tender files -> text -> LLM JSON -> checks -> Word synopsis + review report."""
from __future__ import annotations

import re

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import llm
from .assess import Assessment, assess, to_docx, to_markdown
from .condense import condense
from .ingest import TenderText, read_files
from .prompts import VERIFY_PROMPT, build_system_prompt, build_user_prompt
from .render import render_docx
from .schema import Synopsis
from .style_examples import load_examples
from .verify import Issue, _numbers, run_checks

CRITICAL = ("deadline", "emd", "turnover", "net_worth", "pbg", "security", "fee", "value")


@dataclass
class Result:
    synopsis: Synopsis
    issues: list[Issue]
    docx_path: Path
    json_path: Path
    review_path: Path
    usage: dict = field(default_factory=dict)
    assessments: list[Assessment] = field(default_factory=list)
    assessment_path: Path | None = None
    assessment_docx: Path | None = None


def llm_double_check(syn: Synopsis, tender: TenderText) -> list[Issue]:
    """Second opinion on critical facts only (cheap: short page excerpts)."""
    facts = [k for k in syn.key_facts if any(c in k.field.lower() for c in CRITICAL)]
    if not facts:
        return []
    lines = []
    for k in facts:
        page = tender.page_text(k.page, k.file or None)[:6000]
        nxt = tender.page_text(k.page + 1, k.file or None)[:2500] if k.page else ""
        lines.append(f"FIELD: {k.field}\nSTATED VALUE: {k.value}\nSOURCE PAGE {k.page}:\n{page}"
                     + (f"\n(NEXT PAGE, clause may continue):\n{nxt}" if nxt else ""))
    data, _ = llm.json_call(VERIFY_PROMPT, "\n\n---\n\n".join(lines))
    stated = {k.field: k.value for k in facts}
    issues = []
    for c in data.get("checks", []) if isinstance(data, dict) else []:
        if c.get("ok", True):
            continue
        field, fix = c.get("field", "?"), str(c.get("correct_value") or "").strip()
        # Only a different number/date is a real error; wording-only disagreements are
        # usually OCR typos or synonyms, so they are shown as something to glance at.
        same_numbers = bool(fix) and _numbers(fix) == _numbers(stated.get(field, ""))
        # "not on this page / truncated / not stated" = could not confirm, not proven wrong:
        # the fact often sits in another clause or file.
        unseen = re.search(r"not (?:stated|found|mentioned|present|supported|in the provided)|"
                           r"truncat|does not (?:state|mention)|no mention|cannot (?:be )?(?:confirm|verif)",
                           f"{c.get('reason', '')} {fix}", re.I)
        severity = "warning" if (not fix or same_numbers or unseen) else "error"
        lead = ("Could not confirm this on the quoted page - please check the documents: "
                if unseen else "Second check disagrees: ")
        issues.append(Issue(severity, field, f"{lead}{c.get('reason', '')}"
                            + (f" (suggested: {fix})" if fix else "")))
    return issues


def documents_table(tender: TenderText) -> list[dict]:
    """One row per file: role, pages, OCR pages (for the review sheet and the app)."""
    from .ingest import ROLES
    rows = []
    for f in dict.fromkeys(p.file for p in tender.pages):
        ps = [p for p in tender.pages if p.file == f]
        role = tender.role(f)
        rows.append({"File": f, "Role": ROLES[role][1], "Pages": len(ps),
                     "OCR pages": sum(p.ocr for p in ps), "rank": ROLES[role][0]})
    return sorted(rows, key=lambda r: r["rank"])


def write_review(syn: Synopsis, issues: list[Issue], path: Path, usage: dict,
                 tender: TenderText | None = None) -> None:
    p = syn.profile
    icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}
    out = [f"# Review sheet - {p.synopsis_title}", "",
           "| Field | Value |", "|---|---|"]
    for k, v in p.model_dump().items():
        out.append(f"| {k} | {', '.join(v) if isinstance(v, list) else v} |")
    n_err = sum(i.severity == "error" for i in issues)
    out += ["", f"## Checks: {n_err} error(s), {len(issues) - n_err} other", ""]
    out += [f"- {icon[i.severity]} **{i.field}**"
            + (f" (p.{i.page})" if i.page else "") + f": {i.message}" for i in issues] or ["- All checks passed."]
    out += ["", "## Key facts and sources", "", "| Fact | Value | File | Page | Quote |",
            "|---|---|---|---|---|"]
    out += [f"| {k.field} | {k.value} | {k.file} | {k.page} | {k.quote} |" for k in syn.key_facts]
    if tender is not None and tender.pages:
        out += ["", "## Documents read (most authoritative first)", "",
                "| File | Role | Pages | OCR pages |", "|---|---|---|---|"]
        out += [f"| {r['File']} | {r['Role']} | {r['Pages']} | {r['OCR pages']} |"
                for r in documents_table(tender)]
    if syn.requirements:
        out += ["", "## Requirements read from the tender (used for the bid check)", "",
                "| Category | Requirement | Mandatory | Option | Amount (₹) | Count | Years | File | Page |",
                "|---|---|---|---|---|---|---|---|---|"]
        out += [f"| {r.category} | {r.text.replace('|', '/')} | {'yes' if r.mandatory else 'no'} | "
                f"{r.option_group} | {'' if r.amount_inr is None else f'{r.amount_inr:,.0f}'} | "
                f"{r.count or ''} | {r.period_years or ''} | {r.file} | {r.page} |" for r in syn.requirements]
    if usage:
        out += ["", f"_Provider: {usage.get('provider', '-')} · model: {usage.get('model')} · tokens in/out: "
                    f"{usage.get('input_tokens')}/{usage.get('output_tokens')}_"]
    path.write_text("\n".join(out), encoding="utf-8")


def finish(syn: Synopsis, tender: TenderText, out_dir: Path, name: str, files: list[str],
           usage: dict | None = None, double_check: bool = False,
           extra_issues: list[Issue] | None = None, profiles: list[dict] | None = None) -> Result:
    issues = run_checks(syn, tender) + list(extra_issues or [])
    if double_check:
        try:
            issues = llm_double_check(syn, tender) + issues
        except Exception as e:          # the synopsis is still usable without the second check
            issues.append(Issue("warning", "second_check", f"Second AI check skipped: {llm.friendly(e)}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{name}.json"
    json_path.write_text(syn.model_dump_json(indent=2), encoding="utf-8")
    docx_path = render_docx(syn, out_dir / f"{name}.docx", files)
    review_path = out_dir / f"{name}_review.md"
    write_review(syn, issues, review_path, usage or {}, tender)
    results = assess(syn, profiles)
    a_md = out_dir / f"{name}_bid_check.md"
    a_md.write_text(to_markdown(syn, results), encoding="utf-8")
    a_docx = to_docx(syn, results, out_dir / f"{name}_bid_check.docx") if results else None
    return Result(syn, issues, docx_path, json_path, review_path, usage or {},
                  results, a_md, a_docx)


def generate(files: list[str | Path], out_dir: str | Path = "output", detail: str = "detailed",
             double_check: bool = True, exclude_example: str | None = None,
             name: str | None = None, profiles: list[dict] | None = None,
             progress=None) -> Result:
    """progress: optional callback(str) for status messages (used by the web app)."""
    say = progress or (lambda msg: None)
    files = [Path(f) for f in files]
    t0 = time.time()
    llm.reset_fallback_state()
    say("Reading the tender (OCR for scanned pages)…")
    tender = read_files(files)
    if not tender.full_text.strip():
        raise llm.LLMError("No text could be read from the tender files. If they are scanned, "
                           "install Tesseract OCR (see README).")
    say("Checking length…")
    packed = condense(tender)
    if packed.mode == "condensed":
        say("Long tender: key pages kept in full, other pages summarised in parts…")
    system = build_system_prompt(load_examples(exclude=exclude_example), detail)
    say("Writing the synopsis…")
    syn, usage = llm.structured_call(system, build_user_prompt(packed.text), Synopsis)
    usage = llm._merge_usage(packed.usage, usage) if packed.usage else usage
    if tender.ai_pages:                     # hard-to-read pages the AI read from their images
        for k in ("input_tokens", "output_tokens"):
            usage[k] = (usage.get(k) or 0) + (tender.ai_usage.get(k) or 0)
        usage["ai_image_pages"] = tender.ai_pages
    usage["seconds"] = round(time.time() - t0, 1)
    usage["input_mode"] = packed.mode
    say("Checking figures against the tender…")
    return finish(syn, tender, Path(out_dir), name or files[0].stem + "_synopsis",
                  [f.name for f in files], usage, double_check,
                  [Issue("info", "length", w) for w in packed.warnings]
                  + [Issue("warning", "ai_provider", f"Fallback used – {f}")
                     for f in usage.get("fallbacks", [])], profiles)


def rerender(json_path: str | Path, tender_files: list[str | Path] | None = None,
             out_dir: str | Path | None = None, profiles: list[dict] | None = None) -> Result:
    """Re-build the Word file after a reviewer edits the JSON (no LLM call)."""
    json_path = Path(json_path)
    syn = Synopsis.model_validate(json.loads(json_path.read_text(encoding="utf-8")))
    tender = read_files(tender_files) if tender_files else TenderText()
    return finish(syn, tender, Path(out_dir or json_path.parent), json_path.stem,
                  [Path(f).name for f in tender_files or []], profiles=profiles)
