"""
midday_delta.py — Zero-Gemini intraday book updates.

Architecture split:
  * Pre-market (once/day): heavy Gemini CoS briefing in pre_market_meeting.py
  * Midday / 30-min cadence: deterministic fetch → score → delta vs morning baseline

No llm_chain / Gemini calls here. Same scoring_engine + strike_selector as the
full path, so EXECUTE/PASS accuracy is unchanged.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

import config
import broadcaster
import scoring_engine
import strike_selector
import telemetry
import virtual_broker
from circuit_breaker import CircuitBreaker
from data_engineer import fetch_options_data
from math_agent import calculate_swing_targets
from news_memory import get_historical_context, get_innovation_context
from ticker_desk import fetch_pivot_data

# Persist session baseline next to other runtime state
BASELINE_PATH = os.environ.get(
    "SESSION_BASELINE_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "session_baseline.json")),
)

# Material-change thresholds for Discord (keeps noise low)
SCORE_DELTA_MIN = float(os.environ.get("DELTA_SCORE_MIN", "5"))
PCT_CHANGE_DELTA_MIN = float(os.environ.get("DELTA_PCT_MIN", "0.75"))


def macro_vector_local(ticker: str) -> str:
    """Keyword macro vector — no LLM. Mirrors innovation_manager fallback tags."""
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
    """Deterministic Devil's Advocate — same fallback rules as LLM-offline path."""
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
    except Exception as e:
        print(f"[midday] WARNING: could not save baseline: {e}")


def store_morning_briefing(briefing_text: str, session_date: str | None = None) -> dict:
    """Call after pre-market CoS (Gemini) so midday can reference the morning baseline."""
    today = session_date or datetime.now().strftime("%Y-%m-%d")
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


def compute_deltas(ticker: str, prev: dict | None, cur: dict) -> list[str]:
    """Human-readable change bullets; empty if nothing material."""
    if not prev:
        return [f"NEW baseline | score {cur.get('total_score')} → {cur.get('action_flag')}"]

    changes = []
    prev_flag = prev.get("action_flag")
    cur_flag = cur.get("action_flag")
    if prev_flag != cur_flag:
        changes.append(f"FLAG {prev_flag}→{cur_flag}")

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


def format_delta_discord(
    scan_id: str,
    morning_excerpt: str,
    open_baseline: bool,
    rows: list[dict],
    trades: list[dict],
) -> str:
    """
    Minimal Discord payload — no repeated ES/NQ essay, no per-ticker novels.

    Token/API: zero LLM. Webhook chars kept short.
    """
    lines = [
        f"**📊 MIDDAY DELTA** `{scan_id}`",
        "_No Gemini — deterministic score vs morning baseline_",
    ]
    if open_baseline:
        lines.append("_Open baseline established (first book scan of the session)._")
    if morning_excerpt:
        # One short pointer only — not the full morning brief
        one_line = " ".join(morning_excerpt.split())[:180]
        lines.append(f"_Morning brief on file:_ {one_line}…")

    material = [r for r in rows if r.get("changes") or r.get("action_flag") == "EXECUTE"]
    if not material:
        lines.append("No material deltas vs baseline (scores/flags stable).")
    else:
        lines.append("**Changes:**")
        for r in material[:15]:
            ch = ", ".join(r.get("changes") or []) or "EXECUTE watch"
            spot = r.get("spot")
            lines.append(
                f"• **{r['ticker']}** {r.get('action_flag')} "
                f"{r.get('total_score')}/100 | spot {spot} | {ch}"
            )

    if trades:
        lines.append("**Paper EXECUTE:**")
        for t in trades:
            lines.append(
                f"• {t.get('ticker')} {t.get('direction')} {t.get('strike')} "
                f"exp {t.get('expiration')} @ ${t.get('entry_premium')}"
            )

    text = "\n".join(lines)
    # Hard cap webhook size
    if len(text) > 1800:
        text = text[:1750] + "\n…(truncated)"
    return text


