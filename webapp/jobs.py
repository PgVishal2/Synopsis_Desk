"""Background synopsis jobs.

A synopsis takes 1-5 minutes, longer than a web request should wait, so each upload becomes
a job that runs in a small thread pool. The page polls /api/jobs/<id> for progress.
Results are kept on disk in DATA_DIR/jobs/<id>/ and listed in the jobs table.
"""
from __future__ import annotations

import json
import os
import shutil
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db

WORKERS = max(1, int(os.getenv("JOB_WORKERS", "2")))
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="synopsis")


def bid_check_enabled() -> bool:
    return os.getenv("ENABLE_BID_CHECK", "false").lower() in ("1", "true", "yes")


def new_job(uploads: list, mode: str, double_check: bool) -> str:
    """uploads: werkzeug FileStorage objects. Saves them and queues the job."""
    from werkzeug.utils import secure_filename
    from tender_synopsis.ingest import shrink_image_upload
    job_id = uuid.uuid4().hex[:12]
    inp = db.job_dir(job_id) / "input"
    inp.mkdir(parents=True, exist_ok=True)
    names = []
    for f in uploads:
        name = secure_filename(f.filename or "") or f"file{len(names) + 1}.pdf"
        while name in names:
            name = "_" + name
        f.save(inp / name)
        shrink_image_upload(inp / name)          # large photos: smaller copy, same name
        names.append(name)
    db.create_job(job_id, names, mode, double_check)
    _pool.submit(_run, job_id)
    return job_id


def inputs(job_id: str) -> list[Path]:
    d = db.job_dir(job_id) / "input"
    return sorted(d.iterdir()) if d.exists() else []


def out_dir(job_id: str) -> Path:
    return db.job_dir(job_id) / "out"


def _save_extras(job_id: str, res, tender=None) -> None:
    """Issues and document roles are not in the synopsis JSON, so keep them beside it."""
    from tender_synopsis.pipeline import documents_table
    od = out_dir(job_id)
    from tender_synopsis.pipeline import fix_first
    first = fix_first(res.issues)
    (od / "issues.json").write_text(json.dumps([dict(i.to_dict(), fix_first=i in first)
                                                for i in res.issues]), "utf-8")
    if tender is not None:
        docs = [{k: v for k, v in r.items() if k != "rank"} for r in documents_table(tender)]
        (od / "documents.json").write_text(json.dumps(docs), "utf-8")


def _record(job_id: str, res) -> None:
    counts = {s: sum(i.severity == s for i in res.issues) for s in ("error", "warning", "info")}
    db.update_job(job_id, status="done", step="Finished", finished=db.now(),
                  title=res.synopsis.profile.synopsis_title, usage=res.usage, counts=counts,
                  outputs={"docx": res.docx_path.name, "review": res.review_path.name,
                           "json": res.json_path.name})


def _run(job_id: str) -> None:
    from tender_synopsis.ingest import read_files
    from tender_synopsis.llm import friendly
    from tender_synopsis.pipeline import generate
    job = db.get_job(job_id)
    db.update_job(job_id, status="running", step="Starting…")
    try:
        res = generate(inputs(job_id), out_dir(job_id), job["mode"], bool(job["double_check"]),
                       name="synopsis", profiles=None if bid_check_enabled() else [],
                       progress=lambda msg: db.update_job(job_id, step=msg))
        _save_extras(job_id, res, read_files(inputs(job_id)))   # cached: instant
        _record(job_id, res)
    except Exception as e:                       # shown on the job page
        traceback.print_exc()
        db.update_job(job_id, status="failed", finished=db.now(), step="Stopped",
                      error=friendly(e)[:2000], usage=getattr(e, "usage", None) or {})


def apply_edits(job_id: str, synopsis_data: dict) -> None:
    """Re-render the Word file and checks after edits made on the results page (no AI call)."""
    from tender_synopsis.ingest import read_files
    from tender_synopsis.pipeline import finish
    from tender_synopsis.schema import Synopsis
    syn = Synopsis.model_validate(synopsis_data)
    tender = read_files(inputs(job_id))
    job = db.get_job(job_id)
    from tender_synopsis.verify import check_compact_length
    extra = check_compact_length(syn) if job.get("mode") == "compact" else []
    res = finish(syn, tender, out_dir(job_id), "synopsis", [p.name for p in inputs(job_id)],
                 usage=job.get("usage") or {}, extra_issues=extra,
                 profiles=None if bid_check_enabled() else [])
    _save_extras(job_id, res, tender)
    _record(job_id, res)


def load_results(job_id: str) -> dict:
    od = out_dir(job_id)

    def rd(name, default):
        p = od / name
        try:
            return json.loads(p.read_text("utf-8")) if p.exists() else default
        except ValueError:
            return default
    return {"synopsis": rd("synopsis.json", None), "issues": rd("issues.json", []),
            "documents": rd("documents.json", []),
            "bid_check": (od / "synopsis_bid_check.md").read_text("utf-8")
            if (od / "synopsis_bid_check.md").exists() and bid_check_enabled() else ""}


def delete(job_id: str) -> None:
    shutil.rmtree(db.job_dir(job_id), ignore_errors=True)
    db.delete_job(job_id)


def cleanup_old() -> None:
    """Delete jobs older than KEEP_DAYS (default 90) to keep the volume small."""
    days = int(os.getenv("KEEP_DAYS", "90"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for j in db.list_jobs(limit=100000):
        try:
            if datetime.fromisoformat(j["created"]) < cutoff:
                delete(j["id"])
        except ValueError:
            pass
