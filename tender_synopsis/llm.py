"""LLM access. Any OpenAI-compatible API works - switch provider via environment variables:

  LLM_API_KEY   your key
  LLM_MODEL     default gpt-6-luna
  LLM_BASE_URL  blank for OpenAI; e.g. https://openrouter.ai/api/v1 for OpenRouter,
                https://generativelanguage.googleapis.com/v1beta/openai/ for Gemini

Fallbacks: add LLM2_API_KEY / LLM2_BASE_URL / LLM2_MODEL (and LLM3_..., LLM4_...). If a
provider fails - key rejected, quota used up, model not found, rate limit that does not clear,
request too large, or output that stays invalid - the same request goes to the next provider.
Providers that fail for a lasting reason (key, daily quota, missing model) are skipped for the
rest of the run, so long tenders don't retry them on every part.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
DEFAULT_MODEL = "gpt-6-luna"


class LLMError(RuntimeError):
    """Error with a plain-language explanation for the user."""


SLOTS = ("LLM", "LLM2", "LLM3", "LLM4")


@dataclass(frozen=True)
class Provider:
    slot: str             # "LLM", "LLM2", ...
    key: str
    base_url: str
    model: str

    @property
    def label(self) -> str:
        host = (self.base_url.split("//")[-1].split("/")[0] or "api.openai.com")
        return f"{self.model} @ {host}"


def providers() -> list[Provider]:
    """Configured providers in order: main first, then fallbacks with a real key."""
    from .env import is_placeholder, load_env
    load_env()                                     # re-reads .env every time
    out = []
    for slot in SLOTS:
        key = os.getenv(f"{slot}_API_KEY") or (os.getenv("OPENAI_API_KEY") if slot == "LLM" else "")
        if slot != "LLM" and (not key or is_placeholder(key)):
            continue
        out.append(Provider(slot, key or "", os.getenv(f"{slot}_BASE_URL", ""),
                            os.getenv(f"{slot}_MODEL") or (DEFAULT_MODEL if slot == "LLM" else "")))
    return out


_ACTIVE: Provider | None = None              # provider the current call is using
_DEAD: dict[str, str] = {}                   # slot -> lasting failure reason (this run)


def _client():
    from openai import OpenAI

    p = _ACTIVE or providers()[0]
    if p.slot == "LLM":
        from .env import key_report
        ok, report = key_report()
        if not p.key or not ok:
            raise LLMError(report)
    elif not p.model:
        raise LLMError(f"{p.slot}_MODEL is empty in .env.")
    if _google_header_mode(p):
        import httpx
        return OpenAI(api_key=p.key, base_url=p.base_url or None, timeout=600,
                      http_client=httpx.Client(timeout=600, event_hooks={"request": [_to_goog_header]}))
    return OpenAI(api_key=p.key, base_url=p.base_url or None, timeout=600)


# Google's new Gemini "auth keys" (AQ.…) are rejected by some endpoints when sent the OpenAI way
# (Authorization: Bearer). For those keys the key is sent in Google's own x-goog-api-key header
# instead. GEMINI_KEY_HEADER=auto|header|bearer; auto tries the other way once if the key is rejected.
_GOOGLE_FLIPPED: set[str] = set()


def _is_google_aq(p: Provider) -> bool:
    return "googleapis.com" in p.base_url and p.key.startswith("AQ.")


def _google_header_mode(p: Provider) -> bool:
    if "googleapis.com" not in p.base_url:
        return False
    mode = os.getenv("GEMINI_KEY_HEADER", "auto").lower()
    if mode in ("header", "bearer"):
        return mode == "header"
    default = p.key.startswith("AQ.")
    return (not default) if p.slot in _GOOGLE_FLIPPED else default


def _to_goog_header(request) -> None:
    auth = request.headers.pop("Authorization", "")
    if auth.startswith("Bearer "):
        request.headers["x-goog-api-key"] = auth[7:]


def _try_google_flip(p: Provider, e: Exception) -> bool:
    """For an AQ. Gemini key rejected as unauthorised: switch the header style once."""
    if (_is_google_aq(p) and p.slot not in _GOOGLE_FLIPPED and _is_auth(e)
            and os.getenv("GEMINI_KEY_HEADER", "auto").lower() == "auto"):
        _GOOGLE_FLIPPED.add(p.slot)
        return True
    return False


LASTING = ("api key", "api_key", "401", "403", "unauthenticated", "per day", "perday",
           "daily", "insufficient_quota", "billing", "not found", "404", "does not exist",
           "decommissioned")


def _with_fallback(fn, *args, mark_dead: bool = True):
    """Run fn on each provider in turn until one succeeds. Returns (result, usage).
    mark_dead=False: a failure here (e.g. a text-only model refusing an image) does not stop
    that provider from being used for the synopsis itself."""
    global _ACTIVE
    from openai import APIError
    plist = providers()
    live = [p for p in plist if p.slot not in _DEAD] or plist[:1]
    failures = [f"{p.label}: skipped ({_DEAD[p.slot]})" for p in plist if p.slot in _DEAD]
    for i, p in enumerate(live):
        _ACTIVE = p
        try:
            try:
                res, usage = fn(*args, last=(i == len(live) - 1))
            except (LLMError, APIError) as e:
                if not _try_google_flip(p, e):
                    raise
                res, usage = fn(*args, last=(i == len(live) - 1))     # same provider, other header
            usage = dict(usage, provider=p.label)
            if failures:
                usage["fallbacks"] = failures
            return res, usage
        except (LLMError, APIError) as e:
            msg = friendly(e)
            failures.append(f"{p.label}: {msg[:160]}")
            if mark_dead and any(w in str(e).lower() or w in msg.lower() for w in LASTING):
                _DEAD[p.slot] = msg[:80]
            if i == len(live) - 1:
                if len(failures) > 1:
                    raise LLMError("All AI providers failed:\n- " + "\n- ".join(failures)) from e
                raise LLMError(msg) from e
        finally:
            _ACTIVE = None
    raise LLMError("No AI provider configured - set LLM_API_KEY in .env.")


def reset_fallback_state() -> None:
    """Forget providers marked as failed (call at the start of each tender)."""
    _DEAD.clear()


def friendly(e: Exception) -> str:
    """Translate provider errors into something a non-developer can act on."""
    from .env import key_report
    t = str(e)
    low = t.lower()
    if isinstance(e, LLMError):
        return t
    if "api key" in low or "api_key" in low or "401" in t or "unauthenticated" in low:
        slot = _ACTIVE.slot if _ACTIVE else "LLM"
        if slot != "LLM":
            return f"The AI provider rejected the key in {slot}_API_KEY. Copy it again into .env."
        return ("The AI provider rejected the API key. " + key_report()[1] +
                " Fix: copy the key again (use the copy button), paste it after LLM_API_KEY= in "
                ".env with no spaces or quotes, save the file, and click Generate again.")
    if "429" in t or "quota" in low or "rate" in low and "limit" in low:
        return "Free-tier limit reached. Wait a minute (or until tomorrow for daily limits) and retry."
    if "404" in t or "not found" in low and "model" in low:
        slot = _ACTIVE.slot if _ACTIVE else "LLM"
        return (f"Model '{model_name()}' was not found at this provider. Put a current model name "
                f"after {slot}_MODEL= in .env (copy it from the provider's model list).")
    if "413" in t or "too large" in low or ("context" in low and ("length" in low or "too long" in low)):
        return "The tender is too long for this model. Use a model with a larger context window."
    return t


def check_connection() -> str:
    """Tiny test call to every configured provider. Raises LLMError only if none works."""
    global _ACTIVE
    lines, ok = [], 0
    for p in providers():
        _ACTIVE = p
        try:
            ask = lambda: _client().chat.completions.create(  # noqa: E731
                model=p.model, messages=[{"role": "user", "content": "Reply with the word OK."}])
            try:
                r = ask()
            except Exception as e:
                if not _try_google_flip(p, e):
                    raise
                r = ask()
            lines.append(f"✅ {p.slot} {p.label} – reply: {r.choices[0].message.content!r}")
            ok += 1
        except Exception as e:
            lines.append(f"❌ {p.slot} {p.label} – {friendly(e)}")
        finally:
            _ACTIVE = None
    if not ok:
        raise LLMError("\n".join(lines) or "No AI provider configured.")
    return "\n".join(lines)


def model_name() -> str:
    return _ACTIVE.model if _ACTIVE else os.getenv("LLM_MODEL", DEFAULT_MODEL)


def _settings() -> dict:
    return {"mode": os.getenv("LLM_JSON_MODE", "auto").lower(),        # auto | strict | json | plain
            "retries": int(os.getenv("LLM_RETRIES", "4")),
            "repairs": int(os.getenv("LLM_REPAIR_ATTEMPTS", "3")),
            "max_out": int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "0")) or None}


_NO_STRICT: set[str] = set()       # models seen failing strict JSON-schema mode this session
_NO_JSON_MODE: set[str] = set()    # models that reject response_format={"type": "json_object"}


_LAST = {"flag": True}     # is the active provider the last one left? (fewer waits if not)


def _create(client, method: str = "create", **kw):
    """Call the API, waiting and retrying on rate limits, timeouts and server errors.
    With a fallback provider waiting, only one short retry is made before moving on."""
    import time

    from openai import (APIConnectionError, APITimeoutError, InternalServerError,
                        RateLimitError)
    retries = _settings()["retries"] if _LAST["flag"] else min(1, _settings()["retries"])
    for attempt in range(retries + 1):
        try:
            return getattr(client.chat.completions, method)(**kw)
        except (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError) as e:
            daily = "per day" in str(e).lower() or "perday" in str(e).lower()
            if attempt == retries or daily:
                raise LLMError(friendly(e)) from e
            wait = _retry_after(e) or min(60, 8 * 2 ** attempt)
            time.sleep(wait)


def _retry_after(e) -> float | None:
    import re
    resp = getattr(e, "response", None)
    try:
        h = resp.headers.get("retry-after") if resp is not None else None
        if h:
            return min(90.0, float(h))
    except (TypeError, ValueError):
        pass
    m = re.search(r"retry in ([0-9.]+)s", str(e).lower())
    return min(90.0, float(m[1]) + 1) if m else None


def _is_auth(e: Exception) -> bool:
    low = str(e).lower()
    return "api key" in low or "api_key" in low or "401" in str(e) or "unauthenticated" in low


VISION_PROMPT = """\
You transcribe one page of a tender document from an image (often handwriting, a phone photo
or a poor scan). Copy what is written - do not summarise, explain or correct it.
- Keep the reading order. Put each table row on one line, cells separated by " | ".
- Copy every number, date, amount, unit and reference exactly as written (e.g. 18x3, 54 sq m).
- Anything you cannot read with confidence: write [illegible]. Never guess or complete a word
  or a number. Ignore signatures, stamps and logos unless they contain text or dates.
