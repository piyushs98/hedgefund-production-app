"""
midday_delta.py — Intraday book scans + isolated midday macro meeting.

Architecture:
  * Pre-market (once/day, ~09:15 ET): Gemini CoS in pre_market_meeting.py
  * Every ~30 min (RTH): deterministic score + SINGLE Discord table payload
    (DeepSeek only for short KEY TELEMETRY bullets; no macro headers)
  * Midday macro meeting: DeepSeek once/day STRICTLY 11:00 AM CDT window
    (Gemini backup; Gemini reserved for the morning brief) — never mixed
    into 30-min scan messages

Scoring / strike selection remain deterministic. Stage 3 adds SignalGate
between score/adversarial and strike/execution (rank-before-admit).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, time, timezone
from typing import Any

import config
import broadcaster
import llm_chain
import scoring_engine
import signal_gate
import strike_selector
import telemetry
import virtual_broker
from circuit_breaker import CircuitBreaker
from data_engineer import fetch_options_data
from news_memory import get_innovation_context
from ticker_desk import fetch_pivot_data

try:
    import pytz
except ImportError:  # pragma: no cover
    pytz = None

BASELINE_PATH = os.environ.get(
    "SESSION_BASELINE_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "session_baseline.json")),
)

SCORE_DELTA_MIN = float(os.environ.get("DELTA_SCORE_MIN", "5"))
PCT_CHANGE_DELTA_MIN = float(os.environ.get("DELTA_PCT_MIN", "0.75"))
LLM_TIMEOUT_S = int(os.environ.get("LLM_CALL_TIMEOUT_S", "20"))


def _midday_llm_timeout_s() -> int:
    """Midday meeting wall clock. Default 60; override MIDDAY_LLM_TIMEOUT_S."""
    try:
        return max(1, int(getattr(config, "MIDDAY_LLM_TIMEOUT_S", 60)))
    except (TypeError, ValueError):
        return 60

# 11:00 AM CDT window (America/Chicago). 30-min cadence may land mid-window.
MIDDAY_MACRO_START = time(11, 0)
MIDDAY_MACRO_END = time(11, 44)

BANNED_FILLER = (
    "ceo",
    "ceo says",
    "no deltas to manage",
    "no hesitation",
    "we move",
    "let's work",
    "lets work",
    "disciplined execution",
    "no fluff",
    "load the position",
)

DEEPSEEK_TRADE_SYSTEM = """You write KEY TICKER TELEMETRY bullets for a real-time options radar Discord feed.

OUTPUT RULES (strict):
1. One bullet per ticker. Format exactly:
- **TICKER**: Spot $X vs Pivot $Y. Score: Z/100. <brief technical rationale>
2. Data-dense only: Spot, Pivot, Score, and Contract/Entry/SL/TP if provided.
3. ALWAYS copy piv= mom= dft= d30= vol= T= S= liq= dATR= atm_n= med_spr= usable=
   (score_subs) verbatim after Score on EVERY row (EXECUTE and PASS).
   Prefer numbers over prose; drop commentary first.
4. When the payload includes "ext" (e.g. ext=$0.51 (2.8%)), copy it verbatim after
   contract fields on EXECUTE rows. Do not invent extrinsic.
   When "qty" is present, copy qty=N after the contract.
5. When "delta" is present, append δ=0.XX. Never invent delta.
6. When "block" is present (no_momentum_data / dead_zone), state it briefly —
   that is a data/structure PASS, not weak technicals. Chain atm_n/med_spr/usable
   are forensics, not a score haircut.
7. BANNED phrases (never use any of these or close variants):
   CEO, CEO says, No deltas to manage, No hesitation, We move, Let's work,
   Disciplined execution, No fluff, Load the position.
