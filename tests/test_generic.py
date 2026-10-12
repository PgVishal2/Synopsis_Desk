"""Offline tests for the v2 additions: long tenders, robust JSON, generic schema, bid check.
Run:  python -m pytest -q tests   (no API key needed)"""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tender_synopsis import assess, condense, llm
from tender_synopsis import ingest
from tender_synopsis.ingest import Page, TenderText
from tender_synopsis.schema import Requirement, Synopsis

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "model_output" / "DSIIDC_RAMP_RFP_synopsis.json"


def test_ocr_space_is_preferred(monkeypatch):
    monkeypatch.setenv("OCR_SPACE_API_KEY", "test-key")
    monkeypatch.setattr(ingest, "_ocr_space", lambda png: "cloud text")
    monkeypatch.setattr(ingest, "_tesseract_executable", lambda: None)
    assert ingest._ocr_png(b"png", "eng") == ("cloud text", None)


def test_null_ocr_space_result_uses_tesseract(monkeypatch):
    monkeypatch.setenv("OCR_SPACE_API_KEY", "test-key")
    monkeypatch.setattr(ingest, "_ocr_space", lambda png: None)
    monkeypatch.setattr(ingest, "_tesseract_executable", lambda: "tesseract")
    monkeypatch.setattr(ingest.subprocess, "run",
                        lambda *args, **kwargs: NS(returncode=0, stdout="local text", stderr=""))
    text, warning = ingest._ocr_png(b"png", "eng")
    assert text == "local text"
    assert "Tesseract fallback used" in warning


def test_clean_treats_null_text_as_empty():
    assert ingest._clean(None) == ""


def test_env_loads_ocr_space_key(monkeypatch, tmp_path):
    from tender_synopsis import env

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OCR_SPACE_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OCR_SPACE_API_KEY=ocr-test-key\n")
    env.load_env()
    assert __import__("os").environ["OCR_SPACE_API_KEY"] == "ocr-test-key"


