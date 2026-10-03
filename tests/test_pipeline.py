"""Offline end-to-end test: the LLM call is replaced with a saved synopsis JSON.
Run:  python -m pytest -q tests   (no API key needed)"""
import json
from pathlib import Path

from tender_synopsis import llm, pipeline
from tender_synopsis.schema import Synopsis

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "model_output" / "DSIIDC_RAMP_RFP_synopsis.json"
TENDER = ROOT / "samples" / "input" / "DSIIDC_RAMP_RFP.pdf"


def test_generate_offline(tmp_path, monkeypatch):
    saved = Synopsis.model_validate(json.loads(SAMPLE.read_text()))
    seen = {}

    def fake_call(system, user, schema):
        seen["system"], seen["user"] = system, user
        return saved, {"model": "offline-test"}
    monkeypatch.setattr(llm, "structured_call", fake_call)
    res = pipeline.generate([TENDER], tmp_path, "compact", double_check=False,
                            exclude_example="DSIIDC")
    assert res.docx_path.exists() and res.docx_path.stat().st_size > 10_000
    assert "[[PAGE 8]]" in seen["user"] and "Tender Summary" in seen["user"]
    assert "DSIIDC_RAMP_RFP)" not in seen["system"]          # own example excluded
    assert "Ghummanhera" in seen["system"]                     # other examples included
    assert not [i for i in res.issues if i.severity == "error"]


def test_bad_quote_is_flagged(tmp_path):
    from tender_synopsis.ingest import read_files
    from tender_synopsis.verify import check_quotes
    syn = Synopsis.model_validate(json.loads(SAMPLE.read_text()))
    syn.key_facts[1].value = "₹9,00,000"
    syn.key_facts[1].quote = "Rs. 9,00,000/- (Nine Lac Rupees only)"
    issues = check_quotes(syn, read_files([TENDER]))
    assert any(i.severity == "error" and i.field == "emd" for i in issues)