def run_midday_delta_scan(
    breaker: CircuitBreaker,
    *,
    tickers=None,
    inter_ticker_sleep: float = 5,
    morning_macro_context: str = "",
):
    """
    Lightweight book pass: market data + deterministic scoring + delta Discord.

    **Zero Gemini / llm_chain calls.**
    """
    from master_bot import (  # local import avoids circular init issues
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
    scan_id = f"delta-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    result = {
        "scan_id": scan_id,
        "mode": "midday_delta",
        "llm": False,
        "tickers_scanned": universe,
        "results": [],
        "trades": [],
        "discord_delivered": None,
        "aborted": False,
    }

    if breaker.is_open():
        print("🛑 [midday] Circuit breaker OPEN — delta scan suspended.")
        result["aborted"] = True
        result["circuit_breaker_open"] = True
        return result

    today = datetime.now().strftime("%Y-%m-%d")
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
    print(
        f"\n🚀 MIDDAY DELTA SCAN {scan_id} | tickers={len(universe)} | "
        f"open_baseline={open_baseline} | Gemini=OFF | ES=F {futures_pct}%"
    )

    new_ticker_state = dict(baseline.get("tickers") or {})
    delta_rows = []

    for idx, ticker in enumerate(universe):
        row = {"ticker": ticker, "error": None}
        try:
            options_json = _call_with_timeout(
                lambda t=ticker: fetch_options_data(t),
                timeout_s=API_CALL_TIMEOUT_S,
                step=f"yf_options:{ticker}",
            )
            options_dict = json.loads(options_json)
            if "error" in options_dict:
                row["error"] = options_dict["error"]
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
            # Score-critical macro tags without Gemini
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
                f"[{ticker}] ⚙️ DELTA score L{card.liquidity_score}+"
                f"T{card.technical_score}+S{card.sentiment_score}="
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

            contract = None
            if card.action_flag == "EXECUTE":
                contract = strike_selector.select_optimal_contract(
                    options_dict, pivot_data, atr_abs=atr_abs
                )
                if "error" in contract:
                    card.action_flag = "PASS"
                    card.reasons.append(contract["error"])
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
                    })

            snap = _card_snapshot(card, atr_abs=atr_abs)
            prev = (baseline.get("tickers") or {}).get(ticker)
            changes = compute_deltas(ticker, prev, snap)
            new_ticker_state[ticker] = snap

            # Discord line for this ticker
            delta_rows.append({
                "ticker": ticker,
                "action_flag": card.action_flag,
                "total_score": card.total_score,
                "spot": snap.get("spot"),
                "changes": changes,
            })

            try:
                telemetry.log_scan_result(
                    scan_id,
                    card,
                    adversarial_result=adv,
                    selected_contract=contract,
                    agent_params={"mode": "midday_delta", "llm": False},
                )
            except Exception:
                pass

            row.update({
                "action_flag": card.action_flag,
                "total_score": card.total_score,
                "changes": changes,
            })
            result["results"].append(row)

            # Optional compact per-EXECUTE CEO-style line (deterministic, no Gemini)
            if card.action_flag == "EXECUTE" and contract:
                from master_bot import format_ceo_deterministic
                msg = format_ceo_deterministic(
                    card, contract, include_session_open_context=False
                )
                broadcaster.send_discord_alert(
                    f"**EXECUTE (delta path, no Gemini)**\n{msg}"
                )

        except MasterBotScanError as e:
            print(f"[{ticker}] isolated: {e.step}: {e.message}")
            row["error"] = f"{e.step}: {e.message}"
            result["results"].append(row)
        except Exception as e:
            print(f"[{ticker}] error: {e}")
            row["error"] = str(e)
            result["results"].append(row)

        if idx < len(universe) - 1 and inter_ticker_sleep:
            import time
            time.sleep(inter_ticker_sleep)

    # Persist updated baseline (rolling "last seen" for next delta)
    baseline["tickers"] = new_ticker_state
    if open_baseline:
        baseline["established_at"] = datetime.utcnow().isoformat() + "Z"
    baseline["last_delta_scan_id"] = scan_id
    baseline["last_delta_at"] = datetime.utcnow().isoformat() + "Z"
    save_baseline(baseline)

    digest = format_delta_discord(
        scan_id,
        baseline.get("morning_briefing") or morning_macro_context or "",
        open_baseline,
        delta_rows,
        result["trades"],
    )
    ok = broadcaster.send_discord_alert(digest)
    result["discord_delivered"] = bool(ok)
    print(f"✅ MIDDAY DELTA {scan_id} complete | discord={ok}")
    return result