def test_ocr_space_failure_falls_back_to_tesseract(monkeypatch):
    monkeypatch.setenv("OCR_SPACE_API_KEY", "test-key")
    monkeypatch.setattr(ingest, "_ocr_space", lambda png: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(ingest, "_tesseract_executable", lambda: "tesseract")
    monkeypatch.setattr(ingest.subprocess, "run",
                        lambda *args, **kwargs: NS(returncode=0, stdout="local text", stderr=""))
    text, warning = ingest._ocr_png(b"png", "eng")
    assert text == "local text"
    assert "Tesseract fallback used" in warning


# ------------------------------------------------------------------ schema
def test_old_json_and_sloppy_json_load():
    old = Synopsis.model_validate(json.loads(SAMPLE.read_text()))
    assert old.profile.relevant_verticals == ["Media"] and old.requirements == []
    sloppy = Synopsis.model_validate({
        "profile": {"synopsis_title": "T", "document_type": "Lease of Kiosk", "sector": None},
        "sections": [{"heading": "1. Dates", "blocks": [
            {"type": "list", "items": ["a", {"text": "b"}]},
            {"type": "kv", "rows": [["EMD", 800000]]}, {"type": "weird"}]}],
        "key_facts": [{"field": "emd", "value": 800000, "page": "4", "quote": "Rs 8,00,000"}],
        "requirements": [{"category": "turnover", "amount_inr": "80000000"}]})
    b = sloppy.sections[0].blocks
    assert [x.type for x in b] == ["bullets", "kv_table", "paragraph"]
    assert b[1].rows == [["EMD", "800000"]] and sloppy.key_facts[0].page == 4
    assert sloppy.profile.document_type == "Lease of Kiosk"      # free text allowed
    assert sloppy.requirements[0].amount_inr == 80_000_000


# ------------------------------------------------------------------ robust LLM output
class FakeClient:
    """parse() fails like a provider without json_schema; create() returns bad, then good JSON."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = NS(completions=self)

    def parse(self, **kw):
        raise RuntimeError("Invalid response_format: json_schema is not supported")

    def create(self, **kw):
        self.calls.append(kw)
        content, reason = self.replies.pop(0)
        return NS(choices=[NS(message=NS(content=content), finish_reason=reason)],
                  usage=NS(prompt_tokens=10, completion_tokens=5), model="fake")


def test_structured_call_repairs_invalid_json(monkeypatch):
    good = json.loads(SAMPLE.read_text())
    bad = {"profile": good["profile"], "sections": [{"heading": "x", "blocks": [{"type": "table",
           "rows": "not-a-list-of-lists"}]}]}
    fake = FakeClient([("```json\n" + json.dumps(bad) + "\n```", "stop"),
                       ("Sure! " + json.dumps(good) + ",", "stop")])
    monkeypatch.setattr(llm, "_client", lambda: fake)
    monkeypatch.setenv("LLM_MODEL", "fake-model-1")
    syn, usage = llm.structured_call("sys", "user", Synopsis)
    assert syn.profile.synopsis_title.startswith("Tender Synopsis")
    assert usage["repairs"] == 1 and usage["input_tokens"] == 20
    # a form-only error is repaired from the JSON alone: the tender ("user") is not sent again
    assert usage.get("light_repair") and "does not validate" in fake.calls[1]["messages"][-1]["content"]
    assert all(m["content"] != "user" for m in fake.calls[1]["messages"])


def test_structured_call_gives_up_cleanly(monkeypatch):
    fake = FakeClient([("nonsense", "stop")] * 5)
    monkeypatch.setattr(llm, "_client", lambda: fake)
    monkeypatch.setenv("LLM_MODEL", "fake-model-2")
    monkeypatch.setenv("LLM_REPAIR_ATTEMPTS", "1")
    with pytest.raises(llm.LLMError, match="valid synopsis"):
        llm.structured_call("sys", "user", Synopsis)


# ------------------------------------------------------------------ long tenders
def _long_tender() -> TenderText:
    t = TenderText()
    filler = "The contractor shall comply with general conditions of contract. " * 60
    for n in range(1, 121):
        text = filler
        if n == 47:
            text = "Last date of bid submission 15.10.2026 at 15:00 Hrs. EMD Rs. 5,00,000. " + filler
        if n == 88:
            text = "Average annual turnover of Rs. 10 crore. Similar work experience. " + filler
        if n in range(100, 121):
            text = "1.1 2.2 3.3 4.4 5.5 6.6 7.7 8.8 9.9 10.10 11.11 " * 40   # BOQ-like
        t.pages.append(Page("rfp.pdf", n, text))
    t.pages.append(Page("corrigendum_1.pdf", 1, "Corrigendum: last date extended to 22.10.2026."))
    return t


def test_condense_keeps_key_pages_and_notes(monkeypatch):
    seen = []

    def fake_json_call(system, user):
        seen.append(user)
        return {"notes": [{"page": 60, "file": "rfp.pdf", "topic": "x", "fact": "f", "quote": "q"}]}, \
            {"input_tokens": 1, "output_tokens": 1}
    monkeypatch.setattr(llm, "json_call", fake_json_call)
    tender = _long_tender()
    assert condense.tokens(tender.as_prompt_text()) > 20000
    out = condense.condense(tender, budget=20000)
    assert out.mode == "condensed"
    key = out.text.split("=== NOTES")[0]
    assert "15.10.2026" in key and "22.10.2026" in key and "10 crore" in key
    assert condense.tokens(out.text) < 20000 * 1.1
    assert seen and out.notes and "skipped" in out.warnings[0]


def test_short_tender_untouched():
    t = TenderText([Page("a.pdf", 1, "Short tender. EMD Rs 1,000.")])
    out = condense.condense(t, budget=5000)
    assert out.mode == "full" and out.text == t.as_prompt_text()


# ------------------------------------------------------------------ bid / no-bid check
def _req(**kw):
    base = dict(category="other", text="", mandatory=True, option_group="", amount_inr=None,
                count=None, period_years=None, keywords=[], page=1, quote="")
    return Requirement.model_validate(base | kw)


def test_assessment_local_rules():
    syn = Synopsis.model_validate(json.loads(SAMPLE.read_text()))
    syn.requirements = [
        _req(category="turnover", text="Average annual turnover ₹8 Crore (last 3 FY)",
             amount_inr=8e7, period_years=3),
        _req(category="net_worth", text="Net worth ₹10 Crore", amount_inr=1e8),
        _req(category="years_in_existence", text="5 years existence", period_years=5),
        _req(category="similar_work", text="One event of ₹1 Crore in last 3 years",
             amount_inr=1e7, count=1, period_years=3, keywords=["event"], option_group="exp"),
        _req(category="similar_work", text="Two events of ₹80 lakh", amount_inr=8e6, count=2,
             period_years=3, keywords=["event"], option_group="exp"),
        _req(category="registration_or_licence", text="GST registration", keywords=["GST"]),
        _req(category="jv_consortium", text="JV not allowed"),
    ]
    company = json.loads((ROOT / "company_profile.example.json").read_text())["companies"][0]
    company["works"][0]["completed"] = __import__("datetime").date.today().strftime("%d.%m.%Y")
    a = assess.assess(syn, [company])[0]
    st = [r.status for r in a.rows]
    assert st == ["MET", "NOT MET", "MET", "MET", "NOT MET", "MET", "INFO"]
    assert a.verdict == "Not eligible"                 # net worth fails
    company["net_worth_inr"] = 2e8
    assert assess.assess(syn, [company])[0].verdict == "Eligible"   # option group 'exp' passes
    md = assess.to_markdown(syn, [a])
    assert "❌" in md and "not sent to the AI" in md


def test_no_profile_means_no_assessment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_PROFILE", str(tmp_path / "missing.json"))
    syn = Synopsis.model_validate(json.loads(SAMPLE.read_text()))
    assert assess.assess(syn) == []


# ------------------------------------------------------------------ fallback providers
def test_fallback_to_second_provider(monkeypatch):
    good = SAMPLE.read_text()
    monkeypatch.setenv("LLM_API_KEY", "sk-main-key-123")
    monkeypatch.setenv("LLM_MODEL", "main-model")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM2_API_KEY", "gsk_backup_key_456")
    monkeypatch.setenv("LLM2_MODEL", "backup-model")
    monkeypatch.setenv("LLM2_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setattr("tender_synopsis.env.load_env", lambda: None)

    class Dead:
        chat = NS(completions=NS(parse=None, create=None))

        def __init__(self):
            def boom(**kw):
                raise llm.LLMError("Free-tier limit reached (per day).")
            self.chat = NS(completions=NS(parse=boom, create=boom))

    backup = FakeClient([(good, "stop")])
    monkeypatch.setattr(llm, "_client", lambda: Dead() if llm._ACTIVE.slot == "LLM" else backup)
    llm.reset_fallback_state()
    syn, usage = llm.structured_call("sys", "user", Synopsis)
    assert syn.profile.synopsis_title and "backup-model" in usage["provider"]
    assert "main-model" in usage["fallbacks"][0]
    # main provider is now skipped for the rest of the run (daily quota)
    backup.replies.append((json.dumps({"notes": []}), "stop"))
    data, usage2 = llm.json_call("sys", "user")
    assert data == {"notes": []} and "skipped" in usage2["fallbacks"][0]


def test_all_providers_fail_message(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-main-key-123")
    monkeypatch.setenv("LLM2_API_KEY", "gsk_backup_key_456")
    monkeypatch.setenv("LLM2_MODEL", "backup-model")
    monkeypatch.setattr("tender_synopsis.env.load_env", lambda: None)
    monkeypatch.setattr(llm, "_client", lambda: FakeClient([("nonsense", "stop")] * 9))
    monkeypatch.setenv("LLM_REPAIR_ATTEMPTS", "0")
    llm.reset_fallback_state()
    with pytest.raises(llm.LLMError, match="All AI providers failed"):
        llm.structured_call("sys", "user", Synopsis)


# ------------------------------------------------------------------ Gemini AQ. auth keys
def test_gemini_aq_key_accepted_and_sent_as_google_header(monkeypatch, tmp_path):
    import httpx

    from tender_synopsis import env
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=AQ.Ab8RN6Kx0000000000000000000000000000000000000DGbg\n"
        "LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/\n"
        "LLM_MODEL=gemini-flash-latest\n")
    for k in ("LLM2_API_KEY", "LLM3_API_KEY", "LLM4_API_KEY", "GEMINI_KEY_HEADER"):
        monkeypatch.delenv(k, raising=False)
    ok, msg = env.key_report()
    assert ok, msg

    seen = []

    def handler(req):                      # accepts only the Google header, like AQ. keys
        seen.append(dict(req.headers))
        if "x-goog-api-key" not in req.headers:
            return httpx.Response(401, json={"error": {"message": "API key not valid"}})
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0,
                              "model": "gemini", "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": "OK"}}]})
    class Mocked(httpx.Client):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(httpx, "Client", Mocked)
    llm._GOOGLE_FLIPPED.clear()
    assert "✅" in llm.check_connection()
    assert "authorization" not in seen[0] and seen[0]["x-goog-api-key"].startswith("AQ.")


# ------------------------------------------------------------------ multi-document tenders
def test_document_roles():
    from tender_synopsis.ingest import classify
    assert classify("Delhioutdooradvertisingpolicy2017.pdf", "") == "policy"
    assert classify("OOH_Splissued.pdf", "") == "special"
    assert classify("StandardConditionsofContract.pdf", "") == "standard"
    assert classify("SSBList.pdf", "") == "list"
    assert classify("Lot_details.pdf", "") == "main"
    assert classify("Corrigendum-2.pdf", "") == "corrigendum"
    assert classify("scan01.pdf", "Special Conditions of Contract for OOH") == "special"
    assert classify("scan02.pdf", "DELHI OUTDOOR ADVERTISING POLICY 2017") == "policy"


def test_signature_stamps_removed_but_real_lines_kept():
    raw = ("Signature Not  within 15 days of the Bid Sheet.\nVerified\nDigitally signed by\n"
           "Chhavi Sharma\nDate: 2026.07.28\n18:59:37 IST\nReason: IREPS-CRIS\n"
           "Location: New Delhi\nby DelhiNot ISTIREPS-CRISNew signedSharma 2026.07.28"
           "SignatureVerified DigitallyChhaviDate:19:00:38Reason:Location:")
    assert ingest._clean(raw) == "within 15 days of the Bid Sheet."
    keep = "Location: Village Ghummanhera\nDate: 23.09.2026 EOI published"
    assert ingest._clean(keep) == keep


def _pdf_with_image_table(path, rotate=0):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 300), 0)
    pix.clear_with(255)
    page.insert_image(pymupdf.Rect(50, 100, 550, 600), pixmap=pix)
    page.insert_text((60, 800), "Signature Not Verified\nDigitally signed by A Officer\n"
                                "Date: 2026.07.28\nReason: IREPS-CRIS")
    page2 = doc.new_page(width=612, height=792)
    page2.set_rotation(rotate)          # like SSBList.pdf: stored sideways, shown upright
    page2.insert_text((50, 100), "SN Location Units Area\n1 Punjabi Bagh 3 18\n"
                                 "2 Railway Colony Punjabi Bagh 1 18\n3 Peeragarhi 1 18",
                      rotate=rotate)
    doc.save(path)


def test_stamp_only_image_page_is_ocrd_and_rotation_fixed(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ingest, "_ocr_png", lambda png, lang: ("Kishan Ganj Bridge Panel 2 18", None))
    f = tmp_path / "Feasibility.pdf"
    _pdf_with_image_table(f, rotate=90)
    t = ingest.read_files([f])
    p1, p2 = t.pages
    assert p1.ocr and "Kishan Ganj" in p1.text and "Signature" not in p1.text
    assert "Punjabi Bagh 3 18" in p2.text
    assert any("sideways" in w for w in t.warnings)


def test_missing_main_document_warning_and_header(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    for name, text in (("Standard_Conditions.txt", "Standard conditions of contract"),
                       ("Advertising_Policy.txt", "Outdoor advertising policy 2017")):
        (tmp_path / name).write_text(text)
    t = ingest.read_files([tmp_path / "Standard_Conditions.txt", tmp_path / "Advertising_Policy.txt"])
    assert any("No main tender" in w for w in t.warnings)
    head = t.as_prompt_text().split("\n\n")[0]
    assert head.index("Standard_Conditions") < head.index("Advertising_Policy")


def test_condense_prefers_tender_pages_over_policy(monkeypatch):
    monkeypatch.setattr(llm, "json_call", lambda s, u: ({"notes": []}, {}))
    t = TenderText()
    filler = "The licensee shall comply with the conditions of contract and EMD rules. " * 50
    for n in range(1, 41):
        t.pages.append(Page("policy.pdf", n, "EMD eligibility turnover last date " + filler))
    t.pages.append(Page("special.pdf", 1, "Special conditions. " + filler))
    t.pages.append(Page("special.pdf", 2, "Reserve price Rs 5,00,000 EMD 10% " + filler))
    t.roles = {"policy.pdf": "policy", "special.pdf": "special"}
    out = condense.condense(t, budget=8000)
    key = out.text.split("=== NOTES")[0]
    assert "Reserve price Rs 5,00,000" in key and key.startswith("DOCUMENTS IN THIS TENDER")


def test_ocr_space_image_kept_under_1mb():
    import os

    import pymupdf
    noise = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 2480, 3508), 0)
    noise.set_rect(noise.irect, (200, 120, 40))
    for y in range(0, 3508, 3):
        noise.set_rect(pymupdf.IRect(0, y, 2480, y + 1), tuple(os.urandom(3)))
    png = noise.tobytes("png")
    small = ingest._small_jpeg(png)
    assert len(small) < 1_000_000 and small[:2] == b"\xff\xd8"


def test_free_form_categories_are_mapped():
    req = lambda c, t: Requirement.model_validate({"category": c, "text": t}).category
    assert req("financial", "Minimum annual financial turnover of Rs 20 lacs") == "turnover"
    assert req("em", "EMD 10% of annual bid value") == "emd"
    assert req("legal", "Bidder must not be debarred") == "not_blacklisted"
    assert req("turnover", "anything") == "turnover"


def test_ocr_slips_in_amounts_tolerated():
    from tender_synopsis.verify import _numbers
    assert "10000000" in _numbers("Above Rs. I crore Rs. 50 lacs")
    assert "4000000" in _numbers("Above Rs. 40 laes")


def test_ocr_space_breaker_skips_cloud_after_repeated_failures(monkeypatch):
    calls = {"cloud": 0}

    def bad_cloud(png):
        calls["cloud"] += 1
        raise RuntimeError("429 rate limited")
    monkeypatch.setenv("OCR_SPACE_API_KEY", "k")
    monkeypatch.setenv("OCR_SPACE_MAX_FAILS", "2")
    monkeypatch.setattr(ingest, "_ocr_space", bad_cloud)
    monkeypatch.setattr(ingest, "_tesseract_executable", lambda: "tesseract")
    monkeypatch.setattr(ingest.subprocess, "run",
                        lambda *a, **k: NS(returncode=0, stdout="local text", stderr=""))
    ingest._breaker.update(fails=0, open_until=0.0)
    for _ in range(5):
        assert ingest._ocr_png(b"png", "eng")[0] == "local text"
    assert calls["cloud"] == 2                      # then cloud is paused
    assert ingest.ocr_status()["ocr_space_paused_seconds"] > 0
    ingest._breaker.update(fails=0, open_until=0.0)
    monkeypatch.setenv("OCR_ENGINE", "tesseract")   # local only: cloud never called
    ingest._ocr_png(b"png", "eng")
    assert calls["cloud"] == 2


def test_bullets_in_other_shapes_are_kept_not_blanked():
    s = Synopsis.model_validate({"profile": {}, "key_facts_table": [["Particular", "Details"], ["EMD", "10%"]],
        "sections": [{"heading": "8.1 Scope", "blocks": [{"type": "bullets", "items": [
            {"label": "Display plan", "value": "within 15 days"}, {"content": "Bear electricity cost"}]}]}]})
    texts = [i.text for i in s.sections[0].blocks[0].items]
    assert texts == ["**Display plan:** within 15 days", "Bear electricity cost"]
    assert s.key_facts_table == [["EMD", "10%"]]
    from tender_synopsis.verify import check_empty
    empty = Synopsis.model_validate({"profile": {}, "sections": [{"heading": "9. X", "blocks": [
        {"type": "bullets", "items": [{"zzz": 1}]}]}]})
    assert check_empty(empty)


def test_house_highlights_and_tidy():
    from tender_synopsis.schema import Synopsis
    from tender_synopsis.verify import ensure_highlights, tidy_text
    s = Synopsis.model_validate({"profile": {}, "title": "t",
        "key_facts_table": [["Turnover Eligibility", "Nil / ₹20 lakh"], ["EMD", "==10%=="],
                            ["Last Date of Submission", "17.09.2026 18:00"],
                            ["JV / Consortium", "Not stated"], ["Net Worth", "++Positive++"],
                            ["Authority", "DGLL"]],
        "sections": [{"heading": "1. F", "blocks": [{"type": "bullets", "items": [
            {"text": "++Minimum annual turnover ₹20 lakh++"},
            {"text": "Above ₹1 crore: minimum annual turnover ₹50 lakh."},
            {"text": "Net worth ₹5 crore and ++turnover ₹2 crore++"},
            {"text": "That the EMD is retained."}]}]}], "summary": ""})
    assert ensure_highlights(s) >= 5
    tidy_text(s)
    kt = dict((r[0], r[1]) for r in s.key_facts_table)
    assert kt["==Turnover Eligibility=="] == "==Nil / ₹20 lakh=="
    assert kt["==Last Date of Submission=="] == "==17.09.2026 18:00=="
    assert kt["==JV / Consortium=="] == "==Not stated=="
    assert kt["++Net Worth++"] == "++Positive++"
    assert kt["Authority"] == "DGLL" and kt["EMD"] == "==10%=="
    items = [i.text for i in s.sections[0].blocks[0].items]
    assert items[0] == "==Minimum annual turnover ₹20 lakh=="
    assert items[1] == "==Above ₹1 crore: minimum annual turnover ₹50 lakh.=="
    assert items[3] == "The EMD is retained."


def test_linked_docs_warning(tmp_path):
    import pymupdf
    from tender_synopsis.ingest import read_files
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Bid document. Scope of Work: see attached file. " * 5)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 60, 200, 80),
                      "uri": "https://example.gov.in/biddoc/scope.pdf"})
    f = tmp_path / "bid.pdf"
    doc.save(f)
    t = read_files([f])
    assert any("links to 1 other document" in w for w in t.warnings)


def test_lakh_crore_slip_is_an_error():
    from tender_synopsis.verify import _numbers, _scale_slip
    assert _scale_slip(_numbers("₹2,90,00,000"), _numbers("Estimated Bid Value 290000000"))
    assert _scale_slip({"8700000"}, _numbers("Turnover 870 Lakh (s)"))
    assert not _scale_slip(_numbers("₹29,00,00,000"), _numbers("Estimated Bid Value 290000000"))
    assert not _scale_slip(_numbers("₹8,70,00,000"), _numbers("870 Lakh"))


def test_linked_doc_already_uploaded_is_not_reported():
    from tender_synopsis.ingest import _missing_links
    links = {"bid.pdf": ["https://x.gov.in/biddoc/1788773570.pdf", "https://x.gov.in/a/boq123456.xlsx"]}
    w = _missing_links(links, ["bid.pdf", "1788773570.pdf"])
    assert len(w) == 1 and "links to 1 other" in w[0]
    assert _missing_links(links, ["bid.pdf", "1788773570.pdf", "boq123456.xlsx"]) == []


def test_not_stated_requirement_needs_no_quote():
    from tender_synopsis.ingest import Page, TenderText
    from tender_synopsis.schema import Synopsis
    from tender_synopsis.verify import check_requirements
    t = TenderText(pages=[Page("a.pdf", 1, "Some tender text about EMD and dates.")])
    s = Synopsis.model_validate({"profile": {}, "requirements": [{"category": "jv_consortium",
        "text": "Not stated", "quote": "Not stated in the documents", "page": 1}]})
    assert check_requirements(s, t) == []


def test_closed_bid_row_added():
    from tender_synopsis.schema import Synopsis
    from tender_synopsis.verify import mark_closed_bid
    s = Synopsis.model_validate({"profile": {"submission_deadline": "17.09.2020 18:00"},
        "sections": [{"heading": "9. Quick Decision", "blocks": [{"type": "table",
            "headers": ["Question", "Answer"], "rows": [["EMD?", "Yes"]]}]}], "summary": "x"})
    assert mark_closed_bid(s)
    assert "still open" in s.sections[0].blocks[0].rows[0][0]
    assert mark_closed_bid(s) and len(s.sections[0].blocks[0].rows) == 2   # not added twice


# ---- images ------------------------------------------------------------------------------
def _fake_ocr(calls):
    def f(png, lang, cloud=True, psm="3"):
        import io
        from PIL import Image
        w, h = Image.open(io.BytesIO(png)).size
        calls.append((w, h, cloud))
        # pretend text is only readable when the picture is upright (portrait)
        return ("EMD Rs 54,980 Bid closing 17-09-2026 " * 3 if h >= w else "x"), None
    return f


def test_image_screenshot_is_ocrd_and_enlarged(tmp_path, monkeypatch):
    from PIL import Image
    calls = []
    monkeypatch.setattr(ingest, "_ocr_png", _fake_ocr(calls))
    monkeypatch.setattr(ingest, "_available_ocr_lang", lambda: "eng")
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    f = tmp_path / "LotDetails.png"
    Image.new("RGB", (900, 1200), "white").save(f)
    t = ingest.read_files([f])
    assert len(t.pages) == 1 and t.pages[0].ocr and "54,980" in t.pages[0].text
    assert calls[0][0] >= 1800                       # small screenshot enlarged for OCR
    assert t.role("LotDetails.png") == "main"
    assert any("image read with OCR" in w for w in t.warnings)


def test_image_sideways_retry_tiny_skip_and_tiff(tmp_path, monkeypatch):
    from PIL import Image
    calls = []
    monkeypatch.setattr(ingest, "_ocr_png", _fake_ocr(calls))
    monkeypatch.setattr(ingest, "_available_ocr_lang", lambda: "eng")
    monkeypatch.setattr(ingest, "_tesseract_executable", lambda: "tesseract")
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    side = tmp_path / "photo.jpg"
    Image.new("RGB", (2400, 1600), "white").save(side)          # landscape = sideways page
    t = ingest.read_files([side])
    assert "54,980" in t.pages[0].text
    assert any(c[2] is False for c in calls)                   # rotation retries are local only
    tiny = tmp_path / "logo.png"
    Image.new("RGB", (120, 80), "white").save(tiny)
    t = ingest.read_files([tiny])
    assert not t.pages and any("too small" in w for w in t.warnings)
    tif = tmp_path / "scan.tif"
    a = Image.new("RGB", (1700, 2300), "white")
    a.save(tif, save_all=True, append_images=[a.copy()])
    assert [p.number for p in ingest.read_files([tif]).pages] == [1, 2]


def test_exif_orientation_is_applied():
    from PIL import Image
    img = Image.new("RGB", (2000, 1000), "white")
    exif = img.getexif()
    exif[0x0112] = 6                                           # "rotate 90" phone photo
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    out = ingest._prepare_image(Image.open(io.BytesIO(buf.getvalue())))
    assert out.size[1] > out.size[0] and out.mode == "L"


def test_pictures_inside_word_are_read(tmp_path, monkeypatch):
    import docx
    from PIL import Image
    monkeypatch.setattr(ingest, "_ocr_png", _fake_ocr([]))
    monkeypatch.setattr(ingest, "_available_ocr_lang", lambda: "eng")
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    pic = tmp_path / "annex.png"
    Image.new("RGB", (1600, 2200), "white").save(pic)
    logo = tmp_path / "logo.png"
    Image.new("RGB", (100, 60), "white").save(logo)
    d = docx.Document()
    d.add_paragraph("Notice inviting tender for services.")
    d.add_picture(str(pic))
    d.add_picture(str(logo))
    f = tmp_path / "nit.docx"
    d.save(f)
    t = ingest.read_files([f])
    assert any(p.ocr and "54,980" in p.text for p in t.pages)
    assert any("1 picture(s) inside the document" in w for w in t.warnings)


def test_large_photo_upload_is_shrunk(tmp_path):
    import os
    from PIL import Image
    f = tmp_path / "big.png"
    Image.frombytes("RGB", (4200, 3000), os.urandom(4200 * 3000 * 3)).save(f)
    before = f.stat().st_size
    ingest.shrink_image_upload(f)
    assert f.stat().st_size < before * 0.7
    assert max(Image.open(f).size) <= ingest.IMG_MAX_SIDE
    small = tmp_path / "s.png"
    Image.new("RGB", (800, 600), "white").save(small)
    b = small.read_bytes()
    ingest.shrink_image_upload(small)
    assert small.read_bytes() == b


# ---- hard-to-read pages: AI reads a few --------------------------------------------------
GARBAGE = "~ ’ ' :, \\ —— ;f, 1 li\\ ~% 7~ ’! ~ ,, ‘a \\ j¥ % ~~ —_ ri) , 4 &% sh. ——. . \\ ef ~ { ,"


def _ink_image(path, size=(1600, 2200)):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    for y in range(100, size[1] - 100, 40):
        d.line([(100, y), (size[0] - 100, y + 10)], fill="black", width=6)   # "handwriting"
    img.save(path)


def test_handwriting_goes_to_ai_within_budget(tmp_path, monkeypatch):
    from tender_synopsis import llm
    monkeypatch.setattr(ingest, "_ocr_png", lambda png, lang, cloud=True, psm="3": (GARBAGE, None))
    monkeypatch.setattr(ingest, "_available_ocr_lang", lambda: "eng")
    monkeypatch.setattr(ingest, "_tesseract_executable", lambda: None)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    calls = []

    def fake_vision(img, mime="image/jpeg"):
        calls.append(len(img))
        return "Bridge Panel 1 | 18x3 | 54 sq m | FEASIBLE", {"input_tokens": 900, "output_tokens": 40}
    monkeypatch.setattr(llm, "vision_transcribe", fake_vision)
    monkeypatch.setenv("AI_IMAGE_MAX_PAGES", "1")
    a, b = tmp_path / "feasibility1.jpg", tmp_path / "feasibility2.jpg"
    _ink_image(a)
    _ink_image(b, (1700, 2300))                             # a different page
    t = ingest.read_files([a, b])
    assert len(calls) == 1 and max(calls) < 400_000          # one small image sent, budget kept
    assert "18x3" in t.pages[0].text and "Read by AI" in t.pages[0].text
    assert t.ai_pages == 1 and t.ai_usage["input_tokens"] == 900
    assert any("AI reading limit" in w for w in t.warnings)  # second page: limit reached, flagged


def test_good_ocr_and_blank_pages_never_use_ai(tmp_path, monkeypatch):
    from PIL import Image
    from tender_synopsis import llm
    monkeypatch.setattr(ingest, "_ocr_png", lambda png, lang, cloud=True, psm="3":
                        ("Earnest Money Deposit Rs 54,980 payable online before 17.09.2026 " * 3, None))
    monkeypatch.setattr(ingest, "_available_ocr_lang", lambda: "eng")
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(llm, "vision_transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("AI used")))
    f = tmp_path / "scan.png"
    _ink_image(f)
    assert "54,980" in ingest.read_files([f]).pages[0].text
    assert not ingest.ocr_looks_unreliable("", Image.new("L", (1600, 2200), 255))   # blank page


def test_ai_reading_can_be_switched_off(tmp_path, monkeypatch):
    from tender_synopsis import llm
    monkeypatch.setattr(ingest, "_ocr_png", lambda png, lang, cloud=True, psm="3": (GARBAGE, None))
    monkeypatch.setattr(ingest, "_available_ocr_lang", lambda: "eng")
    monkeypatch.setattr(ingest, "_tesseract_executable", lambda: None)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(llm, "vision_transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("AI used")))
    monkeypatch.setenv("AI_IMAGE_READING", "off")
    f = tmp_path / "photo.jpg"
    _ink_image(f)
    t = ingest.read_files([f])
    assert any("could not read this reliably" in w for w in t.warnings)


def test_vision_failure_does_not_block_provider(monkeypatch):
    from tender_synopsis import llm
    llm.reset_fallback_state()
    from openai import APIError
    import httpx
    P = llm.Provider("LLM", "k", "https://x", "m")
    monkeypatch.setattr(llm, "providers", lambda: [P])

    def boom(*a, last=True):
        raise APIError("model not found 404 image input", httpx.Request("POST", "https://x"), body=None)
    try:
        llm._with_fallback(boom, b"img", "image/jpeg", mark_dead=False)
    except llm.LLMError:
        pass
    assert "LLM" not in llm._DEAD                            # main synopsis can still use it


# ---- v20: scanned PDFs with scrambled scanner text, number reading, qualifying score ----------
JUMBLE = ("1 S art d a o I d own o a d n s nd d o c u m n 23.09.2026 3:00 PM "
          "L a S d a e fo t k n o c a T fi o n f a n v 29.09.2026 ") * 6
CLEAN_OCR = "Start date of downloading tender document 23.09.2026 3:00 PM\n" * 3


def _scan_pdf(path, layer_texts):
    """A 'scanned' PDF: each page is one full-page picture plus the scanner's hidden text."""
    import io
    import pymupdf
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (600, 800), 255).save(buf, "PNG")
    doc = pymupdf.open()
    for txt in layer_texts:
        page = doc.new_page()
        page.insert_image(page.rect, stream=buf.getvalue())
        page.insert_textbox(pymupdf.Rect(36, 36, 560, 800), txt, fontsize=7, render_mode=3)
    doc.save(path)


