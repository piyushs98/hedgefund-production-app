"""
math_agent.py — Mathematician Agent (Employee Tier).

NOTE: This module is imported by master_bot.py (`calculate_swing_targets`)
but was missing from the codebase snapshot — a missing file here crashes
the entire orchestrator at import time. This is a clean reconstruction of
the expected contract: take the Data Engineer's options JSON, derive an
ATM entry premium, and embed `swing_targets` (entry, 20% stop-loss,
50% take-profit) back into the JSON string.
"""

import json


def calculate_swing_targets(options_json):
    """Returns the options JSON string with a `swing_targets` block added.
    Never raises — on any failure it returns the input augmented with a
    null-target block so downstream consumers keep working."""
    try:
        data = json.loads(options_json)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"error": "math_agent received unparseable options JSON",
                           "swing_targets": None})

    if "error" in data:
        data["swing_targets"] = None
        return json.dumps(data, indent=2)

    spot = data.get("current_price")
    chains = data.get("chains", {})
    entry = None

    if isinstance(spot, (int, float)) and chains:
        # Nearest expiration, ATM call closest to spot with a real market
        first_exp = sorted(chains.keys())[0]
        calls = chains.get(first_exp, {}).get("calls", [])
        candidates = [
            c for c in calls
            if (c.get("strike") or 0) > 0 and (c.get("bid") or 0) > 0 and (c.get("ask") or 0) > 0
        ]
        if candidates:
            atm = min(candidates, key=lambda c: abs(c["strike"] - spot))
            entry = round((atm["bid"] + atm["ask"]) / 2.0, 2)
            data["swing_targets"] = {
                "reference_expiration": first_exp,
                "reference_strike": atm["strike"],
                "entry_premium": entry,
                "stop_loss": round(entry * 0.80, 2),    # 20% stop-loss
                "take_profit": round(entry * 1.50, 2),  # 50% take-profit
            }

    if entry is None:
        data["swing_targets"] = None

    return json.dumps(data, indent=2)


if __name__ == "__main__":
    mock = json.dumps({
        "ticker": "TEST", "current_price": 100.0,
        "chains": {"2026-07-17": {"calls": [
            {"strike": 100, "bid": 2.4, "ask": 2.6, "volume": 500, "openInterest": 1200, "impliedVolatility": 0.31},
            {"strike": 105, "bid": 0.9, "ask": 1.1, "volume": 300, "openInterest": 800, "impliedVolatility": 0.29},
        ], "puts": []}}
    })
    print(calculate_swing_targets(mock))
