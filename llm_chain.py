"""
llm_chain.py — Cost-aware dual-provider text generation.

Routing policy (callers set ``primary``):
  * Pre-market CoS: primary="gemini" (free tier), DeepSeek backup.
  * Midday macro note: primary="deepseek", Gemini backup (Gemini reserved
    for the morning brief).
  * Trade scans (CEO/quant/CoS/managers/adversarial): primary="deepseek",
    Gemini optional backup on failure.

Design:
  1. Attempt the primary provider under a hard wall-clock budget.
  2. On any failure, log ``[LLM FAILOVER] primary→backup`` and retry.
  3. Each leg uses the same wall-clock budget (requests + ThreadPoolExecutor).
  4. If both fail, raise LLMChainError — callers isolate / soft-fallback.

Does not own trading logic. Never hangs indefinitely.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import requests

# Prefer config when available (env single source of truth).
try:
    import config as _config
    GEMINI_API_KEY = getattr(_config, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    DEEPSEEK_API_KEY = getattr(_config, "DEEPSEEK_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
except Exception:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# gemini-2.5-flash returns 404 for many free-tier / new keys ("no longer available
# to new users"). Prefer the stable free-tier alias; override via LLM_GEMINI_MODEL.
GEMINI_MODEL = os.environ.get("LLM_GEMINI_MODEL", "gemini-flash-latest")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("LLM_DEEPSEEK_MODEL", "deepseek-chat")

# Match master_bot.API_CALL_TIMEOUT_S default; callers may override per-call.
DEFAULT_TIMEOUT_S = 20
# google-genai HttpOptions.timeout is milliseconds.
GEMINI_HTTP_TIMEOUT_MS = int(os.environ.get("LLM_HTTP_TIMEOUT_MS", "20000"))


class LLMChainError(Exception):
    """Both providers failed (or no usable key)."""

    def __init__(self, message, *, step="llm", is_timeout=False, gemini_error=None, deepseek_error=None):
        super().__init__(message)
        self.message = message
        self.step = step or "llm"
        self.is_timeout = bool(is_timeout)
        self.gemini_error = gemini_error
        self.deepseek_error = deepseek_error


def _run_with_deadline(fn, *, timeout_s, step):
    """Hard wall-clock envelope so a hung SDK cannot freeze the trading thread."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError as exc:
            raise LLMChainError(
                f"Timed out after {timeout_s}s",
                step=step,
                is_timeout=True,
            ) from exc


