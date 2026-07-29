"""
midday_delta.py — Cost-aware intraday book updates.

Architecture split:
  * Pre-market (once/day): Gemini CoS briefing in pre_market_meeting.py
  * Midday / 30-min cadence:
      1. Deterministic fetch → score → delta vs morning baseline
         (scoring accuracy unchanged — no LLM in scoring path)
      2. ONE Gemini-primary midday meeting (what changed, short Discord)
      3. DeepSeek-primary slim trade notes on EXECUTE / material deltas

Gemini free-tier is reserved for meetings; DeepSeek handles trade-path text.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

import config
import broadcaster
import llm_chain
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
LLM_TIMEOUT_S = int(os.environ.get("LLM_CALL_TIMEOUT_S", "20"))


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
    Deterministic Discord payload — used when Gemini midday meeting fails.

    No ES/NQ essay, no per-ticker novels.
    """
    lines = [
        f"**📊 MIDDAY DELTA** `{scan_id}`",
        "_Deterministic score vs morning baseline_",
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


def run_midday_meeting_gemini(
    scan_id: str,
    morning_excerpt: str,
    open_baseline: bool,
    rows: list[dict],
    trades: list[dict],
    *,
    timeout_s: int = LLM_TIMEOUT_S,
) -> str:
    """
    ONE Gemini-primary call: short Discord midday meeting (what changed only).

    DeepSeek is automatic backup if Gemini fails. On total LLM failure, falls
    back to format_delta_discord (deterministic).
    """
    brief = " ".join((morning_excerpt or "").split())[:400]
    table_lines = []
    for r in rows:
        ch = ", ".join(r.get("changes") or []) or "—"
        table_lines.append(
            f"{r.get('ticker')} | {r.get('action_flag')} | "
            f"{r.get('total_score')}/100 | spot {r.get('spot')} | "
            f"pivot {r.get('pivot')} | Δ: {ch}"
        )
    table = "\n".join(table_lines) if table_lines else "(no tickers)"
    trade_bits = []
    for t in trades or []:
        trade_bits.append(
            f"{t.get('ticker')} {t.get('direction')} {t.get('strike')} "
            f"exp {t.get('expiration')} @ ${t.get('entry_premium')}"
        )
    trades_block = "; ".join(trade_bits) if trade_bits else "none"

    system = (
        "You are the hedge fund midday meeting facilitator. Write a tight Discord "
        "update. Only mention what changed vs the morning baseline. Do NOT reprint "
        "ES/NQ futures gap essays. No filler."
    )
    prompt = f"""Scan id: {scan_id}
Open baseline this scan: {open_baseline}

Morning brief excerpt (≤400 chars, context only — do not paste wholesale):
{brief or "(none on file)"}

Ticker table (one line each: ticker | flag | score | spot | pivot | deltas):
{table}

Paper EXECUTEs this pass: {trades_block}

Write a short Discord midday meeting (MAX 800 characters):
- Lead with **📊 MIDDAY MEETING** and the scan id
- Only material changes / EXECUTE flags; say "stable" if nothing moved
- No ES=F / NQ=F / overnight gap reprint
- Keep bullets tight; no long essays
"""
    print(
        f"[midday] 📋 Midday meeting LLM (Gemini primary → DeepSeek backup, "
        f"model={llm_chain.GEMINI_MODEL})..."
    )
    try:
        text = llm_chain.generate_text(
            prompt,
            primary="gemini",
            step="midday_meeting",
            system=system,
            timeout_s=timeout_s,
        )
        text = (text or "").strip()
        if not text:
            raise RuntimeError("empty midday meeting text")
        if len(text) > 800:
            text = text[:780] + "\n…(truncated)"
        return text
    except Exception as exc:
        print(f"[midday] Midday meeting LLM failed ({exc}); deterministic digest.")
        return format_delta_discord(
            scan_id, morning_excerpt, open_baseline, rows, trades
        )


def run_trade_delta_note(
    ticker: str,
    *,
    spot=None,
    pivot=None,
    score=None,
    action_flag=None,
    changes=None,
    contract=None,
    card=None,
    timeout_s: int = LLM_TIMEOUT_S,
) -> str:
    """
    Slim DeepSeek-primary trade note for EXECUTE or material delta tickers.

    Metrics only — no full morning brief, no full options chain. Max ~150 words.
    On failure: format_ceo_deterministic when card is available.
    """
    # Import inside function to avoid circular imports at module load.
    contract_json = ""
    if contract and isinstance(contract, dict) and "error" not in contract:
        slim = {
            k: contract.get(k)
            for k in (
                "direction",
                "strike",
                "expiration",
                "entry_premium",
                "stop_loss",
                "take_profit",
                "implied_volatility",
                "bid_ask_spread_pct",
            )
            if contract.get(k) is not None
        }
        contract_json = json.dumps(slim, separators=(",", ":"))
    else:
        contract_json = "none"

    delta_list = ", ".join(changes or []) or "none"
    prompt = f"""Ticker {ticker} midday trade note. Metrics only:
spot={spot} pivot={pivot} score={score}/100 flag={action_flag}
deltas: {delta_list}
contract: {contract_json}

Write ≤150 words for Discord: decisive CEO-style note with the numbers above.
No morning brief, no ES/NQ gap, no full options chain.
"""
    print(f"[{ticker}] 👑 Trade delta note (DeepSeek primary)...")
    try:
        text = llm_chain.generate_text(
            prompt,
            primary="deepseek",
            step=f"trade_delta:{ticker}",
            timeout_s=timeout_s,
        )
        text = (text or "").strip()
        if not text:
            raise RuntimeError("empty trade note")
        # Soft cap ~150 words / webhook safety
        words = text.split()
        if len(words) > 160:
            text = " ".join(words[:150]) + "…"
        return text
    except Exception as exc:
        print(f"[{ticker}] Trade delta LLM failed ({exc}); deterministic CEO.")
        if card is not None:
            from master_bot import format_ceo_deterministic

            return format_ceo_deterministic(
                card, contract, include_session_open_context=False
            )
        return (
            f"### {ticker} - {action_flag}\n"
            f"* Spot {spot} vs pivot {pivot}; score {score}/100; "
            f"deltas: {delta_list}."
        )


def run_midday_delta_scan(
    breaker: CircuitBreaker,
    *,
    tickers=None,
    inter_ticker_sleep: float = 5,
    morning_macro_context: str = "",
):
    """
    Lightweight book pass: market data + deterministic scoring + delta vs baseline,
    then Gemini midday meeting + DeepSeek trade notes on material/EXECUTE rows.

    Scoring path remains fully deterministic (no LLM).
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
        "llm": True,
        "llm_policy": "gemini_meeting+deepseek_trades",
        "tickers_scanned": universe,
        "results": [],
        "trades": [],
        "discord_delivered": None,
        "meeting_delivered": None,
        "trade_notes": [],
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
        f"open_baseline={open_baseline} | "
        f"LLM=Gemini-meeting+DeepSeek-trades | ES=F {futures_pct}%"
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
            # Score-critical macro tags without LLM
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

            # In-memory row for meeting + trade notes (card not serialized)
            delta_rows.append({
                "ticker": ticker,
                "action_flag": card.action_flag,
                "total_score": card.total_score,
                "spot": snap.get("spot"),
                "pivot": snap.get("pivot"),
                "pct_change": snap.get("pct_change"),
                "changes": changes,
                "contract": contract,
                "card": card,
            })

            try:
                telemetry.log_scan_result(
                    scan_id,
                    card,
                    adversarial_result=adv,
                    selected_contract=contract,
                    agent_params={
                        "mode": "midday_delta",
                        "llm_policy": "gemini_meeting+deepseek_trades",
                    },
                )
            except Exception:
                pass

            row.update({
                "action_flag": card.action_flag,
                "total_score": card.total_score,
                "changes": changes,
            })
            result["results"].append(row)

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

    morning_text = baseline.get("morning_briefing") or morning_macro_context or ""

    # --- Gemini-primary midday meeting (one call) ---
    meeting_text = run_midday_meeting_gemini(
        scan_id,
        morning_text,
        open_baseline,
        delta_rows,
        result["trades"],
    )
    ok_meeting = broadcaster.send_discord_alert(meeting_text)
    result["meeting_delivered"] = bool(ok_meeting)
    result["discord_delivered"] = bool(ok_meeting)
    print(f"[midday] Meeting Discord delivered={ok_meeting}")

    # --- DeepSeek-primary trade notes: EXECUTE or material changes ---
    for r in delta_rows:
        is_execute = r.get("action_flag") == "EXECUTE"
        is_material = bool(r.get("changes"))
        if not (is_execute or is_material):
            continue
        # Skip pure "NEW baseline" noise on open baseline for non-EXECUTE
        if (
            not is_execute
            and open_baseline
            and r.get("changes")
            and all(str(c).startswith("NEW baseline") for c in r["changes"])
        ):
            continue

        note = run_trade_delta_note(
            r["ticker"],
            spot=r.get("spot"),
            pivot=r.get("pivot"),
            score=r.get("total_score"),
            action_flag=r.get("action_flag"),
            changes=r.get("changes"),
            contract=r.get("contract"),
            card=r.get("card"),
        )
        label = "EXECUTE" if is_execute else "DELTA"
        ok_note = broadcaster.send_discord_alert(
            f"**{label} trade note (DeepSeek path)**\n{note}"
        )
        result["trade_notes"].append({
            "ticker": r["ticker"],
            "delivered": bool(ok_note),
            "action_flag": r.get("action_flag"),
        })

    print(
        f"✅ MIDDAY DELTA {scan_id} complete | meeting={ok_meeting} | "
        f"trade_notes={len(result['trade_notes'])}"
    )
    return result
