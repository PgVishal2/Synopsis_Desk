"""Command line.

  python -m tender_synopsis generate tender.pdf [corrigendum.pdf ...] -o output --mode detailed
  python -m tender_synopsis rerender output/x_synopsis.json --tender tender.pdf
  python -m tender_synopsis assess output/x_synopsis.json [--profile company_profile.json]
  python -m tender_synopsis text tender.pdf            # show what the model will read
"""
from __future__ import annotations

import argparse
import sys

from .env import load_env


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tender_synopsis")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="Tender file(s) -> synopsis .docx")
    g.add_argument("files", nargs="+")
    g.add_argument("-o", "--out", default="output")
    g.add_argument("--mode", choices=["detailed", "compact"], default="detailed")
    g.add_argument("--no-double-check", action="store_true")
    g.add_argument("--name")
    g.add_argument("--profile", help="company profile JSON (default company_profile.json)")

    r = sub.add_parser("rerender", help="Edited JSON -> .docx (no AI call)")
    r.add_argument("json")
    r.add_argument("--tender", nargs="*")
    r.add_argument("--profile")

    b = sub.add_parser("assess", help="Re-run the bid/no-bid check on a synopsis JSON (no AI call)")
    b.add_argument("json")
    b.add_argument("--profile")

    sub.add_parser("check", help="Test the API key and model with a tiny call")

    t = sub.add_parser("text", help="Print extracted text")
    t.add_argument("files", nargs="+")

    a = ap.parse_args(argv)
    load_env()
    if a.cmd == "check":
        from .env import key_report
        from .llm import LLMError, check_connection
        print(key_report()[1])
        try:
            print("OK:", check_connection())
        except LLMError as e:
            print("FAILED:", e)
            sys.exit(1)
        return
    if a.cmd == "text":
        from .ingest import read_files
        tt = read_files(a.files)
        print(tt.as_prompt_text())
        for w in tt.warnings:
            print("WARNING:", w, file=sys.stderr)
        return
    from .assess import load_profiles
    profiles = load_profiles(a.profile) if getattr(a, "profile", None) else None
    if a.cmd == "assess":
        import json as _json
        from pathlib import Path

        from .assess import assess, to_markdown
        from .schema import Synopsis
        syn = Synopsis.model_validate(_json.loads(Path(a.json).read_text(encoding="utf-8")))
        print(to_markdown(syn, assess(syn, profiles)))
        return
    from .pipeline import generate, rerender
    res = (generate(a.files, a.out, a.mode, not a.no_double_check, name=a.name, profiles=profiles,
                    progress=lambda m: print("…", m, file=sys.stderr))
           if a.cmd == "generate" else rerender(a.json, a.tender, profiles=profiles))
    errs = sum(i.severity == "error" for i in res.issues)
    print(f"Synopsis: {res.docx_path}\nReview:   {res.review_path}\nJSON:     {res.json_path}")
    print(f"Bid check: {res.assessment_path}")
    for x in res.assessments:
        print(f"  {x.company}: {x.verdict}")
    print(f"Checks: {errs} error(s), {len(res.issues) - errs} warning/info")
    if res.usage:
        from .llm import usage_line
        print("AI usage:", usage_line(res.usage))
        print("Usage:", res.usage)


if __name__ == "__main__":
    main()