def test_scrambled_scanner_text_is_read_again(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "_ocr_png", lambda png, lang, cloud=True, psm="3": (CLEAN_OCR, None))
    monkeypatch.setattr(ingest, "_available_ocr_lang", lambda: "eng")
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    good = "Earnest Money Deposit of Rs. 1.00 Lakh payable by demand draft before opening. " * 20
    f = tmp_path / "rfp.pdf"
    _scan_pdf(f, [JUMBLE, good, good, good, good, good])     # 1 of 6 scrambled (< 20%)
    t = ingest.read_files([f])
    assert "Start date of downloading" in t.pages[0].text and t.pages[0].ocr
    assert "Earnest Money" in t.pages[1].text and not t.pages[1].ocr   # good text kept as is
    g = tmp_path / "rfp2.pdf"
    _scan_pdf(g, [JUMBLE, JUMBLE, good, good, good])          # many scrambled: re-read all
    t2 = ingest.read_files([g])
    assert all(p.ocr and "Start date" in p.text for p in t2.pages)


def test_text_pdfs_are_never_reread():
    assert not ingest.text_layer_jumbled("Sl. No. Name of Work Rs. in Lakh " * 20)
    assert ingest.text_layer_jumbled(JUMBLE)


def test_numbers_written_lakh_fy_and_scan_slips():
    from tender_synopsis.verify import _numbers
    assert "100000" in _numbers("EMD of Rs. 1.00 (one) Lakh only")
    assert "100000" in _numbers("INR One (1) Lakh (refundable)")
    assert "500000" in _numbers("five lakh")
    assert _numbers("FY 2022-23, 2023-24, 2024-25") == _numbers("FY 2022-23,2023-24, 2024-2025")
    assert "100000" in _numbers("Rs 1,00,000") and "2026" in _numbers("HoH t2OZ6", ocr=True)


