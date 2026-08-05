"""
midday_delta.py — Intraday book scans + isolated midday macro meeting.

Architecture:
  * Pre-market (once/day, ~09:15 ET): Gemini CoS in pre_market_meeting.py
  * Every ~30 min (RTH): deterministic score + SINGLE Discord table payload
    (DeepSeek only for short KEY TELEMETRY bullets; no macro headers)
  * Midday macro meeting: Gemini once/day STRICTLY 11:00 AM CDT window
    (DeepSeek backup) — never mixed into 30-min scan messages

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
3. When the payload includes "ext" (e.g. ext=$0.51 (2.8%)), copy it verbatim after Entry/SL/TP.
   That is extrinsic premium — do not invent or omit it on EXECUTE rows.
4. When "delta" is present, append δ=0.XX. Never invent delta.
5. MAX 32 words after the Score clause on EXECUTE rows; 25 on PASS/change rows.
6. BANNED phrases (never use any of these or close variants):
   CEO, CEO says, No deltas to manage, No hesitation, We move, Let's work,
   Disciplined execution, No fluff, Load the position.
7. No morning brief, no ES/NQ futures essays, no MIDDAY MEETING headers, no table.
8. Output ONLY the bullet list. No preamble or closing.
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


def macro_vector_local(ticker: str) -> str:
    innovation_data = get_innovation_context(ticker, days=7) or ""
    if not innovation_data.strip():
        return "No specific macro or supply-chain catalysts identified for this ticker."
    low = innovation_data.lower()
    if "supply-chain bottlenecks" in low or "bottleneck" in low or "tariff" in low:
        return "SUPPLY_CHAIN_BOTTLENECK: Detected supply-chain / tariff friction (local scan)."
    if "rate cut" in low or "subsidize" in low or "subsidy" in low:
        return "EXPANSIONARY_TAILWIND: Accommodative / subsidy signals (local scan)."
    if "earnings scheduled" in low or "earnings" in low:
        return "EARNINGS_IMMINENT: Earnings-related catalyst in hub (local scan)."
    return "Neutral macroeconomic backdrop. No critical tailwinds or bottlenecks detected."


def adversarial_local(card) -> dict:
    if card.liquidity_score < 20 or card.sentiment_score == 0:
        return {
            "veto_triggered": True,
            "risk_confidence": 0.85,
            "reason": "Local adversarial: weak liquidity or hostile sentiment.",
        }
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


def _fmt_extrinsic(contract: dict | None) -> str:
    """
    Compact moneyness: ext=$0.51 (2.8%). Pure arithmetic from strike/spot/premium.
    Appends δ=… only when the chain already supplied delta (no local BS).
    """
    if not contract or "error" in contract:
        return ""
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
    if ext is None or entry is None:
        return ""
    if ext_pct is None and entry > 0:
        ext_pct = ext / entry * 100.0
    parts = [f"ext=${ext:.2f}"]
    if ext_pct is not None:
        parts[0] = f"ext=${ext:.2f} ({ext_pct:.1f}%)"
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


def deterministic_telemetry_bullet(row: dict) -> str:
    ticker = row.get("ticker")
    spot = _fmt_money(row.get("spot")).replace("$", "")
    pivot = _fmt_money(row.get("pivot")).replace("$", "")
    score = row.get("total_score")
    try:
        score_s = f"{float(score):.1f}"
    except (TypeError, ValueError):
        score_s = str(score)
    rel = ">" if _num(row.get("spot"), 0) >= _num(row.get("pivot"), 0) else "<"
    rationale = "Holding pivot structure." if rel == ">" else "Trading below pivot; caution."
    if row.get("action_flag") == "EXECUTE" and row.get("contract"):
        c = dict(row["contract"])
        # Prefer row spot when contract.spot missing
        if c.get("spot") is None and row.get("spot") is not None:
            c["spot"] = row.get("spot")
        ext_s = _fmt_extrinsic(c)
        ext_bit = f" {ext_s}" if ext_s else ""
        rationale = (
            f"Setup {_fmt_contract_cell(c)} entry {_fmt_money(c.get('entry_premium'))} "
            f"SL {_fmt_money(c.get('stop_loss'))} TP {_fmt_money(c.get('take_profit'))}"
            f"{ext_bit}."
        )
    bullet = (
        f"- **{ticker}**: Spot ${spot} {rel} Pivot ${pivot}. "
        f"Score: {score_s}/100. {rationale}"
    )
    # EXECUTE lines carry ext=…; allow a few more words so it is not truncated.
    max_words = 32 if row.get("action_flag") == "EXECUTE" else 25
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
        "| Ticker | Status | Contract | Entry | SL | TP |",
        "|---|---|---|---|---|---|",
    ]
    for ticker in universe:
        r = rows_by_ticker.get(ticker) or {}
        status = r.get("action_flag") or ("ERR" if r.get("error") else "PASS")
        contract = r.get("contract")
        if status == "EXECUTE" and contract and "error" not in contract:
            c_cell = _fmt_contract_cell(contract)
            entry = _fmt_money(contract.get("entry_premium"))
            sl = _fmt_money(contract.get("stop_loss"))
            tp = _fmt_money(contract.get("take_profit"))
        else:
            c_cell, entry, sl, tp = "-", "-", "-", "-"
        lines.append(f"| {ticker} | {status} | {c_cell} | {entry} | {sl} | {tp} |")

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
            ext_label = _fmt_extrinsic(c_for_ext) or None
        payload_lines.append(
            json.dumps(
                {
                    "ticker": r.get("ticker"),
                    "status": r.get("action_flag"),
                    "spot": r.get("spot"),
                    "pivot": r.get("pivot"),
                    "score": r.get("total_score"),
                    "changes": r.get("changes") or [],
                    "contract": _fmt_contract_cell(c) if c else None,
                    "entry": c.get("entry_premium") if c else None,
                    "sl": c.get("stop_loss") if c else None,
                    "tp": c.get("take_profit") if c else None,
                    "ext": ext_label,
                    "extrinsic": c.get("extrinsic") if c else None,
                    "extrinsic_pct": c.get("extrinsic_pct") if c else None,
                    "delta": c.get("delta") if c else None,
                },
                separators=(",", ":"),
            )
        )

    user = (
        "Produce KEY TICKER TELEMETRY bullets for these rows only:\n"
        + "\n".join(payload_lines)
        + "\n\nOne bullet per ticker. ≤25 words after Score. Banned filler enforced."
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
    timeout_s: int = LLM_TIMEOUT_S,
) -> str:
    """
    Comprehensive Midday Macro & News Update — Gemini primary, DeepSeek backup.

    Called ONLY from master_bot inside the 11:00 AM CDT window (once/day).
    Never used as a prefix on 30-minute scan payloads.
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
    print(
        f"[midday] 📋 Midday MACRO meeting (Gemini primary → DeepSeek backup, "
        f"model={llm_chain.GEMINI_MODEL}) — isolated 11:00 CDT slot only"
    )
    try:
        text = llm_chain.generate_text(
            prompt,
            primary="gemini",
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
        print(f"[midday] Macro meeting LLM failed ({exc}); short deterministic note.")
        return (
            "**📊 MIDDAY MACRO & NEWS UPDATE (11:00 CDT)**\n"
            f"Morning brief on file ({len(brief)} chars). "
            f"Book rows: {len(rows or [])}. "
            "LLM offline — see latest 30-MIN SCAN table for structure."
        )


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
        get_latest_futures_pct,
        ensure_news_context,
        record_executed_trade,
        MasterBotScanError,
        _call_with_timeout,
        API_CALL_TIMEOUT_S,
    )

    universe = list(tickers) if tickers is not None else list(TICKERS)
    scan_id = f"scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
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
    weights = config.load_weights()
    futures_pct = get_latest_futures_pct("ES=F")
    clock = cdt_clock_str()
    print(
        f"\n🚀 30-MIN SCAN {scan_id} | {clock} CDT | tickers={len(universe)} | "
        f"open_baseline={open_baseline} | ES=F {futures_pct}%"
    )

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

            card = scoring_engine.score_ticker(
                ticker,
                options_dict,
                pivot_data,
                news_string,
                macro_vector=macro_vector,
                futures_pct=futures_pct,
                atr_pct=atr_pct,
                weights=weights,
            )
            print(
                f"[{ticker}] ⚙️ score "
                f"{card.total_score}/100 → {card.action_flag}"
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

    # ---- Stage 4 exits (B1–B4): before gate so freed slots are available ----
    # Reuses phase-1 options_dict marks — no extra yfinance when open ⊆ universe.
    exit_summary: dict[str, Any] = {}
    try:
        import position_exits
        from tracker_agent import load_active_trades as _load_open

        open_before_exits = _load_open()
        if open_before_exits:
            print(
                f"[scan] Stage 4 exits: evaluating {len(open_before_exits)} open "
                f"position(s) (EOD/expiry/SL/TP + marks)"
            )
            exit_summary = position_exits.run_scan_exits(
                open_before_exits,
                scored,
                scan_id=scan_id,
                now_cdt=_chicago_now(),
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
                        f"**{c.get('ticker')}** {c.get('reason')} "
                        f"@ ${c.get('exit_price')}"
                        + (f" PnL ${c.get('pnl'):.0f}" if c.get("pnl") is not None else "")
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

    # ---- Stage 3 gate: sync durable book (post-exit), rank-before-admit ----
    gate = signal_gate.get_gate()
    try:
        from tracker_agent import load_active_trades
        open_tickers = [
            t.get("ticker") for t in load_active_trades() if t.get("ticker")
        ]
        gate.sync_open_from_book(open_tickers)
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
            )
        )

    now_utc = datetime.now(timezone.utc)
    gate_decisions = gate.process_scan(observations, now_utc)
    gate_by_ticker = {d.ticker: d for d in gate_decisions}
    gate_summary = gate.format_scan_summary(gate_decisions)
    print(f"[scan] {gate_summary}")
    result["gate_summary"] = gate_summary

    # ---- Phase 2: strike + paper buy only for admitted tickers ----
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
                    options_dict, pivot_data, atr_abs=atr_abs
                )
                if "error" in contract:
                    card.action_flag = "PASS"
                    card.reasons.append(contract["error"])
                    # Free the slot we just reserved — strike failed
                    try:
                        gate.on_close(ticker)
                    except Exception:
                        pass
                    contract = None
                else:
                    try:
                        buy_payload = dict(contract)
                        buy_payload.setdefault("ticker", ticker)
                        virtual_broker.paper_buy(buy_payload, contract.get("entry_premium"))
                    except Exception as be:
                        print(f"[{ticker}] paper_buy warn: {be}")
                    try:
                        record_executed_trade(ticker, contract, scan_id=scan_id, card=card)
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
            "gate_reason": None if gdec is None else gdec.reason,
            "gate_admit": None if gdec is None else gdec.admit,
        }
        rows_by_ticker[ticker] = row
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
    print(f"✅ 30-MIN SCAN {scan_id} complete | discord={ok} | telemetry={len(bullets)}")
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
