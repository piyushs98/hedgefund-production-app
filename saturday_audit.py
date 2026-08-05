"""
saturday_audit.py — Weekly performance audit (Stage 4 B6).

Reads CLOSED paper trades from virtual_broker.trade_history
(filters out PAPER_BUY_OPEN markers). Groups by EXIT: notes when present.
Never substitutes mock/sample trades.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any

import broadcaster
import config
import llm_chain

DB_PATH = config.NEWS_DB_PATH
LLM_TIMEOUT_S = int(os.environ.get("LLM_CALL_TIMEOUT_S", "40"))
MIN_AUDIT_TRADES = int(os.environ.get("MIN_AUDIT_TRADES", "3"))


def _is_open_marker(notes: Any) -> bool:
    return str(notes or "").strip() == "PAPER_BUY_OPEN"


def _exit_reason(notes: Any) -> str:
    text = str(notes or "").strip()
    if text.startswith("EXIT:"):
        return text[5:].strip() or "UNKNOWN"
    if not text:
        return "UNLABELED"
    return text


def _load_closed_trades(db_path: str) -> list[dict[str, Any]]:
    """Closed rows only: not PAPER_BUY_OPEN open markers."""
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        # Table may not exist yet on a fresh deploy
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_history'"
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            """
            SELECT closed_at, ticker, direction, strike, expiration,
                   entry_price, exit_price, pnl, notes, slippage, contract_json
            FROM trade_history
            ORDER BY closed_at ASC
            """
        ).fetchall()
    closed = []
    for r in rows:
        if _is_open_marker(r["notes"]):
            continue
        closed.append(dict(r))
    return closed


def run_saturday_audit():
    """
    Saturday Performance Audit Loop
    Reads trade_history closed rows and synthesizes portfolio metrics.
    Outputs a weight adjustment JSON for the upcoming week.
    """
    print("📊 Initiating Saturday Performance Audit Loop...")
    try:
        closed = _load_closed_trades(DB_PATH)
        n = len(closed)
        if n < MIN_AUDIT_TRADES:
            report = (
                f"# 📊 Saturday Performance Audit\n"
                f"**insufficient data: {n} closed trades**\n\n"
                f"Need at least {MIN_AUDIT_TRADES} closed rows in `trade_history` "
                f"(excluding PAPER_BUY_OPEN markers) before win rate or PnL "
                f"will be reported.\n"
                f"No sample data was substituted."
            )
            print(report)
            broadcaster.send_discord_alert(report)
            return

        total_trades = n
        pnls = [float(t["pnl"] or 0) for t in closed]
        wins = sum(1 for p in pnls if p > 0)
        win_loss_ratio = wins / total_trades if total_trades else 0.0
        total_pnl = sum(pnls)
        # Duration not stored on trade_history; leave 0 unless contract_json has entry time
        durations = []
        for t in closed:
            try:
                meta = json.loads(t["contract_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            ent = meta.get("entry_timestamp") or meta.get("entry_time")
            if ent and t.get("closed_at"):
                try:
                    a = datetime.fromisoformat(str(ent).replace("Z", "+00:00"))
                    b = datetime.fromisoformat(str(t["closed_at"]).replace("Z", "+00:00"))
                    durations.append(max(0.0, (b - a).total_seconds() / 3600.0))
                except (TypeError, ValueError):
                    pass
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        by_reason = Counter(_exit_reason(t.get("notes")) for t in closed)
        reason_lines = ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items()))

        metrics_payload = {
            "total_weekly_capital_deployed": total_trades * 1000,
            "gross_win_loss_ratio": win_loss_ratio,
            "average_trade_duration_hours": avg_duration,
            "total_pnl": total_pnl,
            "trade_sample_size": total_trades,
            "exit_reasons": dict(by_reason),
        }

        prompt = f"""
You are an advanced meta-optimization instance for a quantitative hedge fund.
Evaluate the following weekly portfolio metrics and determine if the market shifted regimes (e.g., from high-momentum breakout to sideways mean-reverting).

Metrics:
{json.dumps(metrics_payload, indent=2)}

Based on this performance, recommend exact payload updates for the 100-point system weights for the upcoming week.
You must output a strictly formatted JSON object mirroring this EXACT structure:
{{"recommended_weights": {{"liquidity": 30, "technical": 40, "sentiment": 30}}}}

Ensure the total sum equals exactly 100.
Do not output anything other than raw JSON.
"""
        try:
            raw_text = llm_chain.generate_text(
                prompt,
                primary="gemini",
                step="saturday_audit",
                timeout_s=LLM_TIMEOUT_S,
            ).strip()
        except Exception as llm_err:
            print(f"LLM chain failed for Saturday audit ({llm_err}). Using fallback weights.")
            raw_text = ""

        if raw_text.startswith("```json"):
            raw_text = raw_text.replace("```json", "", 1).strip()
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3].strip()

        try:
            weights = json.loads(raw_text) if raw_text else {}
            if "recommended_weights" not in weights:
                raise ValueError("missing recommended_weights")
        except (json.JSONDecodeError, ValueError):
            print("Failed to decode LLM JSON. Using fallback weights.")
            weights = {"recommended_weights": {"liquidity": 30, "technical": 40, "sentiment": 30}}

        try:
            config.save_weights(weights.get("recommended_weights", {}))
        except Exception as w_err:
            print(f"Could not persist recommended weights ({w_err}); engine keeps prior weights.")

        report = f"""# 📊 Saturday Performance Audit
**Closed trades:** {total_trades} (source: trade_history — real data only)
**Total PnL:** ${total_pnl:.2f}
**Win/Loss Ratio:** {win_loss_ratio*100:.1f}%
**Average Duration:** {avg_duration:.1f} hours
**Exit reasons:** {reason_lines}

**Weight Adjustment Output:**
```json
{json.dumps(weights, indent=2)}
```
"""
        print(report)
        broadcaster.send_discord_alert(report)

    except sqlite3.Error as e:
        print(f"SQLite Error in Saturday Audit: {e}")
        try:
            broadcaster.send_discord_alert(
                f"# 📊 Saturday Performance Audit\n"
                f"**insufficient data: audit DB error**\n`{e}`\n"
                f"No sample data was substituted."
            )
        except Exception:
            pass
    except Exception as e:
        print(f"Saturday Audit Error: {e}")


if __name__ == "__main__":
    print("[Saturday Audit] Module loaded successfully.")