def test_scan_garbled_quote_is_warning_but_reworded_quote_is_error():
    from tender_synopsis.verify import check_quotes
    scan = Page("rfp.pdf", 6, "1 Start date of dwnlding tndr dcmnt 23.09.2026 | 3:00 PM", ocr=True)
    text = Page("nit.pdf", 2, "Bids close on 14.10.2026 at 3:00 PM on the portal. " * 3)
    t = TenderText(pages=[scan, text])
    s = Synopsis.model_validate({"profile": {}, "key_facts": [
        {"field": "download_start", "value": "23.09.2026, 3:00 PM", "file": "rfp.pdf", "page": 6,
         "quote": "Start date of downloading documents 23.09.2026 3:00 PM"},
        {"field": "deadline", "value": "21.10.2026", "file": "nit.pdf", "page": 2,
         "quote": "Last date for submission of bids is 21.10.2026"}]})
    sev = {i.field: i.severity for i in check_quotes(s, t)}
    assert sev == {"download_start": "warning", "deadline": "error"}


def test_quote_words_spread_over_tender_do_not_count():
    from tender_synopsis.verify import check_requirements
    pages = [Page("a.pdf", n, w * 30) for n, w in enumerate(
        ["advertising ", "marketing services ", "minimum three years ", "x ", "y ", "consecutive "], 1)]
    s = Synopsis.model_validate({"profile": {}, "requirements": [{"category": "similar_work",
        "text": "3 years advertising", "page": 5,
        "quote": "minimum three years consecutive advertising marketing services"}]})
    assert check_requirements(s, TenderText(pages=pages))[0].severity == "error"


