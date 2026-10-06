"""Offline tests for the Flask app (AI calls mocked). Run: python -m pytest -q tests"""
import importlib
import io
import json
import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "model_output" / "DSIIDC_RAMP_RFP_synopsis.json"
TENDER = ROOT / "samples" / "input" / "DSIIDC_RAMP_RFP.pdf"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MASTER_PASSWORD", raising=False)
    import webapp.db
    importlib.reload(webapp.db)
    import webapp.auth, webapp.jobs, webapp.views, webapp
    for m in (webapp.auth, webapp.jobs, webapp.views, webapp):
        importlib.reload(m)

    from tender_synopsis import pipeline
    from tender_synopsis.ingest import read_files
    from tender_synopsis.schema import Synopsis

    def fake_generate(files, out_dir, detail, double_check, name=None, profiles=None, progress=None):
        progress and progress("Writing the synopsis…")
        syn = Synopsis.model_validate(json.loads(SAMPLE.read_text()))
        return pipeline.finish(syn, read_files(files), Path(out_dir), name, [Path(f).name for f in files],
                               {"model": "fake", "seconds": 3}, profiles=profiles)
    monkeypatch.setattr(pipeline, "generate", fake_generate)
    app = webapp.create_app()
    app.config["TESTING"] = True
    return app.test_client()


def csrf(c, url):
    html = c.get(url).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_full_flow(client):
    c = client
    assert c.get("/healthz").data == b"ok"
    assert c.get("/").status_code == 302                      # must log in
    page = c.get("/login").get_data(as_text=True)
    assert "Create the master password" in page              # first run = setup
    tok = csrf(c, "/login")
    r = c.post("/login", data={"csrf": tok, "password": "short", "confirm": "short"})
    assert "at least 8" in r.get_data(as_text=True)
    r = c.post("/login", data={"csrf": tok, "password": "tender-pass-1", "confirm": "tender-pass-1"})
    assert r.status_code == 302
    assert "Generate document synopsis" in c.get("/").get_data(as_text=True)

    # upload -> background job -> results
    tok = csrf(c, "/new")
    r = c.post("/new", data={"csrf": tok, "mode": "compact", "double_check": "on",
                             "files": (io.BytesIO(TENDER.read_bytes()), "DSIIDC RFP.pdf")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    job_url = r.headers["Location"]
    job_id = job_url.rsplit("/", 1)[-1]
    for _ in range(100):
        st = c.get(f"/api/jobs/{job_id}").get_json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert st["status"] == "done", st
    html = c.get(job_url).get_data(as_text=True)
    assert "Synopsis preview" in html and "Documents read" in html or "Key facts" in html
    assert '<mark class="hl-y">' in html or '<mark class="hl-c">' in html
    d = c.get(f"/jobs/{job_id}/download/docx")
    assert d.status_code == 200 and d.data[:2] == b"PK"

    # edit
    tok = csrf(c, job_url)
    r = c.post(f"/jobs/{job_id}/edit", data={"csrf": tok, "title": "Edited title",
                                             "kft_label": ["EMD"], "kft_value": ["==₹8,00,000=="],
                                             "summary": "Short.", "notes": "one\ntwo"})
    assert r.status_code == 302
    html = c.get(job_url).get_data(as_text=True)
    assert "Edited title" in html
    assert "Edited title" in c.get("/history?q=edited").get_data(as_text=True)
    assert "Test now" in c.get("/settings").get_data(as_text=True)


def test_csrf_and_lockout(client):
    c = client
    tok = csrf(c, "/login")
    c.post("/login", data={"csrf": tok, "password": "tender-pass-1", "confirm": "tender-pass-1"})
    c.post("/logout", data={"csrf": tok})
    assert c.post("/login", data={"password": "x"}).status_code == 400          # no CSRF token
    tok = csrf(c, "/login")
    for _ in range(5):
        c.post("/login", data={"csrf": tok, "password": "wrong"})
    r = c.post("/login", data={"csrf": tok, "password": "tender-pass-1"})
    assert r.status_code == 429                                                  # locked out


def test_rich_markup_is_escaped():
    from webapp import rich
    out = str(rich("<script>x</script> ==**EMD**== ++₹8 Cr++"))
    assert "<script>" not in out and '<mark class="hl-y"><strong>EMD</strong></mark>' in out
    assert '<mark class="hl-c">₹8 Cr</mark>' in out


def test_image_upload_accepted(client, monkeypatch):
    from PIL import Image
    from tender_synopsis import ingest
    monkeypatch.setattr(ingest, "_ocr_png", lambda png, lang, cloud=True, psm="3": ("EMD 54980 " * 10, None))
    c = client
    tok = csrf(c, "/login")
    c.post("/login", data={"csrf": tok, "password": "tender-pass-1", "confirm": "tender-pass-1"})
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(buf, "PNG")
    tok = csrf(c, "/new")
    r = c.post("/new", data={"csrf": tok, "mode": "compact",
                             "files": (io.BytesIO(buf.getvalue()), "LotDetails.png")},
               content_type="multipart/form-data")
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]
    tok = csrf(c, "/new")
    r = c.post("/new", data={"csrf": tok, "files": (io.BytesIO(b"x"), "boq.xlsx")},
               content_type="multipart/form-data")
    assert "be read: boq.xlsx" in r.get_data(as_text=True)
