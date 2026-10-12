"""Tender Synopsis Generator - Flask web app.

Local run:   python -m flask --app wsgi run --debug
Production:  gunicorn wsgi:app (see Dockerfile / railway.json)
"""
from __future__ import annotations

import os
import re
from datetime import timedelta

from flask import Flask
from markupsafe import Markup, escape


def create_app() -> Flask:
    from tender_synopsis.env import load_env
    load_env()                                   # .env locally; Railway uses real env vars

    from . import auth, db, jobs
    db.init()
    db.mark_interrupted()
    jobs.cleanup_old()
    auth.seed_from_env()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=auth.secret_key(),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.getenv("SESSION_HOURS", "12"))),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("HTTPS_ONLY")),
    )

    from .views import bp
    app.register_blueprint(bp)

    @app.before_request
    def _csrf():
        from flask import request
        if request.endpoint not in ("main.healthz",):
            auth.check_csrf()

    @app.context_processor
    def _ctx():
        from tender_synopsis.config import org
        return {"csrf_token": auth.csrf_token, "org_name": org()["name"]}

    app.add_template_filter(rich, "rich")
    from tender_synopsis.llm import usage_line
    app.add_template_filter(usage_line, "usage_line")
    app.add_template_filter(lambda h: re.sub(r"^\s*\d+[.)]\s*", "", h or ""), "strip_num")
    return app


_MARK = re.compile(r"(\*\*|==|\+\+)")


def rich(text: str | None) -> Markup:
    """Synopsis markup -> HTML: **bold**, ==yellow highlight==, ++cyan highlight++."""
    out, open_tags = [], []
    tags = {"**": ("<strong>", "</strong>"), "==": ('<mark class="hl-y">', "</mark>"),
            "++": ('<mark class="hl-c">', "</mark>")}
    for part in _MARK.split(text or ""):
        if part in tags:
            if part in open_tags:
                # close back to this tag, then reopen inner ones
                inner = open_tags[open_tags.index(part) + 1:]
                for t in reversed(inner):
                    out.append(tags[t][1])
                out.append(tags[part][1])
                open_tags.remove(part)
                for t in inner:
                    out.append(tags[t][0])
            else:
                open_tags.append(part)
                out.append(tags[part][0])
        else:
            out.append(str(escape(part)))
    for t in reversed(open_tags):
        out.append(tags[t][1])
    return Markup("".join(out))
