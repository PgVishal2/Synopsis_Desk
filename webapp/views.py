"""Pages: login, home, new synopsis, job progress/results, history, settings."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)

from . import auth, db, jobs

bp = Blueprint("main", __name__)
ALLOWED = {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


# ------------------------------------------------------------------ helpers
def system_status() -> dict:
    from tender_synopsis.ingest import ocr_status
    from tender_synopsis.llm import providers
    plist = providers()
    return {"providers": [{"slot": p.slot, "label": p.label,
                           "ready": bool(p.key) and bool(p.model)} for p in plist],
            "ocr": ocr_status(), "bid_check": jobs.bid_check_enabled()}


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        from zoneinfo import ZoneInfo
        d = datetime.fromisoformat(iso).astimezone(ZoneInfo("Asia/Kolkata"))
        return d.strftime("%d %b %Y, %H:%M")
    except Exception:
        return iso


bp.add_app_template_filter(_when, "when")


def _job_or_404(job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job:
        abort(404)
    return job


# ------------------------------------------------------------------ auth
@bp.get("/healthz")
def healthz():
    return "ok"


@bp.route("/login", methods=["GET", "POST"])
def login():
    setup = not auth.has_password()
    if request.method == "POST":
        wait = auth.locked_out()
        if wait:
            flash(f"Too many wrong attempts. Try again in {wait // 60 + 1} minutes.", "error")
            return render_template("login.html", setup=setup), 429
        pw = request.form.get("password", "")
        if setup:
            if len(pw) < auth.MIN_LENGTH:
                flash(f"Use at least {auth.MIN_LENGTH} characters.", "error")
            elif pw != request.form.get("confirm", ""):
                flash("The two passwords don't match.", "error")
            else:
                auth.set_password(pw)
                auth.login_user()
                flash("Master password created.", "ok")
                return redirect(url_for("main.home"))
        elif auth.check_password(pw):
            auth.login_user()
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                            else url_for("main.home"))
        else:
            auth.record_fail()
            flash("Wrong password.", "error")
    return render_template("login.html", setup=setup)


@bp.post("/logout")
def logout():
    from flask import session
    session.clear()
    return redirect(url_for("main.login"))


# ------------------------------------------------------------------ pages
@bp.get("/")
@auth.login_required
def home():
    all_jobs = db.list_jobs(limit=500)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    done = [j for j in all_jobs if j["status"] == "done"]
    secs = [j["usage"].get("seconds") for j in done if j["usage"].get("seconds")]
    this_month = [j for j in all_jobs if j["created"].startswith(month)]
    costs = [j["usage"].get("cost_inr") for j in this_month if j["usage"].get("input_tokens")]
    stats = {"total": len(done),
             "month": sum(j["created"].startswith(month) for j in done),
             "month_cost": round(sum(c for c in costs if c is not None), 2) if any(c is not None for c in costs) else None,
             "month_unpriced": sum(c is None for c in costs),
             "avg_minutes": round(sum(secs) / len(secs) / 60, 1) if secs else None,
             "running": sum(j["status"] in ("queued", "running") for j in all_jobs)}
    return render_template("home.html", recent=all_jobs[:6], stats=stats,
                           status=system_status())


@bp.route("/new", methods=["GET", "POST"])
@auth.login_required
def new():
    if request.method == "POST":
        files = [f for f in request.files.getlist("files") if f and f.filename]
        bad = [f.filename for f in files
               if "." + f.filename.rsplit(".", 1)[-1].lower() not in ALLOWED]
        if not files:
            flash("Add at least one tender document.", "error")
        elif bad:
            flash("These files can't be read: " + ", ".join(bad) + ". Use PDF, Word (.docx), "
                  "images (PNG, JPG, TIFF…) or text files.", "error")
        else:
            job_id = jobs.new_job(files, request.form.get("mode", "detailed"),
                                  request.form.get("double_check") == "on")
            return redirect(url_for("main.job", job_id=job_id))
    return render_template("new.html")


@bp.get("/jobs/<job_id>")
@auth.login_required
def job(job_id):
    j = _job_or_404(job_id)
    results = jobs.load_results(job_id) if j["status"] == "done" else None
    return render_template("job.html", job=j, r=results,
                           json_text=json.dumps(results["synopsis"], indent=2,
                                                ensure_ascii=False) if results else "")


@bp.get("/api/jobs/<job_id>")
@auth.login_required
def job_api(job_id):
    j = _job_or_404(job_id)
    return jsonify({k: j[k] for k in ("status", "step", "error", "title")})


@bp.get("/jobs/<job_id>/download/<kind>")
@auth.login_required
def download(job_id, kind):
    j = _job_or_404(job_id)
    name = j["outputs"].get(kind)
    if not name:
        abort(404)
    path = jobs.out_dir(job_id) / name
    if not path.exists():
        abort(404)
    title = (j["title"] or "synopsis")[:80]
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip() or "synopsis"
    ext = {"docx": ".docx", "review": "_review.md", "json": ".json"}[kind]
    return send_file(path, as_attachment=True, download_name=safe + ext)


@bp.post("/jobs/<job_id>/edit")
@auth.login_required
def edit(job_id):
    _job_or_404(job_id)
    data = jobs.load_results(job_id)["synopsis"]
    if data is None:
        abort(404)
    raw = request.form.get("json", "").strip()
    try:
        if raw and request.form.get("use_json") == "on":
            data = json.loads(raw)
        else:
            labels = request.form.getlist("kft_label")
            values = request.form.getlist("kft_value")
            data["key_facts_table"] = [[a.strip(), b.strip()] for a, b in zip(labels, values)
                                       if a.strip() or b.strip()]
            apply_section_edits(data, request.form)
            data["summary"] = request.form.get("summary", data.get("summary", ""))
            data["profile"]["synopsis_title"] = request.form.get(
                "title", data["profile"].get("synopsis_title", ""))
            data["review_notes"] = [n.strip() for n in
                                    request.form.get("notes", "").splitlines() if n.strip()]
        jobs.apply_edits(job_id, data)
        flash("Changes saved. The Word file was rebuilt.", "ok")
    except Exception as e:
        flash(f"Changes not saved: {e}", "error")
    return redirect(url_for("main.job", job_id=job_id) + "#edit")


def apply_section_edits(data: dict, form) -> None:
    """Section tables / bullets / paragraphs edited as text: one row or bullet per line, table
    columns separated by '|'. Only blocks present in the form change."""
    for si, sec in enumerate(data.get("sections") or []):
        for bi, b in enumerate(sec.get("blocks") or []):
            raw = form.get(f"sec_{si}_{bi}")
            if raw is None:
                continue
            lines = [ln.strip() for ln in raw.replace("\r", "").split("\n") if ln.strip()]
            if b.get("type") in ("table", "kv_table"):
                width = len(b.get("headers") or []) or (2 if b.get("type") == "kv_table" else 0)
                rows = []
                for ln in lines:
                    cells = [c.strip() for c in ln.split("|")]
                    if width and len(cells) > width:          # a '|' inside the last cell
                        cells = cells[:width - 1] + [" | ".join(cells[width - 1:])]
                    if width and len(cells) < width:
                        cells += [""] * (width - len(cells))
                    rows.append(cells)
                b["rows"] = rows
            elif b.get("type") in ("bullets", "numbered"):
                old = {it.get("text"): it for it in b.get("items") or [] if isinstance(it, dict)}
                b["items"] = [{"text": ln, "sub_items": (old.get(ln) or {}).get("sub_items", [])}
                              for ln in lines]
            elif b.get("type") == "paragraph":
                b["text"] = " ".join(lines)


@bp.post("/jobs/<job_id>/delete")
@auth.login_required
def delete(job_id):
    jobs.delete(job_id)
    flash("Synopsis deleted.", "ok")
    return redirect(url_for("main.history"))


@bp.get("/history")
@auth.login_required
def history():
    q = request.args.get("q", "").strip().lower()
    items = db.list_jobs(limit=1000)
    if q:
        items = [j for j in items if q in (j["title"] or "").lower()
                 or any(q in f.lower() for f in j["files"])]
    return render_template("history.html", items=items, q=q)


@bp.get("/settings")
@auth.login_required
def settings():
    return render_template("settings.html", status=system_status())


@bp.post("/settings/password")
@auth.login_required
def change_password():
    cur, new_pw = request.form.get("current", ""), request.form.get("new", "")
    if not auth.check_password(cur):
        flash("Current password is wrong.", "error")
    elif len(new_pw) < auth.MIN_LENGTH:
        flash(f"Use at least {auth.MIN_LENGTH} characters.", "error")
    elif new_pw != request.form.get("confirm", ""):
        flash("The two new passwords don't match.", "error")
    else:
        auth.set_password(new_pw)
        flash("Master password changed.", "ok")
    return redirect(url_for("main.settings"))


@bp.post("/settings/test-ai")
@auth.login_required
def test_ai():
    from tender_synopsis.llm import LLMError, check_connection
    try:
        return jsonify({"ok": True, "lines": check_connection().splitlines()})
    except LLMError as e:
        return jsonify({"ok": False, "lines": str(e).splitlines()})
