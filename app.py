"""Tender Synopsis Generator - web app.   Run:  streamlit run app.py"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import streamlit as st

from tender_synopsis.config import org
from tender_synopsis.env import key_report, load_env
from tender_synopsis.llm import LLMError, check_connection, friendly
from tender_synopsis.ingest import read_files
from tender_synopsis.pipeline import finish, generate
from tender_synopsis.schema import Synopsis

load_env()
st.set_page_config(page_title="Tender Synopsis Generator", page_icon="📄", layout="wide")
st.title("📄 Tender Synopsis Generator")
st.caption(f"{org()['name']} · upload a tender (plus any corrigenda/annexures) → review → download")

WORK = Path(tempfile.gettempdir()) / "tender_synopsis_app"
WORK.mkdir(exist_ok=True)

with st.sidebar:
    mode = st.radio("Synopsis style", ["detailed", "compact"],
                    help="Detailed ≈ EPI / Gaushala style. Compact ≈ one-page DSIIDC style.")
    double = st.checkbox("Second AI check on critical figures", value=True)
    st.divider()
    st.markdown("**Bid / no-bid check**")
    from tender_synopsis.assess import load_profiles, profile_path
    up = st.file_uploader("Company profile (.json) – stays on this computer", type=["json"],
                          help="Optional. Default: company_profile.json in the app folder. "
                               "Never sent to the AI.")
    if up:
        prof_file = WORK / "session_company_profile.json"
        prof_file.write_bytes(up.getbuffer())
        st.session_state["profiles"] = load_profiles(prof_file)
    profiles = st.session_state.get("profiles") or load_profiles()
    st.caption(f"{len(profiles)} company profile(s) loaded" if profiles else
               f"No profile – create {profile_path().name} from company_profile.example.json")
    st.markdown("**Highlights:** 🟨 critical items · 🟦 financial thresholds")
    st.divider()
    ok, report = key_report()
    (st.caption if ok else st.error)(report)
    from tender_synopsis.llm import providers
    plist = providers()
    if len(plist) > 1:
        st.caption("Fallbacks: " + " → ".join(p.label for p in plist[1:]))
    if st.button("Test AI connection"):
        try:
            st.success(check_connection().replace("\n", "  \n"))
        except LLMError as e:
            st.error(str(e).replace("\n", "  \n"))

files = st.file_uploader("Tender documents (PDF / DOCX / images)", type=["pdf", "docx", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"],
                         accept_multiple_files=True)

if files and st.button("Generate synopsis", type="primary"):
    job = Path(tempfile.mkdtemp(dir=WORK))
    paths = []
    for f in files:
        p = job / f.name
        p.write_bytes(f.getbuffer())
        paths.append(p)
    with st.status("Working… (scanned or long tenders take longer)", expanded=True) as status:
        try:
            res = generate(paths, job, mode, double, profiles=profiles or None,
                           progress=st.write)
            status.update(label="Done", state="complete", expanded=False)
        except Exception as e:
            status.update(label="Failed", state="error")
            st.error(f"Failed: {friendly(e)}".replace("\n", "  \n"))
            st.stop()
    st.session_state.update(res=res, paths=paths, job=job)

res = st.session_state.get("res")
if res:
    syn: Synopsis = res.synopsis
    p = syn.profile
    st.subheader(p.synopsis_title)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Type", p.document_type)
    c2.metric("Sector", p.sector)
    c3.metric("Deadline", p.submission_deadline)
    c4.metric("Est. value", p.estimated_value)
    st.write(f"**Topic:** {p.topic_one_line}  \n**Authority:** {p.authority}  \n"
             f"**Relevant to:** {', '.join(p.relevant_verticals)} — {p.relevance_note}")

    errs = [i for i in res.issues if i.severity == "error"]
    st.markdown(f"### Checks — {len(errs)} to fix, {len(res.issues) - len(errs)} to review")
    for i in res.issues:
        msg = f"**{i.field}**" + (f" (p.{i.page})" if i.page else "") + f": {i.message}"
        {"error": st.error, "warning": st.warning, "info": st.info}[i.severity](msg)

    st.markdown("### Bid / no-bid check")
    if not res.assessments:
        st.info("Add a company profile (sidebar) to check eligibility automatically.")
    for a in res.assessments:
        icon = {"Eligible": "✅", "Needs review": "⚠️", "Not eligible": "❌"}[a.verdict]
        with st.expander(f"{icon} {a.company} — {a.verdict}", expanded=a is res.assessments[0]):
            st.table([{"": {"MET": "✅", "NOT MET": "❌", "CHECK": "⚠️", "DATA MISSING": "❔",
                            "INFO": "ℹ️"}[r.status],
                       "Requirement": r.requirement.text, "Option": r.requirement.option_group,
                       "Assessment": r.detail, "File": r.requirement.file,
                       "Page": r.requirement.page} for r in a.rows])

    if st.session_state.get("paths") and len(st.session_state["paths"]) > 1:
        try:
            from tender_synopsis.pipeline import documents_table
        except ImportError:
            st.error("The app's files were updated while it was running. Close the app window "
                     "and start it again (run_windows.bat).")
            st.stop()
        with st.expander("Documents read and their roles"):
            st.table([{k: v for k, v in r.items() if k != "rank"}
                      for r in documents_table(read_files(st.session_state["paths"]))])

    with st.expander("Key facts with source page and quote"):
        st.table([{"Fact": k.field, "Value": k.value, "File": k.file, "Page": k.page,
                   "Quote": k.quote}
                  for k in syn.key_facts])

    st.markdown("### Edit (optional)")
    edited = st.text_area("Synopsis JSON — change any text, then click Apply edits",
                          syn.model_dump_json(indent=2), height=350)
    if st.button("Apply edits"):
        try:
            new = Synopsis.model_validate(json.loads(edited))
            tender = read_files(st.session_state["paths"])
            st.session_state["res"] = finish(new, tender, st.session_state["job"],
                                             res.docx_path.stem,
                                             [x.name for x in st.session_state["paths"]],
                                             profiles=profiles or None)
            st.rerun()
        except Exception as e:
            st.error(f"Could not apply edits: {e}")

    st.download_button("⬇️ Download synopsis (.docx)", res.docx_path.read_bytes(),
                       file_name=res.docx_path.name, type="primary")
    st.download_button("Download review sheet (.md)", res.review_path.read_bytes(),
                       file_name=res.review_path.name)
    if res.assessment_docx:
        st.download_button("Download bid / no-bid check (.docx)", res.assessment_docx.read_bytes(),
                           file_name=res.assessment_docx.name)
    if res.usage:
        from tender_synopsis.llm import usage_line
        st.caption(f"{usage_line(res.usage)} · model {res.usage.get('model')} · "
                   f"{res.usage.get('seconds')} s · input: {res.usage.get('input_mode', 'full')}")