Return plain text only."""


def vision_transcribe(image: bytes, mime: str = "image/jpeg") -> tuple[str, dict]:
    """Read a hard-to-OCR page image with a vision-capable model (tried in provider order;
    text-only models are skipped without affecting the synopsis call)."""
    return _with_fallback(_vision_one, image, mime, mark_dead=False)


def _vision_one(image: bytes, mime: str, last: bool = True) -> tuple[str, dict]:
    import base64
    _LAST["flag"] = last
    client, model = _client(), model_name()
    url = f"data:{mime};base64," + base64.b64encode(image).decode()
    resp = _create(client, model=model, max_tokens=3000, messages=[
        {"role": "system", "content": VISION_PROMPT},
        {"role": "user", "content": [{"type": "text", "text": "Transcribe this page."},
                                     {"type": "image_url", "image_url": {"url": url}}]}])
    text = (resp.choices[0].message.content or "").strip()
    if len(text) < 5:
        raise LLMError("The model returned no text for the image.")
    return text, _usage(resp)


def structured_call(system: str, user: str, schema: type[T]) -> tuple[T, dict]:
    """Return (parsed object, usage dict), trying fallback providers if needed."""
    return _with_fallback(_structured_one, system, user, schema)


def json_call(system: str, user: str) -> tuple[dict, dict]:
    """Free-form JSON answer (second check, long-tender notes), with fallback providers."""
    return _with_fallback(_json_one, system, user)


def _structured_one(system: str, user: str, schema: type[T], last: bool = True) -> tuple[T, dict]:
    """One provider. Return (parsed object, usage dict).

    Order of attempts (each step only if the previous one fails):
      1. strict JSON-schema output (OpenAI, newer Gemini)        - skipped if LLM_JSON_MODE=json/plain
      2. JSON mode + schema in the prompt, lenient parsing
      3. repair rounds: the validation errors are sent back and the model fixes its JSON
    Plain text mode is used automatically for providers that reject JSON mode.
    """
    _LAST["flag"] = last
    client, model = _client(), model_name()
    cfg = _settings()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    extra = {"max_tokens": cfg["max_out"]} if cfg["max_out"] else {}
    fallback_reason = ""
    if cfg["mode"] in ("auto", "strict") and model not in _NO_STRICT:
        try:
            resp = _create(client, "parse", model=model, messages=messages,
                           response_format=schema, **extra)
            msg = resp.choices[0].message
            if msg.parsed is not None and not msg.refusal:
                return msg.parsed, _usage(resp)
            fallback_reason = f"refusal/empty: {msg.refusal}"
        except LLMError:
            raise
        except Exception as e:
            if _is_auth(e):
                raise LLMError(friendly(e)) from e
            if cfg["mode"] == "strict" or "413" in str(e) or "too large" in str(e).lower() \
                    or ("context" in str(e).lower() and "length" in str(e).lower()):
                raise LLMError(friendly(e)) from e
            fallback_reason = f"{type(e).__name__}: {str(e)[:300]}"
            _NO_STRICT.add(model)

    messages[0]["content"] += ("\n\nReturn ONLY one JSON object (no prose, no code fences) that "
                               "validates against this JSON schema:\n"
                               + json.dumps(schema.model_json_schema(), separators=(",", ":")))
    usage: dict = {}
    last_err = ""
    for attempt in range(cfg["repairs"] + 1):
        resp = _json_request(client, model, messages, extra)
        usage = _merge_usage(usage, _usage(resp))
        raw = resp.choices[0].message.content or ""
        truncated = getattr(resp.choices[0], "finish_reason", "") == "length"
        try:
            obj = schema.model_validate(loads_loose(raw))
            return obj, usage | ({"fallback": fallback_reason} if fallback_reason else {}) \
                | ({"repairs": attempt} if attempt else {})
        except (ValueError, ValidationError) as e:
            last_err = _short_errors(e)
            fix = ("Your JSON was cut off because it was too long. Return the COMPLETE object "
                   "again but shorter: fewer bullets, shorter sentences."
                   if truncated else
                   f"Your JSON did not validate:\n{last_err}\nReturn the complete corrected JSON object.")
            messages = messages[:2] + [{"role": "assistant", "content": raw[:20000]},
                                       {"role": "user", "content": fix}]
    raise LLMError("The AI did not return a valid synopsis after "
                   f"{cfg['repairs'] + 1} attempts. Last problem: {last_err[:400]}. "
                   "Try again, or switch to a stronger model in .env (LLM_MODEL).")


def _json_request(client, model, messages, extra):
    if model not in _NO_JSON_MODE:
        try:
            return _create(client, model=model, messages=messages,
                           response_format={"type": "json_object"}, **extra)
        except LLMError:
            raise
        except Exception as e:
            if _is_auth(e):
                raise LLMError(friendly(e)) from e
            if "response_format" not in str(e) and "json" not in str(e).lower():
                raise LLMError(friendly(e)) from e
            _NO_JSON_MODE.add(model)
    try:
        return _create(client, model=model, messages=messages, **extra)
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(friendly(e)) from e


def _short_errors(e: Exception, limit: int = 12) -> str:
    if isinstance(e, ValidationError):
        lines = [f"- {'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors()[:limit]]
        more = len(e.errors()) - limit
        return "\n".join(lines) + (f"\n- ...and {more} more" if more > 0 else "")
    return f"- not valid JSON: {e}"


def loads_loose(raw: str):
    """json.loads that tolerates code fences, text around the JSON, trailing commas,
    smart quotes and // comments - common in free-model output."""
    import re
    s = _strip_fences(raw)
    try:
        return json.loads(s)
    except ValueError:
        pass
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"(?m)^\s*//.*$", "", s)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return json.loads(s)


def _strip_fences(raw: str) -> str:
    """Some models wrap JSON in ```json fences or add text around it."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        raw = raw.rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start:end + 1] if start != -1 and end > start else raw


def _json_one(system: str, user: str, last: bool = True) -> tuple[dict, dict]:
    _LAST["flag"] = last
    client, model = _client(), model_name()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(2):
        resp = _json_request(client, model, messages, {})
        raw = resp.choices[0].message.content or "{}"
        try:
            return loads_loose(raw), _usage(resp)
        except ValueError:
            messages = messages[:2] + [{"role": "assistant", "content": raw[:20000]},
                                       {"role": "user", "content": "That was not valid JSON. "
                                        "Return only the JSON object."}]
    return {}, _usage(resp)


def _merge_usage(a: dict, b: dict) -> dict:
    if not a:
        return b
    out = dict(a)
    for k in ("input_tokens", "output_tokens"):
        out[k] = (a.get(k) or 0) + (b.get(k) or 0)
    return out


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    return {"model": getattr(resp, "model", model_name()),
            "input_tokens": getattr(u, "prompt_tokens", None),
            "output_tokens": getattr(u, "completion_tokens", None)}
