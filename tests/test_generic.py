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
    assert "did not validate" in fake.calls[1]["messages"][-1]["content"]


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