def _resolve_keys():
    """Re-read env/config each call so Render/runtime secrets always apply."""
    try:
        import config as _config
        gemini = getattr(_config, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
        deepseek = getattr(_config, "DEEPSEEK_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    except Exception:
        gemini = os.environ.get("GEMINI_API_KEY", "")
        deepseek = os.environ.get("DEEPSEEK_API_KEY", "")
    return (gemini or "").strip(), (deepseek or "").strip()


def _gemini_generate_sdk(prompt, *, system=None, key=None):
    from google import genai

    key = (key or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(
        api_key=key,
        http_options={"timeout": GEMINI_HTTP_TIMEOUT_MS},
    )
    contents = prompt if not system else f"{system}\n\n{prompt}"
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
    )
    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise RuntimeError("Gemini returned empty text")
    return text


def _gemini_generate_rest(prompt, *, system=None, key=None, http_timeout_s=None):
    """
    Direct generateContent HTTPS — no google-genai SDK required.
    Used when the SDK is missing, and as a same-provider retry before failover.
    """
    key = (key or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")

    contents = prompt if not system else f"{system}\n\n{prompt}"
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    timeout = http_timeout_s
    if timeout is None:
        timeout = max(1.0, GEMINI_HTTP_TIMEOUT_MS / 1000.0)
    resp = requests.post(
        url,
        params={"key": key},
        json={"contents": [{"parts": [{"text": contents}]}]},
        timeout=timeout,
    )
    if not resp.ok:
        snippet = (resp.text or "")[:300]
        raise RuntimeError(f"Gemini REST HTTP {resp.status_code}: {snippet}")

    payload = resp.json()
    try:
        parts = (
            payload.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        # Skip thoughtSignature-only parts; join text parts.
        texts = []
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                texts.append(part["text"])
        text = "".join(texts).strip()
    except Exception as exc:
        raise RuntimeError(f"Gemini REST parse failed: {exc}") from exc

    if not text:
        raise RuntimeError("Gemini returned empty text")
    return text


def _gemini_generate(prompt, *, system=None):
    """
    Gemini path: SDK first, REST same-model fallback if SDK is unavailable
    or errors. Failover to the other provider is handled by generate_text.
    """
    key, _ = _resolve_keys()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")

    sdk_err = None
    try:
        return _gemini_generate_sdk(prompt, system=system, key=key)
    except ImportError as exc:
        sdk_err = exc
        print(f"[LLM] Gemini SDK unavailable ({exc}); trying REST ({GEMINI_MODEL})...")
    except Exception as exc:
        sdk_err = exc
        print(
            f"[LLM] Gemini SDK failed ({exc}); trying REST ({GEMINI_MODEL}) "
            "before provider failover..."
        )

    try:
        return _gemini_generate_rest(
            prompt,
            system=system,
            key=key,
            http_timeout_s=max(1.0, GEMINI_HTTP_TIMEOUT_MS / 1000.0),
        )
    except Exception as rest_err:
        if sdk_err is not None:
            raise RuntimeError(
                f"Gemini SDK+REST failed: sdk={sdk_err}; rest={rest_err}"
            ) from rest_err
        raise


def _deepseek_generate(prompt, *, system=None, http_timeout_s=DEFAULT_TIMEOUT_S):
    _, key = _resolve_keys()
    if not key:
        # Module-level cache may still hold a key from import time.
        key = (DEEPSEEK_API_KEY or "").strip()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # Keep HTTP timeout slightly under the executor deadline when possible.
    http_timeout = max(1.0, float(http_timeout_s))
    resp = requests.post(
        DEEPSEEK_API_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "messages": messages,
            "temperature": 0.3,
            "stream": False,
        },
        timeout=http_timeout,
    )
    if not resp.ok:
        snippet = (resp.text or "")[:300]
        raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {snippet}")

    payload = resp.json()
    try:
        text = (
            payload.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
    except Exception as exc:
        raise RuntimeError(f"DeepSeek response parse failed: {exc}") from exc

    text = (text or "").strip()
    if not text:
        raise RuntimeError("DeepSeek returned empty text")
    return text


def _has_gemini_key():
    gemini, _ = _resolve_keys()
    return bool(gemini)


def _has_deepseek_key():
    _, deepseek = _resolve_keys()
    return bool(deepseek or (DEEPSEEK_API_KEY or "").strip())


def _try_gemini(prompt, *, system, timeout_s, step):
    """Run Gemini under deadline; raise LLMChainError or Exception on failure."""
    return _run_with_deadline(
        lambda: _gemini_generate(prompt, system=system),
        timeout_s=timeout_s,
        step=f"{step}:gemini",
    )


def _try_deepseek(prompt, *, system, timeout_s, step):
    """Run DeepSeek under deadline; raise LLMChainError or Exception on failure."""
    return _run_with_deadline(
        lambda: _deepseek_generate(
            prompt,
            system=system,
            http_timeout_s=max(1.0, float(timeout_s) - 0.5),
        ),
        timeout_s=timeout_s,
        step=f"{step}:deepseek",
    )


def generate_text(
    prompt,
    *,
    step="llm",
    system=None,
    timeout_s=DEFAULT_TIMEOUT_S,
    primary: str = "gemini",
):
    """
    Cost-aware dual-provider generation.

    Args:
        primary: ``"gemini"`` — pre-market CoS / free-tier first, DeepSeek backup.
                 ``"deepseek"`` — midday note and trade scans first, Gemini backup.

    Each leg is independently wall-clock bounded by ``timeout_s``.
    Total worst-case latency ≈ 2 * timeout_s.

    Logs always name the provider that succeeded:
      ``[LLM] provider=gemini ok (step) model=...``
      ``[LLM FAILOVER] gemini→deepseek`` / ``deepseek→gemini``

    Raises:
        LLMChainError: when both providers fail (or backup key missing after primary fail).
    """
    prompt = str(prompt or "")
    if not prompt.strip():
        raise LLMChainError("Empty prompt", step=step)

    primary = (primary or "gemini").strip().lower()
    if primary not in ("gemini", "deepseek"):
        print(f"[LLM] Unknown primary={primary!r}; defaulting to gemini")
        primary = "gemini"

    if primary == "gemini":
        first, second = "gemini", "deepseek"
        first_fn = _try_gemini
        second_fn = _try_deepseek
        first_model = GEMINI_MODEL
        second_model = DEEPSEEK_MODEL
        first_key_ok = _has_gemini_key
        second_key_ok = _has_deepseek_key
    else:
        first, second = "deepseek", "gemini"
        first_fn = _try_deepseek
        second_fn = _try_gemini
        first_model = DEEPSEEK_MODEL
        second_model = GEMINI_MODEL
        first_key_ok = _has_deepseek_key
        second_key_ok = _has_gemini_key

    first_err = None
    try:
        text = first_fn(prompt, system=system, timeout_s=timeout_s, step=step)
        print(f"[LLM] provider={first} ok ({step}) model={first_model}")
        return text
    except LLMChainError as exc:
        first_err = exc
        print(
            f"[LLM FAILOVER] {first}→{second} ({step}): {exc.message}"
        )
    except Exception as exc:
        first_err = exc
        print(
            f"[LLM FAILOVER] {first}→{second} ({step}): {exc}"
        )

    if not second_key_ok():
        print(
            f"[LLM FAILOVER] {second} key missing — cannot failover for {step}"
        )
        missing = RuntimeError(f"{second} key missing")
        raise LLMChainError(
            f"{first} failed and {second} key missing: {first_err}",
            step=step,
            is_timeout=getattr(first_err, "is_timeout", False),
            gemini_error=first_err if first == "gemini" else missing,
            deepseek_error=first_err if first == "deepseek" else missing,
        ) from (first_err if isinstance(first_err, BaseException) else None)

    try:
        text = second_fn(prompt, system=system, timeout_s=timeout_s, step=step)
        print(
            f"[LLM FAILOVER] {first}→{second} succeeded ({step}) "
            f"model={second_model}"
        )
        print(f"[LLM] provider={second} ok ({step}) model={second_model} (failover)")
        return text
    except LLMChainError as second_err:
        print(
            f"[LLM FAILOVER] Both {first} and {second} failed for {step}: "
            f"{first}={first_err}; {second}={second_err.message}"
        )
        gemini_e = first_err if first == "gemini" else second_err
        deepseek_e = first_err if first == "deepseek" else second_err
        raise LLMChainError(
            f"LLM chain exhausted: {first}={first_err}; {second}={second_err.message}",
            step=step,
            is_timeout=bool(
                getattr(first_err, "is_timeout", False) or second_err.is_timeout
            ),
            gemini_error=gemini_e,
            deepseek_error=deepseek_e,
        ) from second_err
    except Exception as second_err:
        print(
            f"[LLM FAILOVER] Both {first} and {second} failed for {step}: "
            f"{first}={first_err}; {second}={second_err}"
        )
        gemini_e = first_err if first == "gemini" else second_err
        deepseek_e = first_err if first == "deepseek" else second_err
        raise LLMChainError(
            f"LLM chain exhausted: {first}={first_err}; {second}={second_err}",
            step=step,
            is_timeout=getattr(first_err, "is_timeout", False),
            gemini_error=gemini_e,
            deepseek_error=deepseek_e,
        ) from second_err


# Worst-case wall clock when master_bot wraps a call that itself runs the chain.
CHAIN_WALL_CLOCK_S = DEFAULT_TIMEOUT_S * 2

# First dual-provider fail per process, keyed by step (pre_market_cos /
# midday_macro_meeting). Session == process lifetime on Render.
_dual_fail_alerted: set[str] = set()
_TIMEOUT_RE = re.compile(r"timed out after\s+(\d+)", re.I)
_HTTP_RE = re.compile(r"HTTP\s+(\d{3})", re.I)
_HTTP_HINT = {
    "401": "unauthorized",
    "403": "forbidden",
    "404": "not found",
    "429": "quota",
    "500": "server",
    "502": "bad gateway",
    "503": "unavailable",
}


def reset_dual_fail_alerts_for_tests() -> None:
    _dual_fail_alerted.clear()


def format_provider_err(err) -> str:
    """Compact gemini=/deepseek= fragment for the Discord CRITICAL line."""
    if err is None:
        return "n/a"
    if isinstance(err, LLMChainError):
        if err.is_timeout:
            m = _TIMEOUT_RE.search(err.message or "")
            return f"timeout {m.group(1)}s" if m else "timeout"
        text = err.message or str(err)
    else:
        text = str(err)
    low = text.lower()
    if "key missing" in low or "not set" in low:
        return "key missing"
    m = _HTTP_RE.search(text)
    if m:
        code = m.group(1)
        hint = _HTTP_HINT.get(code, "")
        return f"{code} {hint}".strip()
    m = _TIMEOUT_RE.search(text)
    if m:
        return f"timeout {m.group(1)}s"
    compact = " ".join(text.split())
    if len(compact) > 80:
        compact = compact[:77] + "..."
    return compact or "error"


def alert_llm_dual_fail(step: str, exc: BaseException | None) -> None:
    """
    First dual-provider failure for this step in the process → Discord CRITICAL.

    Same shape as mark-fail / scan-discord-fail. Trading path is unaffected;
    the meeting already swapped in a deterministic fallback.
    """
    key = (step or "llm").strip() or "llm"
    if key in _dual_fail_alerted:
        print(f"[LLM] dual-fail CRITICAL already sent this session for {key}")
        return
    _dual_fail_alerted.add(key)

    gemini = deepseek = "n/a"
    if isinstance(exc, LLMChainError):
        gemini = format_provider_err(exc.gemini_error)
        deepseek = format_provider_err(exc.deepseek_error)
        if gemini == "n/a" and deepseek == "n/a":
            gemini = format_provider_err(exc)
    elif exc is not None:
        gemini = format_provider_err(exc)

    msg = (
        f"🚨 **CRITICAL: LLM DUAL FAIL ({key})** "
        f"gemini={gemini}, deepseek={deepseek}\n"
        "Meeting used the deterministic fallback. Trading path is unaffected."
    )
    print(f"[LLM] {msg.replace(chr(10), ' | ')}")
    try:
        import broadcaster
        delivered = broadcaster.send_discord_alert(msg)
        print(f"[LLM] dual-fail CRITICAL delivered={delivered} step={key}")
    except Exception as send_err:
        print(f"[LLM] dual-fail CRITICAL send failed: {send_err}")
