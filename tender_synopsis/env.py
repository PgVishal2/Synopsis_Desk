"""Load settings from the .env file and explain key problems in plain words.

The .env file is re-read on every run, so edits take effect without restarting the app.
Handles common Windows/Notepad issues: a file saved as ".env.txt", a hidden BOM character,
quotes or spaces around the key, and the placeholder text left in place.
"""
import os
import re
from pathlib import Path

KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
PLACEHOLDERS = ("paste", "your-", "sk-...", "sk-or-...", "...")


def env_file() -> Path | None:
    for folder in (Path.cwd(), Path(__file__).resolve().parent.parent):
        for name in (".env", ".env.txt", "env.txt"):
            f = folder / name
            if f.is_file():
                return f
    return None


def is_placeholder(v: str) -> bool:
    return not v or any(p in v.lower() for p in PLACEHOLDERS)


_is_placeholder = is_placeholder


def load_env() -> Path | None:
    """Read .env; the file wins over old values. Last real (non-placeholder) line wins."""
    f = env_file()
    if not f:
        return None
    values: dict[str, str] = {}
    text = f.read_text(encoding="utf-8-sig", errors="ignore")      # utf-8-sig drops BOM
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().upper()
        v = v.split(" #")[0].strip().strip('"').strip("'").strip()
        is_key = k.endswith("_API_KEY")
        v = "".join(ch for ch in v if ch.isprintable() and not ch.isspace()) if is_key else v
        if is_key and _is_placeholder(v):
            continue
        values[k] = v
    for k in [k for k in os.environ if re.match(r"^LLM[2-9]_", k) and k not in values]:
        del os.environ[k]                 # fallback removed from .env -> forget it
    for k, v in values.items():          # LLM_*, LLM2_*..LLM4_*, tuning options, COMPANY_PROFILE
        if k in KEYS or re.match(r"^(LLM\d?_|OCR_SPACE_|COMPANY_PROFILE$)", k):
            os.environ[k] = v
    return f


def key_report() -> tuple[bool, str]:
    """(ok, message) describing the key the app will use - safe to show (key is masked)."""
    f = load_env()
    key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base = os.getenv("LLM_BASE_URL", "")
    model = os.getenv("LLM_MODEL", "gpt-6-luna")
    where = f"settings file: {f.name}" if f else "no .env file found"
    if not key:
        return False, (f"No API key found ({where}). Open the .env file, put the key after "
                       "LLM_API_KEY= on one line, save, and try again.")
    masked = f"{key[:4]}…{key[-4:]} ({len(key)} characters)"
    problems = []
    if "googleapis" in base and not key.startswith(("AIza", "AQ.")):
        problems.append("Gemini keys start with 'AIza' (older) or 'AQ.' (new auth keys) – this "
                        "key doesn't, so it is probably the wrong key or copied incompletely.")
    if "googleapis" in base and key.startswith("AIza") and len(key) != 39:
        problems.append("Gemini keys are 39 characters long – this one isn't; copy it again "
                        "using the copy button in AI Studio.")
    if "openrouter" in base and not key.startswith("sk-or-"):
        problems.append("OpenRouter keys start with 'sk-or-'.")
    if not base and key.startswith(("AIza", "AQ.")):
        problems.append("This looks like a Gemini key but LLM_BASE_URL is empty (that means "
                        "OpenAI). Set LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/")
    msg = f"Using key {masked}, model '{model}', {where}."
    if f and f.name != ".env":
        msg += f" (Note: file is named '{f.name}'; it works, but '.env' is the usual name.)"
    return (not problems), msg + ("" if not problems else " PROBLEM: " + " ".join(problems))