8. No morning brief, no ES/NQ futures essays, no MIDDAY MEETING headers, no table.
9. Output ONLY the bullet list. No preamble or closing.
"""


def _chicago_now():
    if pytz is None:
        return datetime.now()
    return datetime.now(pytz.timezone("America/Chicago"))


def _edt_now():
    if pytz is None:
        return datetime.now()
    return datetime.now(pytz.timezone("America/New_York"))


def cdt_clock_str(dt=None) -> str:
    dt = dt or _chicago_now()
    return dt.strftime("%H:%M")


def is_midday_macro_window(dt=None) -> bool:
    """True during 11:00–11:44 America/Chicago (11:00 AM CDT target window)."""
    dt = dt or _chicago_now()
    tt = dt.time()
    # Drop tzinfo if present so we can compare to naive time() bounds
    if getattr(tt, "tzinfo", None) is not None:
        tt = tt.replace(tzinfo=None)
    return MIDDAY_MACRO_START <= tt <= MIDDAY_MACRO_END


def is_full_scan_window(dt=None) -> bool:
    """
    True at/after FIRST_FULL_SCAN_CDT (default 08:45 America/Chicago).

    The 08:30 CDT tick is the ET cash open; Yahoo option chains are not
    populated yet and a full universe scan prints no_liq_data on all 10
    names. Carry review + exits still run on that tick via exit-only.
    """
    dt = dt or _chicago_now()
    tt = dt.time()
    if getattr(tt, "tzinfo", None) is not None:
        tt = tt.replace(tzinfo=None)
    boundary = time(
        int(getattr(config, "FIRST_FULL_SCAN_HOUR", 8)),
        int(getattr(config, "FIRST_FULL_SCAN_MINUTE", 45)),
    )
    return tt >= boundary


def macro_vector_local(ticker: str) -> str:
    innovation_data = get_innovation_context(ticker, days=7) or ""
    if not innovation_data.strip():
        return "No specific macro or supply-chain catalysts identified for this ticker."
    # CHINA_MACRO / GOV_POLICY rows were synthesized. Ignore leftovers so
    # they cannot keep tagging S after those scrapers were disabled.
    kept = [
        ln for ln in innovation_data.splitlines()
        if "[CHINA_MACRO]" not in ln.upper()
        and "[GOV_POLICY]" not in ln.upper()
    ]
    innovation_data = "\n".join(kept)
    if not innovation_data.strip():
        return "No specific macro or supply-chain catalysts identified for this ticker."
    low = innovation_data.lower()
    if "supply-chain bottlenecks" in low or "bottleneck" in low or "tariff" in low:
        return "SUPPLY_CHAIN_BOTTLENECK: Detected supply-chain / tariff friction (local scan)."
    if "rate cut" in low or "subsidize" in low or "subsidy" in low:
        return "EXPANSIONARY_TAILWIND: Accommodative / subsidy signals (local scan)."
    try:
        import earnings_blackout
        if earnings_blackout.is_earnings_imminent(ticker):
            return "EARNINGS_IMMINENT: Print is inside the bounded earnings window."
    except Exception as e:
        print(f"[midday] earnings_imminent check failed {ticker}: {e}")
    return "Neutral macroeconomic backdrop. No critical tailwinds or bottlenecks detected."


def adversarial_local(card) -> dict:
    """
    Local adversarial fallback (no LLM).

    Liquidity is a Part C contract reject, not an adversarial veto.
    sentiment_score is signed S (-15..+15); 0 means no news, not "hostile".
    """
    return {
        "veto_triggered": False,
        "risk_confidence": 0.20,
        "reason": "Local adversarial: setup structurally stable.",
    }


def load_baseline() -> dict:
    try:
        if not os.path.exists(BASELINE_PATH):
            return {}
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[midday] WARNING: could not load baseline: {e}")
        return {}


def save_baseline(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(BASELINE_PATH) or ".", exist_ok=True)
        tmp = BASELINE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, BASELINE_PATH)
        try:
            import write_guard
            write_guard.record_write_ok("session_baseline")
        except Exception:
            pass
    except Exception as e:
        print(f"[midday] WARNING: could not save baseline: {e}")
        try:
            import write_guard
            write_guard.record_write_fail(
                "session_baseline", e, detail=str(BASELINE_PATH)
            )
        except Exception:
            pass


def store_morning_briefing(briefing_text: str, session_date: str | None = None) -> dict:
    today = session_date or _edt_now().strftime("%Y-%m-%d")
    base = load_baseline()
    if base.get("date") != today:
        base = {"date": today, "tickers": {}, "morning_briefing": "", "established_at": None}
    base["date"] = today
    base["morning_briefing"] = (briefing_text or "")[:12000]
    base["morning_briefing_at"] = datetime.utcnow().isoformat() + "Z"
    save_baseline(base)
    print(f"[midday] Morning briefing stored in baseline ({len(base['morning_briefing'])} chars).")
    return base


def _card_snapshot(card, atr_abs=None) -> dict[str, Any]:
    tm = card.metrics.get("technical", {})
    lm = card.metrics.get("liquidity", {})
    sm = card.metrics.get("sentiment", {})
    return {
        "total_score": card.total_score,
        "action_flag": card.action_flag,
        "liquidity_score": card.liquidity_score,
        "technical_score": card.technical_score,
        "sentiment_score": card.sentiment_score,
        "spot": tm.get("close"),
        "pivot": tm.get("pivot"),
        "r1": tm.get("r1"),
        "s1": tm.get("s1"),
        "pct_change": tm.get("pct_change"),
        "atr_pct": tm.get("atr_pct"),
        "median_spread_pct": lm.get("median_atm_spread_pct"),
        "futures_pct": sm.get("futures_pct"),
        "macro_note": sm.get("macro_note"),
        "atr_abs": atr_abs,
    }


def _num(v, default=None):
    try:
        if v is None or v == "N/A":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_money(v) -> str:
    n = _num(v)
    if n is None:
        return "-"
    return f"${n:.2f}"


def _fmt_contract_cell(contract: dict | None) -> str:
    if not contract or "error" in contract:
        return "-"
    direction = str(contract.get("direction") or "").upper()
    letter = "C" if "CALL" in direction or direction == "C" else "P"
    strike = contract.get("strike")
    exp = contract.get("expiration") or ""
    try:
        # YYYY-MM-DD → MM/DD
        if len(exp) >= 10:
            exp_s = f"{exp[5:7]}/{exp[8:10]}"
        else:
            exp_s = exp
    except Exception:
        exp_s = str(exp)
    try:
        strike_s = f"{float(strike):g}"
    except (TypeError, ValueError):
        strike_s = str(strike or "?")
    return f"{strike_s}{letter} {exp_s}"


def _fmt_score_subs(card) -> str:
    """piv= mom= vol= T= S= liq= dATR= for EXECUTE telemetry (forensic)."""
    try:
        return scoring_engine.format_subscore_bits(card)
    except Exception:
        return ""


def _fmt_extrinsic(contract: dict | None, card=None) -> str:
    """
    Compact moneyness + Stage 4 C-E fields + score components:
      piv=… mom=… vol=… T=… S=… liq=… dATR=…
      ext=$0.51 (2.8%) dte=1.2 rm_atr=0.31 decay=2.5%/hr
    Appends δ=… only when the chain already supplied delta (no local BS).
    """
    parts: list[str] = []
    if card is not None:
        sub = _fmt_score_subs(card)
        if sub:
            parts.append(sub)
    if not contract or "error" in contract:
        return " ".join(parts)
    entry = _num(contract.get("entry_premium"))
    ext = _num(contract.get("extrinsic"))
    ext_pct = _num(contract.get("extrinsic_pct"))
    # Recompute if strike_selector did not attach fields (legacy contracts)
    if ext is None and entry is not None:
        spot = _num(contract.get("spot"))
        strike = _num(contract.get("strike"))
        direction = str(contract.get("direction") or "").upper()
        if spot is not None and strike is not None:
            if "PUT" in direction or direction == "P":
                intrinsic = max(0.0, strike - spot)
            else:
                intrinsic = max(0.0, spot - strike)
            ext = entry - intrinsic
            ext_pct = (ext / entry * 100.0) if entry > 0 else None
    if ext is not None and entry is not None:
        if ext_pct is None and entry > 0:
            ext_pct = ext / entry * 100.0
        if ext_pct is not None:
            parts.append(f"ext=${ext:.2f} ({ext_pct:.1f}%)")
        else:
            parts.append(f"ext=${ext:.2f}")
    # C-E: dte / required_move_atr / decay_density on every EXECUTE line
    dte = _num(contract.get("days_to_expiration"))
    if dte is not None:
        parts.append(f"dte={dte:g}")
    elif contract.get("calendar_dte") is not None:
        parts.append(f"dte={contract.get('calendar_dte')}")
    rm = _num(contract.get("required_move_atr"))
    if rm is not None:
        parts.append(f"rm_atr={rm:.2f}")
    dens = _num(contract.get("decay_density"))
    if dens is not None:
        parts.append(f"decay={dens:.1f}%/hr")
    delta = _num(contract.get("delta"))
    if delta is not None:
        parts.append(f"δ={delta:.2f}")
    return " ".join(parts)


def compute_deltas(ticker: str, prev: dict | None, cur: dict) -> list[str]:
    if not prev:
        return [f"NEW baseline | score {cur.get('total_score')} → {cur.get('action_flag')}"]

    changes = []
    if prev.get("action_flag") != cur.get("action_flag"):
        changes.append(f"FLAG {prev.get('action_flag')}→{cur.get('action_flag')}")

    ps, cs = _num(prev.get("total_score")), _num(cur.get("total_score"))
    if ps is not None and cs is not None and abs(cs - ps) >= SCORE_DELTA_MIN:
        changes.append(f"score {ps:g}→{cs:g} (Δ{cs - ps:+.1f})")

    pp, cp = _num(prev.get("pct_change")), _num(cur.get("pct_change"))
    if pp is not None and cp is not None and abs(cp - pp) >= PCT_CHANGE_DELTA_MIN:
        changes.append(f"day% {pp:+.2f}→{cp:+.2f}")

    psp, csp = _num(prev.get("spot")), _num(cur.get("spot"))
    if psp and csp and abs(csp - psp) / max(abs(psp), 1e-9) >= 0.01:
        changes.append(f"spot {psp:g}→{csp:g}")

    return changes


def _purge_banned(text: str) -> str:
    out = text or ""
    for phrase in BANNED_FILLER:
        out = re.sub(re.escape(phrase), "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _limit_rationale_words(bullet: str, max_words: int = 25) -> str:
    """Keep '**TK**: Spot ... Score: X/100.' then cap remaining rationale to max_words."""
    m = re.search(r"(Score:\s*[\d.]+/100\.)\s*(.*)$", bullet, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        words = bullet.split()
        if len(words) > max_words + 12:
            return " ".join(words[: max_words + 12])
        return bullet
    head = bullet[: m.start(2)].rstrip()
    tail = (m.group(2) or "").strip()
    words = tail.split()
    if len(words) > max_words:
        tail = " ".join(words[:max_words])
    return f"{head} {tail}".strip() if tail else head


def _row_subscore_bits(row: dict) -> str:
    """Sub-score string from card or precomputed bits — every telemetry row."""
    card = row.get("card")
    if card is not None:
        return _fmt_score_subs(card)
    bits = row.get("score_subs")
    return str(bits) if bits else ""


def deterministic_telemetry_bullet(row: dict) -> str:
    """
    KEY TELEMETRY bullet. Numbers first: always attach piv/mom/vol/T/S/liq/dATR.
    Prose is secondary and truncated first if the payload is long.
    """
    ticker = row.get("ticker")
    spot = _fmt_money(row.get("spot")).replace("$", "")
    pivot = _fmt_money(row.get("pivot")).replace("$", "")
    score = row.get("total_score")
    try:
        score_s = f"{float(score):.1f}"
    except (TypeError, ValueError):
        score_s = str(score)
    rel = ">" if _num(row.get("spot"), 0) >= _num(row.get("pivot"), 0) else "<"
    sub_bits = _row_subscore_bits(row)
    sub_tail = f" {sub_bits}" if sub_bits else ""

    rationale = ""
    if row.get("action_flag") == "EXECUTE" and row.get("contract"):
        c = dict(row["contract"])
        if c.get("spot") is None and row.get("spot") is not None:
            c["spot"] = row.get("spot")
        # Contract fields only — subs already attached once
        ext_s = _fmt_extrinsic(c, card=None)
        ext_bit = f" {ext_s}" if ext_s else ""
        try:
            qty_s = str(int(c.get("quantity") or 1))
        except (TypeError, ValueError):
            qty_s = "1"
        lim = c.get("bp_limited")
        lim_bit = f" {lim}" if lim else ""
        rationale = (
            f"Setup {_fmt_contract_cell(c)} qty={qty_s}{lim_bit} "
            f"entry {_fmt_money(c.get('entry_premium'))} "
            f"SL {_fmt_money(c.get('stop_loss'))} TP {_fmt_money(c.get('take_profit'))}"
            f"{ext_bit}."
        )
    elif row.get("block_reason") or (
        row.get("card") and getattr(row.get("card"), "block_reason", None)
    ):
        br = row.get("block_reason") or getattr(row.get("card"), "block_reason", None)
        rationale = f"block={br}."
    # else: no prose — numbers only (prefer subs over "Holding pivot…")

    bullet = (
        f"- **{ticker}**: Spot ${spot} {rel} Pivot ${pivot}. "
        f"Score: {score_s}/100.{sub_tail}"
        + (f" {rationale}" if rationale else "")
    )
    # Prefer keeping numbers: truncate prose first via high word budget on subs
    max_words = 80 if row.get("action_flag") == "EXECUTE" else 56
    return _limit_rationale_words(_purge_banned(bullet), max_words)


def format_thirty_min_scan_discord(
    rows_by_ticker: dict[str, dict],
    *,
    universe: list[str],
    clock_cdt: str | None = None,
    telemetry_bullets: list[str] | None = None,
    gate_summary: str | None = None,
) -> str:
    """
    SINGLE Discord payload for a 30-minute scan cycle.

    Table lists every production ticker; telemetry only EXECUTE / changed.
    gate_summary is a compact one-line Stage 3 rejection/admit tally.
    """
    clock = clock_cdt or cdt_clock_str()
    lines = [
        f"📊 **30-MIN SCAN [{clock} CDT]**",
        "",
        "| Ticker | Status | Contract | Qty | Entry | SL | TP |",
        "|---|---|---|---|---|---|---|",
    ]
    for ticker in universe:
        r = rows_by_ticker.get(ticker) or {}
        status = r.get("action_flag") or ("ERR" if r.get("error") else "PASS")
        contract = r.get("contract")
        if status == "EXECUTE" and contract and "error" not in contract:
            c_cell = _fmt_contract_cell(contract)
            try:
                qty_s = str(int(contract.get("quantity") or 1))
            except (TypeError, ValueError):
                qty_s = "1"
            if contract.get("bp_limited"):
                qty_s = f"{qty_s} {contract['bp_limited']}"
            entry = _fmt_money(contract.get("entry_premium"))
            sl = _fmt_money(contract.get("stop_loss"))
            tp = _fmt_money(contract.get("take_profit"))
        else:
            c_cell, qty_s, entry, sl, tp = "-", "-", "-", "-", "-"
        lines.append(
            f"| {ticker} | {status} | {c_cell} | {qty_s} | {entry} | {sl} | {tp} |"
        )

    if gate_summary:
        lines.append("")
        lines.append(f"`{gate_summary}`")

    lines.append("")
    lines.append("---")
    lines.append("**KEY TICKER TELEMETRY:**")
    if telemetry_bullets:
        lines.extend(telemetry_bullets)
    else:
        lines.append("- No EXECUTE or status/score changes this cycle.")

    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1850] + "\n…(truncated)"
    return text


def deepseek_key_telemetry(
    candidates: list[dict],
    *,
    timeout_s: int = LLM_TIMEOUT_S,
) -> list[str]:
    """
    One DeepSeek-primary call for all KEY TELEMETRY bullets (EXECUTE / changed).
    Falls back to deterministic bullets on failure.
    """
    if not candidates:
        return []

    payload_lines = []
    for r in candidates:
        c = r.get("contract") if r.get("action_flag") == "EXECUTE" else None
        ext_label = None
        if c:
            c_for_ext = dict(c)
            if c_for_ext.get("spot") is None and r.get("spot") is not None:
                c_for_ext["spot"] = r.get("spot")
            ext_label = _fmt_extrinsic(c_for_ext, card=None) or None
        card = r.get("card")
        sub = {}
        if card is not None and isinstance(getattr(card, "metrics", None), dict):
            sub = (card.metrics or {}).get("subscores") or {}
        payload_lines.append(
            json.dumps(
                {
                    "ticker": r.get("ticker"),
                    "status": r.get("action_flag"),
                    "spot": r.get("spot"),
                    "pivot": r.get("pivot"),
                    "score": r.get("total_score"),
                    # Sub-scores first-class — copy verbatim; prefer over prose
                    "piv": sub.get("pivot_sub"),
                    "mom": sub.get("mom_sub"),
                    "vol": sub.get("vol_mult"),
                    "T": sub.get("T", getattr(card, "technical_score", None) if card else None),
                    "S": sub.get("S", getattr(card, "sentiment_score", None) if card else None),
                    "liq": sub.get("liq_mult"),
                    "dATR": sub.get("atr_distance"),
                    "atm_n": sub.get("atm_n"),
                    "med_spr": sub.get("med_spr"),
                    "usable": sub.get("usable"),
                    "block": sub.get("block_reason")
                    or getattr(card, "block_reason", None)
                    or r.get("block_reason"),
                    "score_subs": _row_subscore_bits(r) or None,
                    "changes": r.get("changes") or [],
                    "contract": _fmt_contract_cell(c) if c else None,
                    "qty": (c.get("quantity") if c else None) or (1 if c else None),
                    "bp_limited": c.get("bp_limited") if c else None,
                    "entry": c.get("entry_premium") if c else None,
                    "sl": c.get("stop_loss") if c else None,
                    "tp": c.get("take_profit") if c else None,
                    "ext": ext_label,
                    "extrinsic": c.get("extrinsic") if c else None,
                    "extrinsic_pct": c.get("extrinsic_pct") if c else None,
                    "days_to_expiration": c.get("days_to_expiration") if c else None,
                    "required_move_atr": c.get("required_move_atr") if c else None,
                    "decay_density": c.get("decay_density") if c else None,
                    "delta": c.get("delta") if c else None,
                },
                separators=(",", ":"),
                default=str,
            )
        )

    user = (
        "Produce KEY TICKER TELEMETRY bullets for these rows only:\n"
        + "\n".join(payload_lines)
        + "\n\nOne bullet per ticker. ALWAYS copy piv= mom= vol= T= S= liq= dATR= "
        "from score_subs (or piv/mom/vol/T/S/liq/dATR fields) verbatim after Score. "
        "Prefer numbers over prose; omit commentary if long. Banned filler enforced."
    )
    print(
        f"[scan] KEY TELEMETRY via DeepSeek primary "
        f"({len(candidates)} tickers, model={llm_chain.DEEPSEEK_MODEL})..."
    )
    try:
        raw = llm_chain.generate_text(
            user,
            primary="deepseek",
            step="scan_telemetry",
            system=DEEPSEEK_TRADE_SYSTEM,
            timeout_s=timeout_s,
        )
        raw = _purge_banned(raw or "")
        bullets = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if not line.startswith("-"):
                line = f"- {line.lstrip('* ')}"
            bullets.append(_limit_rationale_words(line, 25))
        if bullets:
            return bullets
    except Exception as exc:
        print(f"[scan] DeepSeek telemetry failed ({exc}); deterministic bullets.")

    return [deterministic_telemetry_bullet(r) for r in candidates]


def run_midday_macro_meeting(
    *,
    morning_excerpt: str = "",
    rows: list[dict] | None = None,
    timeout_s: int | None = None,
) -> str:
    """
    Comprehensive Midday Macro & News Update — DeepSeek primary, Gemini backup.

    Gemini is reserved for the pre-market brief. Timeout default 60s
    (MIDDAY_LLM_TIMEOUT_S). Called ONLY from master_bot inside the 11:00 AM
    CDT window (once/day). Never used as a prefix on 30-minute scan payloads.
    """
    brief = " ".join((morning_excerpt or "").split())[:500]
    table = []
    for r in rows or []:
        table.append(
            f"{r.get('ticker')}|{r.get('action_flag')}|{r.get('total_score')}|"
            f"spot={r.get('spot')}|pivot={r.get('pivot')}"
        )
    system = (
        "You facilitate a once-daily midday macro & news update for a hedge fund radar. "
        "Synthesize what changed since the morning brief. Data-dense. No CEO filler. "
        "No 30-MIN SCAN tables."
    )
    prompt = f"""Time: 11:00 AM CDT midday macro window.

