"""Go / no-go check before deploying or going live.

Runs every check that can be made without sending a message, and prints a
verdict per line. Exit code 0 means safe to deploy.

    .venv\\Scripts\\python.exe scripts\\preflight.py            # dev sanity check
    .venv\\Scripts\\python.exe scripts\\preflight.py --production  # full go-live gate

Written because the failures that cost the most time on this project were all
silent: a wrong app secret that looked like "the bot never replied", a token
that expired mid-afternoon, knowledge files that loaded as zero snippets, and a
Sheets sink that was switched on but unusable. Every one of those is a line
below.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.knowledge.consistency import check_business_hours  # noqa: E402
from app.services.knowledge.loader import load_knowledge_base  # noqa: E402

PASS, WARN, FAIL = "  [PASS]", "  [WARN]", "  [FAIL]"

_failures: list[str] = []
_warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"{PASS}  {msg}")


def warn(msg: str, fix: str = "") -> None:
    print(f"{WARN}  {msg}" + (f"\n           -> {fix}" if fix else ""))
    _warnings.append(msg)


def fail(msg: str, fix: str = "") -> None:
    print(f"{FAIL}  {msg}" + (f"\n           -> {fix}" if fix else ""))
    _failures.append(msg)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# --------------------------------------------------------------------------- #
def check_knowledge() -> None:
    section("Knowledge base")
    try:
        kb = load_knowledge_base(get_settings().knowledge_dir)
    except Exception as exc:
        fail(f"knowledge base will not load: {exc}",
             "run: .venv\\Scripts\\python.exe scripts\\declutter.py")
        return

    by_source: dict[str, int] = {}
    for s in kb.snippets:
        by_source[s.source] = by_source.get(s.source, 0) + 1

    ok(f"{len(kb.courses)} courses, {len(kb.snippets)} snippets {by_source}")

    # Each of these silently became zero at some point in this project's life.
    for source, label in [("course", "course"), ("faq", "FAQ"),
                          ("policy", "policy"), ("company", "company")]:
        if not by_source.get(source):
            fail(f"zero {label} snippets loaded",
                 f"the shape of the {label} JSON probably changed - check the loader")

    if not kb.featured_courses:
        fail("no courses would appear in the WhatsApp menu")

    for w in check_business_hours(kb, get_settings()):
        warn(f"business hours disagree: {w}",
             "the bot will state one thing and enforce another")


def check_llm() -> None:
    """Probe whichever provider is actually configured.

    Always checking Groq would have passed happily while Gemini answered every
    real message - a green preflight for a backend nobody is using.
    """
    s = get_settings()
    if s.llm_provider == "groq":
        _check_groq(s)
    elif s.llm_provider == "gemini":
        _check_gemini(s)
    else:
        _check_openai(s)


def _check_openai(s: Any) -> None:
    section("OpenAI")
    key = s.openai_api_key.get_secret_value()
    if not key:
        fail("OPENAI_API_KEY is empty", "the bot cannot answer questions")
        return

    payload: dict[str, Any] = {
        "model": s.openai_model,
        "max_completion_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    if s.openai_temperature is not None:
        payload["temperature"] = s.openai_temperature

    try:
        r = httpx.post(
            f"{s.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
    except Exception as exc:
        fail(f"cannot reach OpenAI: {exc}")
        return

    if r.status_code == 200:
        ok(f"reachable, model {s.openai_model} responded")
        return

    # Each of these is a different fix, and the raw status alone sends you to
    # the wrong one - a dead model reads like a dead key from the outside.
    detail = ""
    code = ""
    try:
        error = r.json().get("error") or {}
        detail = str(error.get("message", ""))[:160]
        code = str(error.get("code") or error.get("type") or "")
    except Exception:
        detail = r.text[:160]

    if r.status_code == 401:
        fail("OpenAI rejected the API key", "check OPENAI_API_KEY was copied whole")
    elif code == "insufficient_quota":
        fail(
            "the OpenAI credit balance is empty",
            "top it up at platform.openai.com/settings/organization/billing",
        )
    elif r.status_code == 429:
        warn("rate limited right now", "the key works; this clears on its own")
    elif r.status_code == 404 or code == "model_not_found":
        fail(
            f"model {s.openai_model} is not available to this key",
            "set OPENAI_MODEL to one your account can use",
        )
    elif "temperature" in detail.lower():
        fail(
            f"model {s.openai_model} rejects OPENAI_TEMPERATURE={s.openai_temperature}",
            "leave OPENAI_TEMPERATURE empty for the gpt-5 family",
        )
    else:
        fail(f"OpenAI returned {r.status_code}: {detail}")


def _check_groq(s: Any) -> None:
    section("Groq")
    if not s.groq_api_key.get_secret_value():
        fail("GROQ_API_KEY is empty", "the bot cannot answer questions")
        return
    try:
        r = httpx.post(
            f"{s.groq_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {s.groq_api_key.get_secret_value()}"},
            json={"model": s.groq_model, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=20,
        )
    except Exception as exc:
        fail(f"cannot reach Groq: {exc}")
        return
    if r.status_code == 200:
        ok(f"reachable, model {s.groq_model} responded")
    elif r.status_code == 401:
        fail("Groq rejected the API key")
    else:
        fail(f"Groq returned {r.status_code}: {r.text[:120]}")


def _check_gemini(s: Any) -> None:
    section("Gemini")
    key = s.gemini_api_key.get_secret_value()
    if not key:
        fail("GEMINI_API_KEY is empty", "the bot cannot answer questions")
        return
    try:
        r = httpx.post(
            f"{s.gemini_base_url.rstrip('/')}/models/{s.gemini_model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 1, "thinkingConfig": {"thinkingBudget": 0}},
            },
            timeout=20,
        )
    except Exception as exc:
        fail(f"cannot reach Gemini: {exc}")
        return

    if r.status_code == 200:
        ok(f"reachable, model {s.gemini_model} responded")
        if s.gemini_thinking_budget != 0:
            warn(
                f"GEMINI_THINKING_BUDGET={s.gemini_thinking_budget}",
                "thinking tokens bill as output - roughly 4x the quota per reply",
            )
    elif r.status_code in (401, 403):
        fail("Gemini rejected the API key")
    elif r.status_code == 404:
        fail(f"model {s.gemini_model} not found for this key")
    elif r.status_code == 429:
        fail("Gemini quota exhausted", "answers will fall back to the canned reply")
    else:
        fail(f"Gemini returned {r.status_code}: {r.text[:140]}")


def check_whatsapp() -> None:
    section("WhatsApp")
    s = get_settings()
    if not s.whatsapp_enabled:
        warn("WHATSAPP_ENABLED is false - replies go to the log, not to users")
        return

    token = s.whatsapp_access_token.get_secret_value()
    secret = s.whatsapp_app_secret.get_secret_value()
    pid = s.whatsapp_phone_number_id
    if not token or not pid:
        fail("WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID is empty")
        return

    base = f"{s.whatsapp_base_url}/{s.whatsapp_api_version}"
    try:
        fields = (
            "display_phone_number,verified_name,platform_type,status,quality_rating"
        )
        r = httpx.get(
            f"{base}/{pid}",
            params={"fields": fields},
            headers={"Authorization": f"Bearer {token}"}, timeout=20,
        )
    except Exception as exc:
        fail(f"cannot reach Meta: {exc}")
        return

    if r.status_code != 200:
        fail(f"cannot read the phone number: {r.text[:160]}",
             "token expired, or it is not authorised for this number")
        return

    d = r.json()
    ok(f"{d.get('display_phone_number')} ({d.get('verified_name')}) "
       f"{d.get('platform_type')} / {d.get('status')} / {d.get('quality_rating')}")

    if d.get("platform_type") != "CLOUD_API":
        fail(f"platform_type is {d.get('platform_type')}, not CLOUD_API",
             "this number cannot send or receive through this codebase")
    if d.get("status") != "CONNECTED":
        warn(f"status is {d.get('status')}, not CONNECTED")

    # A wrong app secret is the single most confusing failure in this system:
    # every webhook returns 403 and it looks exactly like "the bot never replied".
    if not secret:
        (fail if s.is_production else warn)(
            "WHATSAPP_APP_SECRET is empty - webhook signatures are unverified")
    else:
        proof = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
        r2 = httpx.get(f"{base}/{pid}", params={"fields": "id", "appsecret_proof": proof},
                       headers={"Authorization": f"Bearer {token}"}, timeout=20)
        if r2.status_code == 200:
            ok("app secret matches the access token's app")
        else:
            fail("APP SECRET DOES NOT MATCH THE TOKEN'S APP",
                 "every webhook will fail signature checks and return 403")

    if not s.whatsapp_verify_token.get_secret_value():
        fail("WHATSAPP_VERIFY_TOKEN is empty - webhook setup will not verify")


def check_security(production: bool) -> None:
    section("Security")
    s = get_settings()

    if s.api_key.get_secret_value():
        if "change" in s.api_key.get_secret_value().lower():
            fail("API_KEY is still the placeholder", "generate a real one")
        else:
            ok("API_KEY is set - lead endpoints are protected")
    elif production:
        fail("API_KEY is empty - lead endpoints refuse to serve in production")
    else:
        warn("API_KEY is empty - lead endpoints are open (local only)")

    ok(f"rate limiting {'on' if s.rate_limit_enabled else 'OFF'}: "
       f"{s.rate_limit_per_sender}/sender, {s.rate_limit_webhook_per_ip}/ip "
       f"per {s.rate_limit_window_seconds:.0f}s") if s.rate_limit_enabled else fail(
        "rate limiting is disabled", "every message costs an LLM call")

    guard = s.whatsapp_allowlist_enabled
    if guard and s.whatsapp_allowed_numbers:
        listed = len(s.whatsapp_allowed_numbers.split(","))
        ok(f"allowlist ON - only {listed} number(s) can reach the bot")
    elif guard:
        warn("allowlist ON but EMPTY - the bot will reply to nobody")
    else:
        warn("allowlist OFF - every customer reaches the bot",
             "correct only when you intend to be live")

    if production:
        if not s.is_production:
            fail(f"ENVIRONMENT is {s.environment}, not production",
                 "/docs and /simulate stay exposed; /simulate impersonates any number")
        else:
            ok("ENVIRONMENT=production - /docs and /simulate disabled")
        if s.debug:
            fail("DEBUG is true")
        if not s.log_json:
            warn("LOG_JSON is false - logs will not be machine-readable")


def check_sheets() -> None:
    section("Google Sheets")
    s = get_settings()
    if not s.google_sheets_enabled:
        warn("Sheets sync disabled - leads live only in PostgreSQL")
        return
    if s.google_credentials_info() is None:
        fail("Sheets enabled but no service-account credentials found",
             f"expected at {s.google_service_account_file}")
        return
    ok("service-account key loaded")
    print("           (run scripts/check_sheets.py to verify sheet access)")


def main() -> int:
    production = "--production" in sys.argv
    print("=" * 68)
    print("  PREFLIGHT" + ("  [PRODUCTION GATE]" if production else "  [development]"))
    print("=" * 68)

    check_knowledge()
    check_llm()
    check_whatsapp()
    check_security(production)
    check_sheets()

    print("\n" + "=" * 68)
    if _failures:
        print(f"  {len(_failures)} BLOCKER(S), {len(_warnings)} warning(s) - DO NOT DEPLOY")
        for f in _failures:
            print(f"    - {f}")
        return 1
    print(f"  All checks passed, {len(_warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
