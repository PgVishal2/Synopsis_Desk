"""API-credit optimisation: every billed call is counted, repairs are cheap when they can be,
the second check sends each page once, and the ₹ cost is computed. (Offline - AI mocked.)"""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tender_synopsis import llm, pipeline
from tender_synopsis.ingest import Page, TenderText
from tender_synopsis.schema import KeyFact, Synopsis

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "model_output" / "DSIIDC_RAMP_RFP_synopsis.json"
TENDER = ROOT / "samples" / "input" / "DSIIDC_RAMP_RFP.pdf"


class Fake:
    def __init__(self, replies, tokens=(1000, 200), cached=0):
        self.replies, self.calls, self.tokens, self.cached = list(replies), [], tokens, cached
        self.chat = NS(completions=self)

    def parse(self, **kw):
        raise RuntimeError("Invalid response_format: json_schema is not supported")

    def create(self, **kw):
        self.calls.append(kw)
        content = self.replies.pop(0)
        return NS(choices=[NS(message=NS(content=content), finish_reason="stop")],
                  usage=NS(prompt_tokens=self.tokens[0], completion_tokens=self.tokens[1],
                           prompt_cache_hit_tokens=self.cached), model="fake")


def test_missing_field_repair_keeps_full_context(monkeypatch):
    good = json.loads(SAMPLE.read_text())
    bad = {k: v for k, v in good.items() if k != "profile"}            # required field missing
    fake = Fake([json.dumps(bad), json.dumps(good)])
    monkeypatch.setattr(llm, "_client", lambda: fake)
    monkeypatch.setenv("LLM_MODEL", "fake-credits-1")
    syn, usage = llm.structured_call("sys", "THE TENDER TEXT", Synopsis)
    assert any(m["content"] == "THE TENDER TEXT" for m in fake.calls[1]["messages"])
    assert not usage.get("light_repair")


def test_light_repair_falls_back_to_full_context(monkeypatch):
    good = json.loads(SAMPLE.read_text())
    bad = dict(good, sections=[{"heading": "x", "blocks": [{"type": "table", "rows": "oops"}]}])
    fake = Fake([json.dumps(bad), "still broken", json.dumps(good)])
    monkeypatch.setattr(llm, "_client", lambda: fake)
    monkeypatch.setenv("LLM_MODEL", "fake-credits-2")
    syn, usage = llm.structured_call("sys", "THE TENDER TEXT", Synopsis)
    assert all(m["content"] != "THE TENDER TEXT" for m in fake.calls[1]["messages"])   # light
    assert any(m["content"] == "THE TENDER TEXT" for m in fake.calls[2]["messages"])   # full
    assert syn.profile.synopsis_title


def test_verify_input_sends_each_page_once():
    t = TenderText()
    t.pages = [Page("nit.pdf", n, f"PAGE-{n}-TEXT " * 50) for n in range(1, 6)]
    facts = [KeyFact(field="emd", value="Rs 5 lakh", page=2, quote="x"),
             KeyFact(field="deadline", value="10-11-2026", page=2, quote="y"),
             KeyFact(field="turnover", value="Rs 2 crore", page=3, quote="z")]
    text = pipeline.verify_input(facts, t)
    assert text.count("[[PAGE 2]]") == 1 and text.count("[[PAGE 3]]") == 1 and "[[PAGE 4]]" in text
    for f in ("emd", "deadline", "turnover"):
        assert f"FIELD: {f}" in text
    old_size = sum(len(t.page_text(k.page)[:6000]) + len(t.page_text(k.page + 1)[:2500]) for k in facts)
    assert len(text) < old_size


def test_every_call_counted_and_priced(tmp_path, monkeypatch):
    """Main call + second check are both counted; ₹ cost uses .env prices; cached input cheaper."""
    good = SAMPLE.read_text()
    checks = json.dumps({"checks": []})
    fake = Fake([good, checks], tokens=(50_000, 4_000), cached=9_000)
    monkeypatch.setattr(llm, "_client", lambda: fake)
    monkeypatch.setattr("tender_synopsis.env.load_env", lambda: None)
    for k in ("LLM2_API_KEY", "LLM3_API_KEY", "LLM4_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-test-credits")
    monkeypatch.setenv("LLM_MODEL", "fake-credits-3")
    monkeypatch.setenv("LLM_PRICE_IN", "0.30")
    monkeypatch.setenv("LLM_PRICE_OUT", "1.20")
    monkeypatch.setenv("LLM_PRICE_CACHED", "0.03")
    monkeypatch.setenv("USD_INR", "88")
    res = pipeline.generate([TENDER], tmp_path, "compact", double_check=True, exclude_example="DSIIDC")
    u = res.usage
    assert u["calls"] == 2 and u["input_tokens"] == 100_000 and u["output_tokens"] == 8_000
    expected = ((100_000 - 18_000) * 0.30 + 18_000 * 0.03 + 8_000 * 1.20) / 1e6 * 88
    assert u["cost_inr"] == pytest.approx(expected, abs=0.01)
    assert "₹" in llm.usage_line(u) and "₹" in res.review_path.read_text(encoding="utf-8")
    assert not llm.ledger_active()                     # stopped after the run


def test_no_prices_means_no_rupee_figure():
    llm.start_ledger()
    try:
        llm._LEDGER.calls.append({"slot": "LLM9", "provider": "x", "input": 10, "output": 5, "cached": 0})
        tot = llm.ledger_totals()
    finally:
        llm.stop_ledger()
    assert tot["cost_inr"] is None and tot["input_tokens"] == 10


def test_light_repair_that_changes_content_is_rejected(monkeypatch):
    """If the cheap repair alters any value/quote, it is thrown away and the full repair runs."""
    good = json.loads(SAMPLE.read_text())
    bad = dict(good, sections=[{"heading": "x", "blocks": [{"type": "table", "rows": "oops"}]}])
    tampered = json.loads(json.dumps(good))
    tampered["key_facts"][0]["value"] = "SOMETHING ELSE"
    fake = Fake([json.dumps(bad), json.dumps(tampered), json.dumps(good)])
    monkeypatch.setattr(llm, "_client", lambda: fake)
    monkeypatch.setenv("LLM_MODEL", "fake-credits-4")
    syn, usage = llm.structured_call("sys", "THE TENDER TEXT", Synopsis)
    assert any(m["content"] == "THE TENDER TEXT" for m in fake.calls[2]["messages"])  # full repair
    assert syn.key_facts[0].value == good["key_facts"][0]["value"]