Morning brief excerpt (context only, ≤500 chars):
{brief or "(none)"}

Current book snapshot (ticker|flag|score|spot|pivot):
{chr(10).join(table) if table else "(no snapshot)"}

Write a Discord post MAX 900 characters:
- Title: **📊 MIDDAY MACRO & NEWS UPDATE (11:00 CDT)**
- Macro/news deltas since morning only
- Optional: 2–4 ticker callouts if material
- Forbidden: 30-MIN SCAN headers, CEO language, banned filler
"""
    if timeout_s is None:
        timeout_s = _midday_llm_timeout_s()
    print(
        f"[midday] 📋 Midday MACRO meeting (DeepSeek primary → Gemini backup, "
        f"model={llm_chain.DEEPSEEK_MODEL}, timeout={timeout_s}s) "
        "— isolated 11:00 CDT slot only"
    )
    try:
        text = llm_chain.generate_text(
            prompt,
            primary="deepseek",
            step="midday_macro_meeting",
            system=system,
            timeout_s=timeout_s,
        )
        text = _purge_banned((text or "").strip())
        if not text:
            raise RuntimeError("empty midday macro text")
        # Never allow 30-min branding on macro message
        text = re.sub(r"30-MIN SCAN", "BOOK SNAPSHOT", text, flags=re.IGNORECASE)
        if len(text) > 900:
            text = text[:880] + "\n…(truncated)"
        if "MIDDAY MACRO" not in text.upper():
            text = f"**📊 MIDDAY MACRO & NEWS UPDATE (11:00 CDT)**\n{text}"
        return text
    except Exception as exc:
        print(f"[midday] Macro meeting LLM failed ({exc}); deterministic fallback.")
        llm_chain.alert_llm_dual_fail("midday_macro_meeting", exc)
        return (
            "**📊 MIDDAY MACRO & NEWS UPDATE (11:00 CDT)**\n"
            f"Morning brief on file ({len(brief)} chars). "
            f"Book rows: {len(rows or [])}. "
            "Deterministic fallback — dual LLM fail (see CRITICAL)."
        )


def _discord_scan_fail_threshold() -> int:
    try:
        return max(1, int(getattr(config, "MARK_FAIL_ALERT_STREAK", 2)))
    except (TypeError, ValueError):
        return 2


def _note_scan_discord_result(
    ok: bool,
    *,
    scan_id: str,
    clock: str,
    n_admits: int,
    gate_summary: str | None = None,
) -> None:
    """
    Failed 30-min scan Discord posts are otherwise silent while admits persist.

    Same CRITICAL shape as mark failures: after MARK_FAIL_ALERT_STREAK
    consecutive misses, or immediately if this scan admitted positions.
    write_guard also counts store=discord_scan.
    """
    try:
        import write_guard
        if ok:
            write_guard.record_write_ok("discord_scan")
            return
        n = write_guard.record_write_fail(
            "discord_scan",
            detail=f"scan={scan_id} clock={clock} admits={n_admits}",
        )
    except Exception as wg_err:
        print(f"[scan] write_guard discord_scan warn: {wg_err}")
        if ok:
            return
        n = 1

    thresh = _discord_scan_fail_threshold()
    if n_admits <= 0 and n < thresh:
        print(
            f"[scan] Discord 30-min payload FAILED scan={scan_id} "
            f"consecutive={n}/{thresh} admits=0 — no CRITICAL yet"
        )
        return

    why = (
        f"{n_admits} position(s) admitted this scan — book changed while Discord is dark"
        if n_admits > 0
        else f"{n} consecutive 30-min scan Discord failures"
    )
    msg = (
        f"🚨 **CRITICAL: SCAN DISCORD FAILED**\n"
        f"30-MIN SCAN `{scan_id}` at {clock} CDT did **not** post.\n"
        f"{why}.\n"
        f"Admits this scan: **{n_admits}**. "
        f"Consecutive post failures: **{n}**.\n"
        f"`{gate_summary or 'GATE n/a'}`\n"
        f"Scan itself succeeded — check webhook / Render logs."
    )
    print(f"[scan] {msg.replace(chr(10), ' | ')}")
    try:
        import broadcaster
        delivered = broadcaster.send_discord_alert(msg)
        print(f"[scan] scan-discord CRITICAL delivered={delivered}")
    except Exception as e:
        print(f"[scan] scan-discord CRITICAL send failed: {e}")


def score_tickers_for_book(
    tickers: list[str],
    breaker: CircuitBreaker,
    *,
    inter_ticker_sleep: float = 0.5,
) -> dict[str, dict[str, Any]]:
    """
    Score a ticker subset (open book for morning carry / thesis void).

    No gate, no admits, no Discord table. Returns ticker ->
    {card, options_dict, pivot_data, atr_abs} for run_morning_carry_review.
    """
    from master_bot import (
        fetch_atr,
        fetch_intraday_drift,
        get_latest_futures_pct,
        ensure_news_context,
        MasterBotScanError,
        _call_with_timeout,
        API_CALL_TIMEOUT_S,
    )

    scored: dict[str, dict[str, Any]] = {}
    if not tickers:
        return scored
    futures_pct = get_latest_futures_pct("ES=F")
    universe = [str(t).upper().strip() for t in tickers if t]
    print(
        f"[score-book] scoring {len(universe)} open ticker(s) "
        f"(no universe admit): {', '.join(universe)}"
    )
    for idx, ticker in enumerate(universe):
        try:
            options_json = _call_with_timeout(
                lambda t=ticker: fetch_options_data(t),
                timeout_s=API_CALL_TIMEOUT_S,
                step=f"carry_yf_options:{ticker}",
            )
            options_dict = json.loads(options_json)
            if "error" in options_dict:
                print(
                    f"[score-book] [{ticker}] options error: "
                    f"{options_dict.get('error')}"
                )
                continue
            breaker.record_success(f"options:{ticker}")
            pivot_data = _call_with_timeout(
                lambda t=ticker: fetch_pivot_data(t),
                timeout_s=API_CALL_TIMEOUT_S,
                step=f"carry_yf_pivot:{ticker}",
            )
            atr_abs, atr_pct = fetch_atr(ticker, breaker)
            news_string = ensure_news_context(ticker, breaker)
            drift_pct = fetch_intraday_drift(ticker, breaker)
            card = scoring_engine.score_ticker(
                ticker,
                options_dict,
                pivot_data,
                news_string,
                macro_vector=macro_vector_local(ticker),
                futures_pct=futures_pct,
                atr_pct=atr_pct,
                atr_abs=atr_abs,
                drift_pct=drift_pct,
            )
            print(
                f"[score-book] [{ticker}] T={card.technical_score} "
                f"S={card.sentiment_score:+g} = {card.total_score}/100"
            )
            scored[ticker] = {
                "card": card,
                "options_dict": options_dict,
                "pivot_data": pivot_data if isinstance(pivot_data, dict) else {},
                "atr_abs": atr_abs,
            }
        except MasterBotScanError as e:
            print(f"[score-book] [{ticker}] isolated: {e.step}: {e.message}")
        except Exception as e:
            print(f"[score-book] [{ticker}] error: {e}")
        if idx < len(universe) - 1 and inter_ticker_sleep:
            import time as _time
            _time.sleep(inter_ticker_sleep)
    return scored


def run_thirty_min_scan(
    breaker: CircuitBreaker,
    *,
    tickers=None,
    inter_ticker_sleep: float = 5,
    morning_macro_context: str = "",
):
    """
    30-minute advisory radar scan.

    - Deterministic scoring for all production tickers
    - ONE Discord payload: full table + KEY TELEMETRY (EXECUTE / changed only)
    - Does NOT send Midday Macro headers (isolated to 11:00 CDT job)
    """
    from master_bot import (
        TICKERS,
        fetch_atr,
        fetch_intraday_drift,
        get_latest_futures_pct,
        ensure_news_context,
        record_executed_trade,
        MasterBotScanError,
        _call_with_timeout,
        API_CALL_TIMEOUT_S,
    )

    universe = list(tickers) if tickers is not None else list(TICKERS)
    scan_id = f"scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    try:
        import fill_accounting as _fa
        _fa.note_scan()
    except Exception:
        pass
    result = {
        "scan_id": scan_id,
        "mode": "thirty_min_scan",
        "tickers_scanned": universe,
        "results": [],
        "trades": [],
        "discord_delivered": None,
        "aborted": False,
    }

    if breaker.is_open():
        print("🛑 [scan] Circuit breaker OPEN — 30-min scan suspended.")
        result["aborted"] = True
        result["circuit_breaker_open"] = True
        return result

    today = _edt_now().strftime("%Y-%m-%d")
    baseline = load_baseline()
    if baseline.get("date") != today:
        baseline = {
            "date": today,
            "tickers": {},
            "morning_briefing": (morning_macro_context or "")[:12000],
            "established_at": None,
        }
    elif morning_macro_context and not baseline.get("morning_briefing"):
        baseline["morning_briefing"] = morning_macro_context[:12000]

    open_baseline = not bool(baseline.get("tickers"))
    futures_pct = get_latest_futures_pct("ES=F")
    clock = cdt_clock_str()
    print(
        f"\n🚀 30-MIN SCAN {scan_id} | {clock} CDT | tickers={len(universe)} | "
        f"open_baseline={open_baseline} | ES=F {futures_pct}% | "
        f"score=T+S thr={config.EXECUTE_THRESHOLD}"
    )
    # Heartbeat BEFORE the Yahoo loop so a hung chain fetch cannot look like a dead bot.
    try:
        start_ok = broadcaster.send_discord_alert(
            f"📡 **SCAN START** [{clock} CDT] `{scan_id}` | n={len(universe)}"
        )
        print(f"[scan] start ping delivered={start_ok}")
    except Exception as start_err:
        print(f"[scan] start ping failed: {start_err}")

    new_ticker_state = dict(baseline.get("tickers") or {})
    rows_by_ticker: dict[str, dict] = {}
    telemetry_candidates: list[dict] = []
    # Phase-1 workspace: score all tickers before any admit (rank-before-admit).
    scored: dict[str, dict[str, Any]] = {}

    for idx, ticker in enumerate(universe):
        row: dict[str, Any] = {"ticker": ticker, "error": None}
        try:
            options_json = _call_with_timeout(
                lambda t=ticker: fetch_options_data(t),
                timeout_s=API_CALL_TIMEOUT_S,
                step=f"yf_options:{ticker}",
            )
            options_dict = json.loads(options_json)
            if "error" in options_dict:
                row["error"] = options_dict["error"]
                row["action_flag"] = "PASS"
                rows_by_ticker[ticker] = row
                result["results"].append(row)
                breaker.record_failure(f"options:{ticker}")
                continue
            breaker.record_success(f"options:{ticker}")

            pivot_data = _call_with_timeout(
                lambda t=ticker: fetch_pivot_data(t),
                timeout_s=API_CALL_TIMEOUT_S,
                step=f"yf_pivot:{ticker}",
            )
            atr_abs, atr_pct = fetch_atr(ticker, breaker)
            news_string = ensure_news_context(ticker, breaker)
            macro_vector = macro_vector_local(ticker)
            drift_pct = fetch_intraday_drift(ticker, breaker)

            card = scoring_engine.score_ticker(
                ticker,
                options_dict,
                pivot_data,
                news_string,
                macro_vector=macro_vector,
                futures_pct=futures_pct,
                atr_pct=atr_pct,
                atr_abs=atr_abs,
                drift_pct=drift_pct,
            )
            # Per-scan sub-score line already printed inside score_ticker
            print(
                f"[{ticker}] ⚙️ score "
                f"T={card.technical_score} S={card.sentiment_score:+g} "
                f"= {card.total_score}/100 → "
                f"{card.action_flag}"
                + (f" ({card.block_reason})" if getattr(card, "block_reason", None) else "")
            )

            adv = None
            if card.action_flag == "EXECUTE":
                adv = adversarial_local(card)
                if adv.get("veto_triggered") and float(adv.get("risk_confidence", 0)) > 0.75:
                    scoring_engine.apply_adversarial_penalty(
                        card, 15.0, adv.get("reason") or "local veto"
                    )
                    print(f"[{ticker}] 🛑 Local adversarial veto → {card.action_flag}")

            direction = None
            if card.action_flag == "EXECUTE":
                direction = strike_selector.infer_direction(pivot_data)

            scored[ticker] = {
                "card": card,
                "adv": adv,
                "options_dict": options_dict,
                "pivot_data": pivot_data,
                "atr_abs": atr_abs,
                "direction": direction,
            }

        except MasterBotScanError as e:
            print(f"[{ticker}] isolated: {e.step}: {e.message}")
            row["error"] = f"{e.step}: {e.message}"
            row["action_flag"] = "PASS"
            step = str(e.step or "")
            br = None
            if "pivot" in step:
                br = "no_pivot_data"
            elif "atr" in step:
                br = "no_atr_data"
            if br:
                print(f"REJECT {ticker}:{br}")
                card = scoring_engine.data_fail_card(ticker, br)
                row["block_reason"] = br
                scored[ticker] = {
                    "card": card,
                    "adv": None,
                    "options_dict": {},
                    "pivot_data": {"error": br},
                    "atr_abs": None,
                    "direction": None,
                }
            rows_by_ticker[ticker] = row
            result["results"].append(row)
        except Exception as e:
            print(f"[{ticker}] error: {e}")
            row["error"] = str(e)
            row["action_flag"] = "PASS"
            rows_by_ticker[ticker] = row
            result["results"].append(row)

        if idx < len(universe) - 1 and inter_ticker_sleep:
            import time as _time
            _time.sleep(inter_ticker_sleep)

    # ---- Stage 4: carry review (first scan of day) + exits, then gate sync ----
    # Order is intentional: mark/close carried book BEFORE admits so
    # sync_open_from_book sees the true open set (no double-admit on carry).
    # Reuses phase-1 options_dict / scores — no extra yfinance when open ⊆ universe.
    exit_summary: dict[str, Any] = {}
    carry_summary: dict[str, Any] = {}
    try:
        import position_exits
        from tracker_agent import load_active_trades as _load_open

        open_before_exits = _load_open()
        now_cdt = _chicago_now()

        # Morning carry review once/day before any new entries this session
        if open_before_exits and not position_exits.carry_review_already_done(
            now_cdt.date()
        ):
            print(
                f"[scan] CARRY REVIEW: {len(open_before_exits)} open position(s) "
                f"vs today's pivot/score"
            )
            carry_summary = position_exits.run_morning_carry_review(
                open_before_exits,
                scored,
                scan_id=scan_id,
                now_cdt=now_cdt,
            )
            result["carry_review"] = {
                "ran": carry_summary.get("ran"),
                "held": carry_summary.get("held"),
                "closed": [
                    {
                        "ticker": c.get("ticker"),
                        "reason": c.get("reason"),
                        "exit_price": c.get("exit_price"),
                        "pnl": c.get("pnl"),
                    }
                    for c in (carry_summary.get("closed") or [])
                ],
                "lines": carry_summary.get("lines"),
            }
            # Reload book after carry closes
            open_before_exits = _load_open()
        elif not open_before_exits and not position_exits.carry_review_already_done(
            now_cdt.date()
        ):
            # No open book — still mark review done so we don't re-check all day
            position_exits.mark_carry_review_done(now_cdt.date())

        if open_before_exits:
            print(
                f"[scan] Stage 4 exits: evaluating {len(open_before_exits)} open "
                f"position(s) (selective EOD/expiry/SL/TP + marks)"
            )
            exit_summary = position_exits.run_scan_exits(
                open_before_exits,
                scored,
                scan_id=scan_id,
                now_cdt=now_cdt,
            )
            result["exits"] = {
                "closed": [
                    {
                        "ticker": c.get("ticker"),
                        "reason": c.get("reason"),
                        "exit_price": c.get("exit_price"),
                        "pnl": c.get("pnl"),
                    }
                    for c in (exit_summary.get("closed") or [])
                ],
                "marks_recorded": exit_summary.get("marks_recorded"),
                "eod_triggered": exit_summary.get("eod_triggered"),
                "open_after": exit_summary.get("open_after"),
            }
            if exit_summary.get("closed"):
                try:
                    lines = [
                        position_exits.format_closed_discord_line(c)
                        for c in exit_summary["closed"]
                    ]
                    broadcaster.send_discord_alert(
                        "📉 **SCAN EXITS**\n" + "\n".join(lines)
                    )
                except Exception as disc_err:
                    print(f"[scan] exit Discord warn: {disc_err}")
    except Exception as exit_err:
        print(f"[scan] Stage 4 exits error (continuing to gate): {exit_err}")
        result["exits_error"] = str(exit_err)

    # ---- Stage 3 gate: sync durable book (post-exit/carry), rank-before-admit ----
    gate = signal_gate.get_gate()
    try:
        from tracker_agent import load_active_trades
        open_tickers = [
            t.get("ticker") for t in load_active_trades() if t.get("ticker")
        ]
        gate.sync_open_from_book(open_tickers)
        print(
            f"[scan] gate sync_open_from_book: {len(open_tickers)} open "
            f"{open_tickers} (before admit)"
        )
    except Exception as sync_err:
        print(f"[scan] gate sync_open_from_book warn: {sync_err}")

    observations = []
    for ticker in universe:
        ctx = scored.get(ticker)
        if not ctx:
            observations.append(
                signal_gate.Observation(ticker=ticker, score=0.0, direction=None, action_flag="PASS")
            )
            continue
        card = ctx["card"]
        observations.append(
            signal_gate.Observation(
                ticker=ticker,
                score=float(card.total_score),
                direction=ctx.get("direction"),
                action_flag=card.action_flag,
                block_reason=getattr(card, "block_reason", None),
            )
        )

    # Tickers closed earlier this scan must not be re-admitted (anti-churn)
    closed_this_scan = {
        str(c.get("ticker") or "").upper().strip()
        for c in (exit_summary.get("closed") or [])
        if c.get("ticker")
    }
    # Carry-review closes also count
    for c in (carry_summary.get("closed") or []):
        if c.get("ticker"):
            closed_this_scan.add(str(c["ticker"]).upper().strip())
    closed_this_scan.discard("")

    now_utc = datetime.now(timezone.utc)
    gate_decisions = gate.process_scan(
        observations, now_utc, closed_this_scan=closed_this_scan
    )
    gate_by_ticker = {d.ticker: d for d in gate_decisions}
    gate_summary = gate.format_scan_summary(gate_decisions)
    print(f"[scan] {gate_summary}")
    result["gate_summary"] = gate_summary

    # ---- Phase 2: strike + paper buy only for admitted tickers ----
    strike_rejects: list[str] = []
    for ticker in universe:
        ctx = scored.get(ticker)
        if not ctx:
            continue
        card = ctx["card"]
        adv = ctx.get("adv")
        options_dict = ctx["options_dict"]
        pivot_data = ctx["pivot_data"]
        atr_abs = ctx.get("atr_abs")
        gdec = gate_by_ticker.get(ticker.upper()) or gate_by_ticker.get(ticker)

        contract = None
        gate_blocked = False
        if card.action_flag == "EXECUTE":
            if gdec is not None and not gdec.admit:
                gate_blocked = True
                card.action_flag = "PASS"
                card.reasons.append(f"Gate: {gdec.reason}")
                print(f"[{ticker}] 🚧 Gate blocked → PASS ({gdec.reason})")
            elif gdec is not None and gdec.admit:
                contract = strike_selector.select_optimal_contract(
                    options_dict, pivot_data, atr_abs=atr_abs, ticker=ticker
                )
                if "error" in contract:
                    card.action_flag = "PASS"
                    card.reasons.append(contract["error"])
                    tag = contract.get("reject_tag") or "strike_fail"
                    strike_rejects.append(f"{ticker}:{tag}")
                    # Full admit rollback — not on_close (never opened a position)
                    try:
                        gate.rollback_admit(ticker)
                    except Exception:
                        pass
                    print(
                        f"[{ticker}] 🚧 Contract filter REJECT {tag} → PASS "
                        f"(admit rolled back)"
                    )
                    contract = None
                else:
                    buy_payload = dict(contract)
                    buy_payload.setdefault("ticker", ticker)
                    qty = virtual_broker.apply_entry_quantity(buy_payload)
                    contract["quantity"] = qty
                    if buy_payload.get("bp_limited"):
                        contract["bp_limited"] = buy_payload["bp_limited"]
                    print(
                        f"[EXECUTE] {ticker} "
                        f"{virtual_broker.format_execute_qty_bit(buy_payload, qty)} "
                        f"entry={buy_payload.get('entry_premium')} "
                        f"SL={buy_payload.get('stop_loss')}"
                    )
                    buy_ok = False
                    if qty < 1:
                        if int(buy_payload.get("qty_desired") or 0) <= 0:
                            print(
                                f"REJECT {ticker}:"
                                f"{buy_payload.get('bp_limited') or 'risk_too_large at qty1'}"
                            )
                        else:
                            print(
                                f"[{ticker}] paper_buy blocked: qty=0 (buying power) "
                                f"{buy_payload.get('bp_limited') or ''}".rstrip()
                            )
                    else:
                        try:
                            buy_res = virtual_broker.paper_buy(
                                buy_payload,
                                contract.get("entry_premium"),
                                quantity=qty,
                            )
                            buy_ok = bool(buy_res.get("ok"))
                            if not buy_ok:
                                print(
                                    f"[{ticker}] paper_buy blocked: "
                                    f"{buy_res.get('error')}"
                                )
                        except Exception as be:
                            print(f"[{ticker}] paper_buy warn: {be}")
                    if not buy_ok:
                        card.action_flag = "PASS"
                        card.reasons.append("Broker: insufficient buying_power")
                        contract = None
                        try:
                            gate.rollback_admit(ticker)
                        except Exception:
                            pass
                    else:
                        contract["quantity"] = qty
                        try:
                            record_executed_trade(
                                ticker,
                                contract,
                                scan_id=scan_id,
                                card=card,
                                pivot_data=pivot_data,
                            )
                        except Exception as pe:
                            print(f"[{ticker}] persist warn: {pe}")
                        result["trades"].append({
                            "ticker": ticker,
                            "direction": contract.get("direction"),
                            "strike": contract.get("strike"),
                            "expiration": contract.get("expiration"),
                            "entry_premium": contract.get("entry_premium"),
                            "stop_loss": contract.get("stop_loss"),
                            "take_profit": contract.get("take_profit"),
                            "quantity": qty,
                            "gate_rank": getattr(gdec, "conviction_rank", None),
                            "total_score": card.total_score,
                        })

        snap = _card_snapshot(card, atr_abs=atr_abs)
        prev = (baseline.get("tickers") or {}).get(ticker)
        changes = compute_deltas(ticker, prev, snap)
        if gate_blocked and gdec is not None:
            changes = list(changes or [])
            changes.append(f"GATE {gdec.reason}")
        new_ticker_state[ticker] = snap

        row = {
            "ticker": ticker,
            "error": None,
            "action_flag": card.action_flag,
            "total_score": card.total_score,
            "spot": snap.get("spot"),
            "pivot": snap.get("pivot"),
            "pct_change": snap.get("pct_change"),
            "changes": changes,
            "contract": contract,
            "card": card,
            "block_reason": getattr(card, "block_reason", None),
            "score_subs": _fmt_score_subs(card),
            "gate_reason": None if gdec is None else gdec.reason,
            "gate_admit": None if gdec is None else gdec.admit,
        }
        rows_by_ticker[ticker] = row
        if ticker == "SPY":
            try:
                import fill_accounting as _fa
                _fa.note_spy(snap.get("spot"))
            except Exception:
                pass
        result["results"].append({
            "ticker": ticker,
            "action_flag": card.action_flag,
            "total_score": card.total_score,
            "changes": changes,
            "gate_reason": row["gate_reason"],
            "gate_admit": row["gate_admit"],
        })

        is_execute = card.action_flag == "EXECUTE"
        is_material = bool(changes) and not (
            open_baseline
            and all(str(c).startswith("NEW baseline") for c in changes)
            and not is_execute
        )
        if is_execute or is_material or gate_blocked:
            telemetry_candidates.append(row)

        try:
            telemetry.log_scan_result(
                scan_id,
                card,
                adversarial_result=adv,
                selected_contract=contract,
                agent_params={
                    "mode": "thirty_min_scan",
                    "llm_policy": "deepseek_telemetry",
                    "gate_reason": row["gate_reason"],
                    "gate_admit": row["gate_admit"],
                },
            )
        except Exception as te:
            print(f"[{ticker}] telemetry WARNING: {te}")

    # Data-failure + dead-zone + Part C rejects on the GATE summary line.
    # Chain-level no_liq_data is forensic/CRITICAL only — it does not zero the score.
    DATA_BLOCKS = (
        "no_momentum_data",
        "dead_zone",
    )
    data_rejects: list[str] = []
    data_fail_count = 0  # no_liq_data + no_momentum_data (outages)
    seen_liq: set[str] = set()
    for ticker, ctx in scored.items():
        card = ctx.get("card")
        br = getattr(card, "block_reason", None) if card is not None else None
        liq_status = None
        if card is not None and isinstance(getattr(card, "metrics", None), dict):
            liq_status = (card.metrics.get("subscores") or {}).get("liq_status")
        if liq_status == "no_liq_data":
            data_rejects.append(f"{ticker}:no_liq_data")
            data_fail_count += 1
            seen_liq.add(str(ticker).upper())
        if br in DATA_BLOCKS:
            data_rejects.append(f"{ticker}:{br}")
        if br == "no_momentum_data":
            data_fail_count += 1
    for bit in strike_rejects:
        tag = str(bit)
        name = tag.split(":", 1)[0].upper() if ":" in tag else ""
        if "no_liq_data" in tag and name not in seen_liq:
            data_fail_count += 1
            seen_liq.add(name)
    reject_bits = data_rejects + strike_rejects
    if reject_bits:
        reject_line = "REJECT " + " ".join(reject_bits)
        gate_summary = f"{gate_summary} | {reject_line}"
        result["gate_summary"] = gate_summary
        print(f"[scan] {reject_line}")

    # Whole-book data outage: loud, not silent
    DATA_FAIL_CRITICAL_N = 3
    if data_fail_count > DATA_FAIL_CRITICAL_N:
        msg = (
            f"🚨 **CRITICAL: SCORE DATA OUTAGE** | "
            f"{data_fail_count} tickers hit no_liq_data/no_momentum_data "
            f"this scan (>{DATA_FAIL_CRITICAL_N}). "
            f"Failures: {', '.join(data_rejects) or 'n/a'}. "
            f"Not scoring as weak setups — investigate Yahoo/options/pivot feed."
        )
        print(f"[scan] {msg}")
        try:
            broadcaster.send_discord_alert(msg)
        except Exception as crit_err:
            print(f"[scan] data-outage Discord warn: {crit_err}")
        result["data_outage_critical"] = True
        result["data_fail_count"] = data_fail_count

    baseline["tickers"] = new_ticker_state
    if open_baseline:
        baseline["established_at"] = datetime.utcnow().isoformat() + "Z"
    baseline["last_scan_id"] = scan_id
    baseline["last_scan_at"] = datetime.utcnow().isoformat() + "Z"
    baseline["last_scan_rows"] = [
        {
            "ticker": t,
            "action_flag": rows_by_ticker.get(t, {}).get("action_flag"),
            "total_score": rows_by_ticker.get(t, {}).get("total_score"),
            "spot": rows_by_ticker.get(t, {}).get("spot"),
            "pivot": rows_by_ticker.get(t, {}).get("pivot"),
        }
        for t in universe
    ]
    save_baseline(baseline)

    bullets = deepseek_key_telemetry(telemetry_candidates)
    payload = format_thirty_min_scan_discord(
        rows_by_ticker,
        universe=universe,
        clock_cdt=clock,
        telemetry_bullets=bullets,
        gate_summary=gate_summary,
    )
    # Hard guarantee: never ship MIDDAY headers on 30-min cycles
    if re.search(r"MIDDAY\s+(MEETING|MACRO)", payload, re.I):
        payload = re.sub(r"MIDDAY\s+(MEETING|MACRO)[^\n]*", "", payload, flags=re.I)

    ok = broadcaster.send_discord_alert(payload)
    result["discord_delivered"] = bool(ok)
    result["payload_preview"] = payload[:200]
    n_admits = len(result.get("trades") or [])
    _note_scan_discord_result(
        ok,
        scan_id=scan_id,
        clock=clock,
        n_admits=n_admits,
        gate_summary=gate_summary,
    )
    print(f"✅ 30-MIN SCAN {scan_id} complete | discord={ok} | telemetry={len(bullets)}")
    return result


def run_exit_only_pass(
    breaker: CircuitBreaker,
    *,
    inter_ticker_sleep: float = 0.5,
) -> dict[str, Any]:
    """
    Lightweight off-cycle exit pass (default every 5 min).

    - Quotes each open contract via fetch_contract_quote (one expiry, one strike)
    - Falls back to full fetch_options_data only if the light path fails
    - Selective EOD / 0DTE / SL-TP / B5 — no score, admit, or LLM
    - Syncs gate open book; Discord only when something closes
    """
    from master_bot import (
        MasterBotScanError,
        _call_with_timeout,
        API_CALL_TIMEOUT_S,
    )
    from data_engineer import fetch_options_data, fetch_contract_quote
    from tracker_agent import load_active_trades
    import position_exits

    scan_id = f"exit-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    clock = cdt_clock_str()
    result: dict[str, Any] = {
        "scan_id": scan_id,
        "mode": "exit_only",
        "closed": [],
        "open_before": 0,
        "open_after": 0,
        "quote_mode_counts": {"single_contract": 0, "full_chain_fallback": 0, "failed": 0},
    }

    if breaker.is_open():
        print("🛑 [exit-pass] Circuit breaker OPEN — exit pass suspended.")
        result["aborted"] = True
        return result

    try:
        open_trades = load_active_trades()
    except Exception as e:
        print(f"[exit-pass] load_active_trades failed: {e}")
        result["error"] = str(e)
        return result

    result["open_before"] = len(open_trades)
    if not open_trades:
        try:
            import position_exits as _pex
            now_c = _chicago_now()
            if not _pex.carry_review_already_done(now_c.date()):
                _pex.mark_carry_review_done(now_c.date())
        except Exception:
            pass
        print(f"[exit-pass] {clock} CDT — no open positions; skip mark fetches.")
        return result

    print(
        f"\n⏱️ EXIT-ONLY PASS {scan_id} | {clock} CDT | "
        f"{len(open_trades)} open position(s) | single-contract quotes"
    )

    # Morning carry review on the 08:30 tick (full scan is deferred to 08:45).
    # Score OPEN names only — do not Discord a 10-row no_liq_data table.
    try:
        import position_exits as _pex
        now_c = _chicago_now()
        if not _pex.carry_review_already_done(now_c.date()):
            open_tickers = []
            seen_t = set()
            for t in open_trades:
                tk = str((t or {}).get("ticker") or "").upper().strip()
                if tk and tk not in seen_t:
                    seen_t.add(tk)
                    open_tickers.append(tk)
            scored_carry = score_tickers_for_book(
                open_tickers, breaker, inter_ticker_sleep=inter_ticker_sleep
            )
            print(
                f"[exit-pass] CARRY REVIEW: {len(open_trades)} open "
                f"vs today's pivot/score (no admits)"
            )
            carry_summary = _pex.run_morning_carry_review(
                open_trades,
                scored_carry,
                scan_id=scan_id,
                now_cdt=now_c,
            )
            result["carry_review"] = {
                "ran": carry_summary.get("ran"),
                "held": carry_summary.get("held"),
                "closed": [
                    {
                        "ticker": c.get("ticker"),
                        "reason": c.get("reason"),
                        "exit_price": c.get("exit_price"),
                        "pnl": c.get("pnl"),
                    }
                    for c in (carry_summary.get("closed") or [])
                ],
                "lines": carry_summary.get("lines"),
            }
            open_trades = load_active_trades()
            result["open_before"] = len(open_trades)
            if not open_trades:
                print(
                    f"[exit-pass] {clock} CDT — book flat after carry review."
                )
                return result
    except Exception as carry_err:
        print(f"[exit-pass] carry review warn: {carry_err}")

    # Key quotes by trade identity so multi-leg same ticker (if any) stays correct.
    # lookup in run_scan_exits is by ticker — one options_dict per ticker is enough
    # under no-pyramiding (one open per ticker).
    scored: dict[str, dict[str, Any]] = {}
    for i, trade in enumerate(open_trades):
        if not isinstance(trade, dict):
            continue
        ticker = str(trade.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        exp = trade.get("expiration") or (
            (trade.get("option_contract") or {}).get("expiration")
            if isinstance(trade.get("option_contract"), dict)
            else None
        )
        strike = trade.get("strike")
        if strike is None and isinstance(trade.get("option_contract"), dict):
            strike = trade["option_contract"].get("strike")
        direction = trade.get("direction") or (
            (trade.get("option_contract") or {}).get("direction")
            if isinstance(trade.get("option_contract"), dict)
            else "CALL"
        )

        options_dict = None
        mode = "failed"
        # --- light path: one expiry + one strike ---
        if exp and strike is not None:
            try:
                options_json = _call_with_timeout(
                    lambda t=ticker, e=exp, s=strike, d=direction: fetch_contract_quote(
                        t, e, s, d
                    ),
                    timeout_s=API_CALL_TIMEOUT_S,
                    step=f"exit_yf_quote:{ticker}",
                )
                od = json.loads(options_json)
                if "error" not in od:
                    options_dict = od
                    mode = "single_contract"
                    if ticker == "SPY":
                        try:
                            import fill_accounting as _fa
                            _fa.note_spy(od.get("current_price"))
                        except Exception:
                            pass
                else:
                    print(
                        f"[exit-pass] [{ticker}] light quote failed: {od.get('error')} "
                        f"— trying full chain"
                    )
            except MasterBotScanError as e:
                print(f"[exit-pass] [{ticker}] light quote timeout: {e.message}")
            except Exception as e:
                print(f"[exit-pass] [{ticker}] light quote error: {e}")

        # --- fallback: full multi-expiry chain ---
        if options_dict is None:
            try:
                options_json = _call_with_timeout(
                    lambda t=ticker: fetch_options_data(t),
                    timeout_s=API_CALL_TIMEOUT_S,
                    step=f"exit_yf_options_fallback:{ticker}",
                )
                od = json.loads(options_json)
                if "error" not in od:
                    options_dict = od
                    mode = "full_chain_fallback"
                    breaker.record_success(f"exit_options:{ticker}")
                else:
                    print(f"[exit-pass] [{ticker}] full chain error: {od.get('error')}")
                    breaker.record_failure(f"exit_options:{ticker}")
            except MasterBotScanError as e:
                print(f"[exit-pass] [{ticker}] full chain isolated: {e.step}: {e.message}")
                breaker.record_failure(f"exit_options:{ticker}")
            except Exception as e:
                print(f"[exit-pass] [{ticker}] full chain error: {e}")
                breaker.record_failure(f"exit_options:{ticker}")

        result["quote_mode_counts"][mode] = (
            result["quote_mode_counts"].get(mode, 0) + 1
        )
        if options_dict is not None:
            scored[ticker] = {
                "options_dict": options_dict,
                "card": None,
                "pivot_data": {},
            }
            if mode == "single_contract":
                breaker.record_success(f"exit_quote:{ticker}")

        if i < len(open_trades) - 1 and inter_ticker_sleep:
            import time as _time
            _time.sleep(inter_ticker_sleep)

    print(
        f"[exit-pass] quote modes: single={result['quote_mode_counts'].get('single_contract', 0)} "
        f"fallback={result['quote_mode_counts'].get('full_chain_fallback', 0)} "
        f"failed={result['quote_mode_counts'].get('failed', 0)}"
    )

    try:
        exit_summary = position_exits.run_scan_exits(
            open_trades,
            scored,
            scan_id=scan_id,
            now_cdt=_chicago_now(),
        )
        result["closed"] = [
            {
                "ticker": c.get("ticker"),
                "reason": c.get("reason"),
                "exit_price": c.get("exit_price"),
                "pnl": c.get("pnl"),
            }
            for c in (exit_summary.get("closed") or [])
        ]
        result["marks_recorded"] = exit_summary.get("marks_recorded")
        result["positions_checked"] = exit_summary.get("positions_checked")
        result["marks_ok"] = exit_summary.get("marks_ok")
        result["marks_failed"] = exit_summary.get("marks_failed")
        result["eod_triggered"] = exit_summary.get("eod_triggered")
        result["open_after"] = exit_summary.get("open_after")
        # Always log one-line mark health (also printed inside run_scan_exits)
        print(
            f"[exit-pass] {clock} CDT marks checked="
            f"{exit_summary.get('positions_checked')} "
            f"ok={exit_summary.get('marks_ok')} "
            f"failed={exit_summary.get('marks_failed')}"
        )
        if exit_summary.get("closed"):
            try:
                lines = [
                    position_exits.format_closed_discord_line(c)
                    for c in exit_summary["closed"]
                ]
                broadcaster.send_discord_alert(
                    f"📉 **EXIT PASS [{clock} CDT]**\n" + "\n".join(lines)
                )
            except Exception as disc_err:
                print(f"[exit-pass] Discord warn: {disc_err}")
    except Exception as e:
        print(f"[exit-pass] exits error: {e}")
        result["error"] = str(e)

    # Keep gate concurrent book aligned (no admits on this path)
    try:
        gate = signal_gate.get_gate()
        remaining = [
            t.get("ticker") for t in load_active_trades() if t.get("ticker")
        ]
        gate.sync_open_from_book(remaining)
        print(f"[exit-pass] gate synced open={remaining}")
    except Exception as se:
        print(f"[exit-pass] gate sync warn: {se}")

    print(
        f"✅ EXIT-ONLY {scan_id} complete | closed={len(result.get('closed') or [])} "
        f"open {result.get('open_before')}→{result.get('open_after')}"
    )
    return result


# Backward-compatible alias used by older call sites / docs
def run_midday_delta_scan(*args, **kwargs):
    """Alias → run_thirty_min_scan (30-min radar; no midday macro header)."""
    return run_thirty_min_scan(*args, **kwargs)


def run_midday_meeting_gemini(*args, **kwargs):
    """Deprecated name — maps to isolated midday macro meeting builder."""
    return run_midday_macro_meeting(
        morning_excerpt=kwargs.get("morning_excerpt") or (args[1] if len(args) > 1 else ""),
        rows=kwargs.get("rows"),
        timeout_s=kwargs.get("timeout_s", LLM_TIMEOUT_S),
    )