def test_prompt_asks_for_qualifying_score_test():
    from tender_synopsis.prompts import build_system_prompt
    p = build_system_prompt("", "compact")
    assert "minimum eligibility" in p and "Qualifying score" in p and "who may bid" in p


def test_same_time_in_12_and_24_hour_format():
    from tender_synopsis.verify import _numbers
    assert _numbers("14.10.2026 15:00") == _numbers("14.10.2026 3:00 PM")
    assert _numbers("12.00 Noon") == _numbers("12:00 noon") == {"time1200"}
    assert _numbers("15:00 Hrs") == _numbers("3.00 p.m.")
    assert "100000" in _numbers("Rs. 1.00 Lakh")            # amounts are not read as times


def test_table_figures_not_in_documents_are_flagged():
    from tender_synopsis.verify import check_table_figures
    t = TenderText(pages=[Page("nit.pdf", 1,
        "Bid validity 60 (sixty) days. Performance Security 5% of contract value valid for "
        "6 (six) months. Penalty upto 10%. Tender fee Rs. 5,000 + GST. EMD Rs. 1.00 (one) Lakh. "
        "ePBG Percentage(%) 3.00  Duration of ePBG required (Months). 14  Bid Offer Validity 45 (Days). "
        "Blacklisting for three years. GEM/2026/B/7675944")])
    s = Synopsis.model_validate({"profile": {}, "key_facts_table": [
        ["Ref", "GEM/2026/B/7675944"], ["EMD", "₹1,00,000"], ["Fee", "₹5,000 + GST (₹5,900 computed)"],
        ["ePBG", "3% for 14 months; offer valid 45 days"]],
        "sections": [{"key": "x", "heading": "1. Terms", "blocks": [{"type": "table", "text": "",
            "headers": ["Item", "Rule", "Source"], "items": [], "rows": [
                ["Validity", "90 days", "p.1"], ["Security", "5% valid 6 months", "p.1"],
                ["Penalty", "upto 10%; blacklisting 3 years", "p.1"], ["Fee", "₹5,900", "p.1"]]}]}]})
    msgs = [i.message for i in check_table_figures(s, t)]
    assert len(msgs) == 2
    assert any("'90 days'" in m and "Validity" in m for m in msgs)
    assert any("'₹5,900'" in m for m in msgs)                 # not labelled computed there


