"""
llm_payloads.py — Compact, quality-preserving payloads for LLM prompts.

Scoring and strike selection continue to use full structured data in-process.
These helpers only shrink what is *sent to the model*, so free-tier / paid
token burn drops without changing EXECUTE/PASS math.
"""

from __future__ import annotations

import json
from typing import Any


def compact_options_for_llm(
    options_json: str | dict,
    *,
    atm_band: int = 6,
    max_per_side: int = 8,
) -> str:
    """
    Build a compact options JSON for Risk/Quant prompts.

    Keeps: ticker, spot, swing_targets, and near-ATM contracts only
    (closest strikes to spot per side, capped). Full chains remain available
    to scoring_engine / strike_selector via the original dict.
    """
    try:
        if isinstance(options_json, dict):
            data = options_json
        else:
            data = json.loads(options_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return str(options_json or "")[:4000]

    if not isinstance(data, dict):
        return "{}"
    if data.get("error"):
        return json.dumps({"error": data.get("error")}, separators=(",", ":"))

    spot = data.get("current_price")
    out: dict[str, Any] = {
        "ticker": data.get("ticker"),
        "current_price": spot,
        "swing_targets": data.get("swing_targets"),
        "chains": {},
        "_compact": True,
        "_note": f"ATM± band, max {max_per_side}/side (full chain used offline for scoring)",
    }

    chains = data.get("chains") or {}
    if not isinstance(chains, dict):
        return json.dumps(out, separators=(",", ":"))

    try:
        spot_f = float(spot) if spot is not None and spot != "N/A" else None
    except (TypeError, ValueError):
        spot_f = None

    for exp, sides in chains.items():
        if not isinstance(sides, dict):
            continue
        compact_sides = {}
        for side in ("calls", "puts"):
            rows = sides.get(side) or []
            if not isinstance(rows, list):
                compact_sides[side] = []
                continue
            usable = [r for r in rows if isinstance(r, dict) and r.get("strike")]
            if spot_f is not None and usable:
                usable = sorted(usable, key=lambda r: abs(float(r["strike"]) - spot_f))
                # Prefer a band around ATM, then hard-cap count
                banded = [
                    r for r in usable
                    if abs(float(r["strike"]) - spot_f) <= max(atm_band, 1) * max(spot_f * 0.02, 1.0)
                ]
                if not banded:
                    banded = usable
                usable = banded[:max_per_side]
            else:
                usable = usable[:max_per_side]
            # Drop bulky unused keys if present; keep trading fields
            slim = []
            for r in usable:
                slim.append({
                    k: r.get(k)
                    for k in (
                        "strike", "bid", "ask", "lastPrice", "volume",
                        "openInterest", "impliedVolatility",
                    )
                    if k in r
                })
            compact_sides[side] = slim
        out["chains"][exp] = compact_sides

    # Compact JSON (no indent) — large win vs indent=2 full dumps
    return json.dumps(out, separators=(",", ":"))


def clip_text_for_llm(
    text: str,
    *,
    max_chars: int = 6000,
    max_lines: int = 60,
) -> str:
    """
    Cap free-form text for LLM prompts (most-recent lines first).

    Scoring should keep the full string; only the model sees the clip.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = raw.splitlines()
    if len(lines) > max_lines:
        # Headlines are usually newest-first from get_historical_context
        lines = lines[:max_lines]
        lines.append(f"... ({len(raw.splitlines()) - max_lines} older lines omitted for LLM)")
    clipped = "\n".join(lines)
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 40].rstrip() + "\n... (truncated for LLM)"
    return clipped


def quant_local_note(math_json: str, ticker: str) -> str:
    """Deterministic one-liner when Quant LLM is skipped (PASS path)."""
    try:
        data = json.loads(math_json) if isinstance(math_json, str) else (math_json or {})
        targets = (data or {}).get("swing_targets") or {}
        if targets:
            return (
                f"Quant (local): ATM ref strike {targets.get('reference_strike')} "
                f"exp {targets.get('reference_expiration')} entry "
                f"${targets.get('entry_premium')} "
                f"(SL ${targets.get('stop_loss')} / TP ${targets.get('take_profit')}). "
                f"Strike selector remains authoritative for {ticker}."
            )
    except Exception:
        pass
    return (
        f"Quant (local): no ATM swing targets for {ticker}; "
        f"deterministic strike selector remains authoritative."
    )


def risk_local_note(math_json: str, ticker: str) -> str:
    """Short risk color without an LLM call (PASS path)."""
    try:
        data = json.loads(math_json) if isinstance(math_json, str) else (math_json or {})
        targets = (data or {}).get("swing_targets") or {}
        entry = targets.get("entry_premium", "N/A")
        sl = targets.get("stop_loss", "N/A")
        tp = targets.get("take_profit", "N/A")
    except Exception:
        entry, sl, tp = "N/A", "N/A", "N/A"
    return (
        f"=== RISK REPORT FOR {ticker} (LOCAL, PASS PATH) ===\n"
        f"Liquidity/IV detail deferred (PASS — scoring already used full chain offline). "
        f"Reference targets entry ${entry} / SL ${sl} / TP ${tp}. Risk Rating: n/a (no EXECUTE)."
    )


def structural_cos_brief(
    ticker: str,
    technical_context: str,
    news_report: str,
    risk_report: str,
    quant_report: str,
) -> str:
    """Zero-LLM CoS brief — same information, concatenated (matches soft-fallback shape)."""
    return (
        f"=== CORPORATE BRIEF {ticker} (STRUCTURAL) ===\n"
        f"**Technical**: {(technical_context or '')[:900]}\n"
        f"**Sentiment**: {(news_report or '')[:900]}\n"
        f"**Risk**: {(risk_report or '')[:900]}\n"
        f"**Quant**: {(quant_report or '')[:500]}\n"
    )
