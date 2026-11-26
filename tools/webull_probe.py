"""Read-only sandbox probe. No orders.

Writes a redacted report to logs/webull_sandbox_probe.txt.
Does not print credentials or full account ids.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import session_clock  # noqa: E402
from webull_exec.broker import BrokerError, SdkBroker, scrub  # noqa: E402
from webull_exec.envfile import load_dotenv  # noqa: E402

OUT = ROOT / "logs" / "webull_sandbox_probe.txt"


def _line(parts: list[str], text: str) -> None:
    parts.append(text)


def _classify(status: int | None, bid, ask, quote_time) -> str:
    if status == 403:
        return "not_entitled"
    if bid is None and ask is None:
        return "quote_missing"
    lag = _lag_seconds(quote_time)
    if lag is None:
        return "quote_present_time_unknown"
    if lag >= 20 * 60:
        return "stale_or_session_closed"
    return "fresh"


def _lag_seconds(quote_time) -> float | None:
    try:
        if quote_time is None or quote_time == "":
            return None
        stamp = float(quote_time)
    except (TypeError, ValueError):
        return None
    if stamp > 10_000_000_000:
        stamp = stamp / 1000.0
    return datetime.now(timezone.utc).timestamp() - stamp


def _pick_contract(rows: list[dict], spot: float | None, not_before: str) -> dict | None:
    parsed = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or raw.get("option_symbol") or "")
        strike = raw.get("strike_price") or raw.get("strikePrice") or raw.get("strike")
        expiration = str(
            raw.get("expire_date")
            or raw.get("expiration_date")
            or raw.get("option_expire_date")
            or raw.get("expiration")
            or ""
        )[:10]
        option_type = str(raw.get("option_type") or raw.get("optionType") or raw.get("call_put") or "")
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        if len(expiration) != 10 or not symbol:
            continue
        parsed.append(
            {
                "symbol": symbol,
                "strike": strike_f,
                "expiration": expiration,
                "option_type": option_type.upper() or "CALL",
            }
        )
    calls = [row for row in parsed if row["option_type"].startswith("C")] or parsed
    calls = [row for row in calls if row["expiration"] > not_before]
    if not calls:
        return None
    calls.sort(key=lambda row: (row["expiration"], abs((spot or row["strike"]) - row["strike"])))
    return calls[0]


def _yahoo(expiration: str, strike: float, option_type: str) -> str:
    code = (
        "import json\n"
        "from data_engineer import fetch_contract_quote\n"
        f"print(fetch_contract_quote('SPY', {expiration!r}, {strike!r}, {option_type!r}))\n"
    )
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-c", code],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    return (proc.stdout or "") + ("\n" + proc.stderr if proc.returncode else "")


def _yahoo_bid_ask(blob: str) -> tuple[str, str]:
    start = blob.find("{")
    if start < 0:
        return "unavailable", "yahoo returned no JSON"
    try:
        payload = json.loads(blob[start:])
    except json.JSONDecodeError:
        return "unavailable", "yahoo JSON did not parse"
    if payload.get("error"):
        return "unavailable", str(payload.get("error"))[:180]
    chains = payload.get("chains") or {}
    bid = ask = None
    for chain in chains.values():
        if not isinstance(chain, dict):
            continue
        for side in ("calls", "puts"):
            rows = chain.get(side) or []
            if rows and isinstance(rows[0], dict):
                bid = rows[0].get("bid")
                ask = rows[0].get("ask")
    if bid is None and ask is None:
        return "unavailable", "yahoo chain had no bid or ask"
    return "ok", f"bid={bid} ask={ask}"


def main() -> int:
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    key = (os.environ.get("WEBULL_SANDBOX_APP_KEY") or "").strip()
    secret = (os.environ.get("WEBULL_SANDBOX_APP_SECRET") or "").strip()
    lines: list[str] = []
    _line(lines, f"probe_at={datetime.now(timezone.utc).replace(microsecond=0).isoformat()}")
    _line(lines, "host=api.sandbox.webull.com")
    _line(lines, "orders=none")
    if not key or not secret:
        missing = []
        if not key:
            missing.append("WEBULL_SANDBOX_APP_KEY")
        if not secret:
            missing.append("WEBULL_SANDBOX_APP_SECRET")
        _line(lines, "auth=skipped")
        _line(lines, "missing=" + ",".join(missing))
        _write(lines)
        print("\n".join(lines))
        return 2
    broker = SdkBroker(key, secret, sandbox=True)
    started = time.monotonic()
    try:
        broker.connect()
    except BrokerError as exc:
        _line(lines, f"auth=fail status={exc.status}")
        _line(lines, "error=" + scrub(str(exc)))
        _write(lines)
        print("\n".join(lines))
        return 1
    _line(lines, f"auth=ok account={broker.account_id and '…' + broker.account_id[-4:]}")
    _line(lines, f"auth_seconds={time.monotonic() - started:.2f}")
    try:
        accounts = broker.list_accounts()
    except BrokerError as exc:
        accounts = []
        _line(lines, f"accounts=fail status={exc.status} {scrub(str(exc))}")
    else:
        _line(lines, f"accounts={len(accounts)}")
        for row in accounts:
            _line(
                lines,
                "account "
                f"id={row['masked']} type={row['account_type'] or 'unknown'} "
                f"class={row.get('account_class') or 'unknown'} "
                f"label={row.get('account_label') or 'unknown'} "
                f"options={row['options']}",
            )
    _line(lines, "selection=INDIVIDUAL_MARGIN when the list has no options flag")
    for row in accounts:
        try:
            balances = broker.balances(row["account_id"])
            _line(lines, f"balances {row['masked']} " + json.dumps(balances, default=str)[:400])
        except BrokerError as exc:
            _line(lines, f"balances {row['masked']} fail status={exc.status} {scrub(str(exc))}")
        try:
            positions = broker.positions(row["account_id"])
            _line(lines, f"positions {row['masked']} n={len(positions)}")
            for pos in positions[:10]:
                _line(
                    lines,
                    "position "
                    f"{pos.get('ticker')} {pos.get('expiration')} {pos.get('option_type')} "
                    f"{pos.get('strike')} x{pos.get('qty')}",
                )
        except BrokerError as exc:
            _line(lines, f"positions {row['masked']} fail status={exc.status} {scrub(str(exc))}")
    spot = _spot()
    _line(lines, f"spy_spot={spot if spot is not None else 'unavailable'}")
    try:
        band = {}
        if spot is not None:
            band = {"strike_gte": round(spot - 5, 2), "strike_lte": round(spot + 5, 2)}
        rows = broker.list_option_contracts("SPY", end_date="2026-12-19", **band)
        if not rows:
            rows = broker.list_option_contracts("SPY", **band)
        chosen = _pick_contract(rows, spot, session_clock.chicago_now().date().isoformat())
        _line(lines, f"spy_contracts_seen={len(rows)}")
        if chosen is None:
            _line(lines, "spy_contract=none")
        else:
            _line(
                lines,
                "spy_contract "
                f"{chosen['symbol']} exp={chosen['expiration']} "
                f"{chosen['option_type']} strike={chosen['strike']}",
            )
            _quote(lines, broker, chosen)
    except BrokerError as exc:
        _line(lines, f"spy_chain=fail status={exc.status} {scrub(str(exc))}")
    _write(lines)
    print("\n".join(lines))
    return 0


def _spot() -> float | None:
    code = (
        "import json\n"
        "from data_engineer import _yahoo_ticker, _spot_or_na\n"
        "print(json.dumps({'spot': _spot_or_na(_yahoo_ticker('SPY'))}))\n"
    )
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-c", code],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    start = (proc.stdout or "").find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(proc.stdout[start:])
        spot = float(payload.get("spot"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if spot != spot or spot <= 0:  # NaN
        return None
    return spot


def _quote(lines: list[str], broker: SdkBroker, chosen: dict) -> None:
    started = time.monotonic()
    try:
        snap = broker.snapshot(chosen["symbol"])
    except BrokerError as exc:
        _line(lines, f"quote=fail status={exc.status} class={_classify(exc.status, None, None, None)}")
        _line(lines, "quote_error=" + scrub(str(exc)))
        return
    elapsed = time.monotonic() - started
    lag = _lag_seconds(snap.get("quote_time"))
    kind = _classify(snap.get("status"), snap.get("bid"), snap.get("ask"), snap.get("quote_time"))
    lag_text = "none" if lag is None else f"{lag:.0f}"
    _line(
        lines,
        "quote "
        f"class={kind} status={snap.get('status')} bid={snap.get('bid')} ask={snap.get('ask')} "
        f"lag_seconds={lag_text} http_seconds={elapsed:.2f}",
    )
    yahoo = _yahoo(chosen["expiration"], chosen["strike"], chosen["option_type"])
    state, detail = _yahoo_bid_ask(yahoo)
    _line(lines, f"yahoo={state} {detail}")
    _line(lines, "yahoo_note=cash session may already be closed; a print can be the close")


def _write(lines: list[str]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