def test_final_self_check_comes_last():
    from tender_synopsis.prompts import build_system_prompt
    p = build_system_prompt("PAST SYNOPSIS EXAMPLE", "compact")
    assert p.rstrip().endswith("then return the JSON.") and "One clause per cell" in p


def test_compact_prompt_is_strict_and_detailed_is_unchanged():
    from tender_synopsis.prompts import build_system_prompt
    c, d = build_system_prompt("", "compact"), build_system_prompt("", "detailed")
    assert "AT MOST 3-4 PAGES" in c and "LEAVE OUT in compact" in c and "5. COMPACT length" in c
    assert "COMPACT length" not in d and "5-6 pages" in d
    assert "SMALLEST single step" in c and "FULL marks" in c          # rule 22 clarified


def test_long_compact_synopsis_is_flagged():
    from tender_synopsis.verify import check_compact_length, synopsis_words
    row = ["Item", "word " * 20]
    short = Synopsis.model_validate({"profile": {}, "key_facts_table": [row] * 20, "summary": "x"})
    long = Synopsis.model_validate({"profile": {}, "key_facts_table": [row] * 90, "summary": "x"})
    assert check_compact_length(short) == [] and synopsis_words(short) < 1500
    w = check_compact_length(long)
    assert len(w) == 1 and w[0].severity == "warning" and "3-4 page target" in w[0].message
