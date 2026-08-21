"""
virtual_broker.py — Virtual SQLite paper ledger for Master Bot EXECUTE trades.

Buying power and realized PnL live in news_room.db (config.NEWS_DB_PATH).
Options use the standard 100x multiplier. Used by master_bot only — not a
web dashboard. Does not touch scoring, AI prompts, or LLM provider routing.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

DB_PATH = config.NEWS_DB_PATH
CONTRACT_MULTIPLIER = 100
# Legacy hardcoded seed — used only to detect old DBs that still hold $100k
# with $0 realized so we can rebase to ACCOUNT_SIZE on first boot.
_LEGACY_LEDGER_SEED = 100_000.0


def starting_buying_power() -> float:
    """Ledger seed = ACCOUNT_SIZE unless STARTING_BUYING_POWER is set."""
    return float(
        getattr(
            config,
            "STARTING_BUYING_POWER",
            getattr(config, "ACCOUNT_SIZE", 10000.0),
        )
    )


# Back-compat alias; prefer starting_buying_power() so tests can patch config.
STARTING_BUYING_POWER = starting_buying_power()

# UI event bus for Server-Sent Events (dashboard only — does not affect trading).
ui_event_queue: queue.Queue = queue.Queue()

# Buy-time slippage stashed until paper_sell writes trade_history (closed-trade log).
# Keyed by a lightweight fingerprint so observability never alters fill math.
_pending_slippage: dict[str, float | None] = {}
_boot_reset_done: bool = False

# In-process session book (peak deployed / day realized). Discord at 14:45 is
# the durable copy — the SQLite ledger does not survive a redeploy.
_book: dict[str, Any] = {
    "session_date": None,
    "start_realized": 0.0,
    "open_cost": 0.0,
    "peak_deployed": 0.0,
}


def get_ui_event() -> dict[str, Any] | None:
    """Non-blocking pull of the next UI event for the SSE stream. Returns None if empty."""
    try:
        return ui_event_queue.get_nowait()
    except queue.Empty:
        return None


def _entry_fingerprint(ticker: Any, direction: Any, entry_price: float) -> str:
    return f"{ticker or '?'}|{direction or '?'}|{entry_price:.6f}"


def _extract_ask(meta: dict[str, Any]) -> float | None:
    """Pull ask from contract root or nested option_contract, if present."""
    for source in (meta, meta.get("option_contract") if isinstance(meta.get("option_contract"), dict) else None):
        if not source:
            continue
        raw = source.get("ask")
        if raw is None:
            continue
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            continue
    return None


def _connect() -> sqlite3.Connection:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _ensure_slippage_column(conn: sqlite3.Connection) -> None:
    """Migrate trade_history to include slippage when upgrading an existing DB."""
    cols = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(trade_history)").fetchall()
    }
    if "slippage" not in cols:
        conn.execute("ALTER TABLE trade_history ADD COLUMN slippage REAL")


def reset_book_for_tests() -> None:
    """Test helper — clear in-process peak-deployed session."""
    _book["session_date"] = None
    _book["start_realized"] = 0.0
    _book["open_cost"] = 0.0
    _book["peak_deployed"] = 0.0


def _chicago_date_str() -> str:
    try:
        import pytz
        return datetime.now(pytz.timezone("America/Chicago")).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def ensure_session_book() -> None:
    """Start-of-Chicago-day snapshot for peak deployed / day realized."""
    day = _chicago_date_str()
    if _book.get("session_date") == day:
        return
    port = get_portfolio()
    open_cost = _deployed_from_open_trades()
    _book["session_date"] = day
    _book["start_realized"] = float(port.get("total_realized_pnl") or 0.0)
    _book["open_cost"] = open_cost
    _book["peak_deployed"] = open_cost


def open_mark_value() -> float:
    """Mark-to-market of open lots: last_mark (else entry) × 100 × qty."""
    try:
        from tracker_agent import load_active_trades
        trades = load_active_trades() or []
    except Exception:
        return 0.0
    total = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        try:
            mark = t.get("last_mark")
            if mark is None:
                mark = t.get("entry_price") or t.get("entry_premium")
            mark = float(mark)
        except (TypeError, ValueError):
            continue
        if mark <= 0:
            continue
        qty = resolve_quantity(t)
        total += mark * CONTRACT_MULTIPLIER * qty
    return total


def _deployed_from_open_trades() -> float:
    try:
        from tracker_agent import load_active_trades
        trades = load_active_trades() or []
    except Exception:
        return float(_book.get("open_cost") or 0.0)
    total = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        try:
            entry = float(t.get("entry_price") or t.get("entry_premium") or 0.0)
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        qty = resolve_quantity(t)
        total += entry * CONTRACT_MULTIPLIER * qty
    return total


def note_session_open(cost: float) -> None:
    ensure_session_book()
    _book["open_cost"] = float(_book.get("open_cost") or 0.0) + float(cost)
    _book["peak_deployed"] = max(
        float(_book.get("peak_deployed") or 0.0),
        float(_book["open_cost"]),
    )


def note_session_close(entry_cost: float) -> None:
    ensure_session_book()
    _book["open_cost"] = max(
        0.0, float(_book.get("open_cost") or 0.0) - float(entry_cost)
    )


def format_book_line() -> str:
    """
    BOOK: start 10,000 | peak deployed X | realized +/-Y | open value Y | equity Z

    equity = buying_power (cash) + open mark value. That is account value.
    """
    ensure_session_book()
    port = get_portfolio()
    start = starting_buying_power()
    peak = float(_book.get("peak_deployed") or 0.0)
    peak = max(peak, _deployed_from_open_trades())
    realized = float(port.get("total_realized_pnl") or 0.0) - float(
        _book.get("start_realized") or 0.0
    )
    bp = float(port.get("buying_power") or 0.0)
    open_val = open_mark_value()
    equity = bp + open_val
    return (
        f"BOOK: start {start:,.0f} | peak deployed {peak:,.0f} | "
        f"realized {realized:+,.0f} | open value {open_val:,.0f} | "
        f"equity {equity:,.0f}"
    )


def ensure_ledger() -> None:
    """
    Create portfolio_ledger + trade_history if missing.
    Seed portfolio_ledger with ACCOUNT_SIZE buying_power / $0 PnL when empty.
    Rebases a legacy $100k / $0-PnL seed to ACCOUNT_SIZE.
    Migrates trade_history.slippage via ALTER TABLE when the column is absent.
    """
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_ledger (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                buying_power REAL NOT NULL,
                total_realized_pnl REAL NOT NULL,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                closed_at TEXT NOT NULL,
                ticker TEXT,
                direction TEXT,
                strike REAL,
                expiration TEXT,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                pnl REAL NOT NULL,
                contract_json TEXT,
                notes TEXT,
                slippage REAL
            )
            """
        )
        _ensure_slippage_column(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM portfolio_ledger"
        ).fetchone()
        seed = starting_buying_power()
        now = datetime.now(timezone.utc).isoformat()
        if row is None or int(row["n"]) == 0:
            conn.execute(
                """
                INSERT INTO portfolio_ledger
                    (id, buying_power, total_realized_pnl, updated_at)
                VALUES (1, ?, 0.0, ?)
                """,
                (seed, now),
            )
        else:
            led = conn.execute(
                "SELECT buying_power, total_realized_pnl FROM portfolio_ledger WHERE id = 1"
            ).fetchone()
            if led is not None:
                bp = float(led["buying_power"])
                realized = float(led["total_realized_pnl"])
                # Empty legacy $100k seed from before ACCOUNT_SIZE was the ceiling.
                if (
                    abs(bp - _LEGACY_LEDGER_SEED) < 0.51
                    and abs(realized) < 0.51
                    and abs(seed - _LEGACY_LEDGER_SEED) > 0.51
                ):
                    conn.execute(
                        """
                        UPDATE portfolio_ledger
                        SET buying_power = ?, updated_at = ?
                        WHERE id = 1
                        """,
                        (seed, now),
                    )
                    print(
                        f"[VirtualBroker] CRITICAL: rebased ledger seed "
                        f"${_LEGACY_LEDGER_SEED:,.0f} → ${seed:,.0f} "
                        f"(ACCOUNT_SIZE; $0 realized, empty book assumed)"
                    )
        conn.commit()


def reset_boot_flag_for_tests() -> None:
    """Test helper — allow reset_ledger_if_requested to fire again."""
    global _boot_reset_done
    _boot_reset_done = False


def _clear_active_trades_json() -> str:
    """Write empty [] to active_trades.json (atomic). Returns the path."""
    try:
        from tracker_agent import ACTIVE_TRADES_PATH
        path = ACTIVE_TRADES_PATH
    except Exception:
        path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "active_trades.json")
        )
    path = os.path.abspath(str(path))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("[]\n")
    os.replace(tmp, path)
    return path


def reset_ledger_now() -> dict[str, Any]:
    """
    Wipe paper book and reseed at ACCOUNT_SIZE. Does not touch hedge_fund.db.

    Truncates portfolio_ledger, trade_history, active_trades_store,
    position_marks; clears active_trades.json; reseeds buying_power.
    """
    seed = starting_buying_power()
    now = datetime.now(timezone.utc).isoformat()
    wiped: list[str] = []
    ensure_ledger()
    with _connect() as conn:
        names = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in (
            "trade_history",
            "active_trades_store",
            "position_marks",
            "portfolio_ledger",
        ):
            if table in names:
                conn.execute(f"DELETE FROM {table}")
                wiped.append(table)
        conn.execute(
            """
            INSERT INTO portfolio_ledger
                (id, buying_power, total_realized_pnl, updated_at)
            VALUES (1, ?, 0.0, ?)
            """,
            (seed, now),
        )
        conn.commit()
    json_path = _clear_active_trades_json()
    reset_book_for_tests()
    _pending_slippage.clear()
    return {
        "ok": True,
        "seed": seed,
        "wiped_tables": wiped,
        "active_trades_json": json_path,
    }


def reset_ledger_if_requested() -> dict[str, Any] | None:
    """
    If RESET_LEDGER_ON_BOOT is true, wipe+reseed once per process.

    Must be called at process start BEFORE preflight / carry / any book read.
    Left true: fires on every boot (Render free-tier restarts included)
    and logs WARNING so a forgotten flag is loud.
    """
    global _boot_reset_done
    flag = bool(getattr(config, "RESET_LEDGER_ON_BOOT", False))
    if not flag:
        return None
    if _boot_reset_done:
        print(
            "[reset] WARNING: RESET_LEDGER_ON_BOOT is true but this process "
            "already wiped once — skipping duplicate."
        )
        return None
    _boot_reset_done = True
    result = reset_ledger_now()
    seed = float(result.get("seed") or 0.0)
    line = (
        f"[reset] ledger wiped, reseeded at ${seed:,.0f}, 0 open positions"
    )
    warn = (
        "[reset] WARNING: RESET_LEDGER_ON_BOOT is true — this fires on "
        "every process start. Set false after you confirm this line."
    )
    print(line)
    print(warn)
    result["line"] = line
    result["warning"] = warn
    try:
        import broadcaster
        broadcaster.send_discord_alert(f"{line}\n{warn}")
    except Exception as e:
        print(f"[reset] Discord warn: {e}")
    return result


def get_portfolio() -> dict[str, float]:
    """Return current buying_power and total_realized_pnl."""
    ensure_ledger()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT buying_power, total_realized_pnl
            FROM portfolio_ledger WHERE id = 1
            """
        ).fetchone()
    if not row:
        return {
            "buying_power": starting_buying_power(),
            "total_realized_pnl": 0.0,
        }
    return {
        "buying_power": float(row["buying_power"]),
        "total_realized_pnl": float(row["total_realized_pnl"]),
    }


def _contract_meta(contract: Any) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    return contract


def resolve_quantity(contract: Any, quantity: int | None = None) -> int:
    """Contracts on a paper fill. Explicit arg wins; else contract fields; else 1.

    Explicit 0 is preserved so a buying-power size-down to nothing can block.
    """
    if quantity is not None:
        try:
            q = int(quantity)
            return q if q >= 0 else 1
        except (TypeError, ValueError):
            return 1
    meta = _contract_meta(contract)
    for key in ("quantity", "qty", "contracts"):
        raw = meta.get(key)
        if raw is None and isinstance(meta.get("option_contract"), dict):
            raw = meta["option_contract"].get(key)
        if raw is None:
            continue
        try:
            q = int(raw)
        except (TypeError, ValueError):
            continue
        if q >= 0:
            return q
    return 1


def size_position(
    entry_price: float | int | None,
    stop_loss: float | int | None,
    *,
    buying_power: float | None = None,
) -> int:
    """
    Risk-based contract count.

      1. if 1-lot risk > RISK_PER_TRADE_DOLLARS * MAX_RISK_BREACH_PCT → 0
         (no min-1 floor that blows the budget; selector walks instead)
      2. qty = floor(account_risk / ((entry - SL) * 100)), min 1 when 1-lot fits
      3. soft cap: min(qty, MAX_CONTRACTS_PER_TRADE)
      4. hard cap: min(qty, floor(buying_power / (entry * 100))) → may be 0

    Buying power is applied last and is the hard constraint: the contract
    cap never forces a fill the ledger cannot debit. paper_buy also
    refuses the open if the debit still exceeds buying_power.
    """
    max_qty = max(1, int(getattr(config, "MAX_CONTRACTS_PER_TRADE", 10)))
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return 1
    if entry <= 0:
        return 1

    account = float(getattr(config, "ACCOUNT_SIZE", 10000.0))
    risk_pct = float(getattr(config, "RISK_PER_TRADE_PCT", 1.5))
    account_risk = account * (risk_pct / 100.0)

    sl = None
    try:
        if stop_loss is not None and stop_loss != "":
            sl = float(stop_loss)
    except (TypeError, ValueError):
        sl = None

    qty = 1
    if sl is not None:
        per_contract_risk = (entry - sl) * CONTRACT_MULTIPLIER
        breach = float(getattr(config, "MAX_RISK_BREACH_PCT", 1.0))
        cap = account_risk * breach
        if per_contract_risk > 0 and cap > 0 and per_contract_risk > cap:
            # 1 lot already exceeds the risk budget — do not floor to qty 1.
            return 0
        if per_contract_risk > 0 and account_risk > 0:
            qty = int(account_risk // per_contract_risk)  # floor
            if qty < 1:
                qty = 1
    qty = min(qty, max_qty)

    if buying_power is not None:
        try:
            bp = float(buying_power)
        except (TypeError, ValueError):
            bp = None
        else:
            cost_one = entry * CONTRACT_MULTIPLIER
            if cost_one > 0:
                affordable = int(bp // cost_one)
                qty = min(qty, max(0, affordable))

    return max(0, int(qty))


def apply_entry_quantity(
    contract: dict[str, Any],
    *,
    buying_power: float | None = None,
) -> int:
    """Stamp contract['quantity'] from the risk formula. 0 = cannot afford.

    If buying power cannot fund the risk qty but can fund fewer contracts,
    size down (do not reject) and stamp bp_limited='bp_limited(3->2)'.
    """
    if not isinstance(contract, dict):
        return 1
    entry = (
        contract.get("entry_premium")
        if contract.get("entry_premium") is not None
        else contract.get("entry_price")
    )
    sl = contract.get("stop_loss")
    desired = size_position(entry, sl, buying_power=None)
    if buying_power is None:
        try:
            buying_power = float(get_portfolio().get("buying_power"))
        except (TypeError, ValueError):
            buying_power = None
    qty = size_position(entry, sl, buying_power=buying_power)
    contract["quantity"] = qty
    contract["qty_desired"] = desired
    contract.pop("bp_limited", None)
    if desired > 0 and qty != desired:
        contract["bp_limited"] = f"bp_limited({desired}->{qty})"
    return qty


def format_execute_qty_bit(contract: dict[str, Any] | None, qty: int | None = None) -> str:
    """`qty=2 bp_limited(3->2)` fragment for the EXECUTE line."""
    if isinstance(contract, dict):
        if qty is None:
            try:
                qty = int(contract.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0
        lim = contract.get("bp_limited")
        if lim:
            return f"qty={qty} {lim}"
    return f"qty={qty if qty is not None else 1}"


def paper_buy(
    contract: Any,
    entry_price: float | int | None,
    quantity: int | None = None,
) -> dict[str, Any]:
    """
    Open a virtual long option: debit entry_price * 100 * qty from buying_power.

    Returns a result dict with ok/error and the updated ledger snapshot.
    quantity defaults to contract['quantity'] or 1.
    """
    ensure_ledger()
    try:
        premium = float(entry_price)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"invalid entry_price: {entry_price!r}"}

    if premium <= 0:
        return {"ok": False, "error": f"entry_price must be > 0, got {premium}"}

    qty = resolve_quantity(contract, quantity)
    if qty < 1:
        return {
            "ok": False,
            "error": "insufficient buying_power",
            "quantity": 0,
            "cost": 0.0,
        }

    cost = premium * CONTRACT_MULTIPLIER * qty
    now = datetime.now(timezone.utc).isoformat()

    # Stage 1: write_guard is additive only. Soft failures still return
    # {"ok": False, ...} as before. Hard DB errors still raise so callers'
    # existing except blocks keep logging — we record then re-raise.
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT buying_power, total_realized_pnl FROM portfolio_ledger WHERE id = 1"
            ).fetchone()
            if not row:
                return {"ok": False, "error": "portfolio_ledger missing after ensure"}

            buying_power = float(row["buying_power"])
            if buying_power < cost:
                print(
                    f"[VirtualBroker] paper_buy blocked: need ${cost:.2f}, "
                    f"have ${buying_power:.2f}"
                )
                return {
                    "ok": False,
                    "error": "insufficient buying_power",
                    "buying_power": buying_power,
                    "cost": cost,
                    "quantity": qty,
                }

            new_bp = buying_power - cost
            conn.execute(
                """
                UPDATE portfolio_ledger
                SET buying_power = ?, updated_at = ?
                WHERE id = 1
                """,
                (new_bp, now),
            )
            conn.commit()
            realized = float(row["total_realized_pnl"])
    except Exception as e:
        try:
            import write_guard
            write_guard.record_write_fail("paper_broker", e)
        except Exception:
            pass
        raise

    try:
        import write_guard
        write_guard.record_write_ok("paper_broker")
    except Exception:
        pass

    meta = _contract_meta(contract)
    if isinstance(contract, dict):
        contract["quantity"] = qty
        meta = contract
    ticker = meta.get("ticker") or meta.get("symbol")
    direction = meta.get("direction") or "?"

    # Paper vs. market: theoretical fill vs. displayed ask (observability only).
    # Does not change debit math — only records how far paper entry sat from ask.
    theoretical_slippage: float | None = None
    ask = _extract_ask(meta)
    if ask is not None:
        theoretical_slippage = (premium - ask) * CONTRACT_MULTIPLIER * qty
        fp = _entry_fingerprint(ticker, direction, premium)
        _pending_slippage[fp] = theoretical_slippage
        # Log to trade_history immediately (open marker); paper_sell writes the
        # realized close row with the same slippage for closed-trade analytics.
        try:
            with _connect() as conn:
                _ensure_slippage_column(conn)
                conn.execute(
                    """
                    INSERT INTO trade_history
                        (closed_at, ticker, direction, strike, expiration,
                         entry_price, exit_price, pnl, contract_json, notes, slippage)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, ?, 'PAPER_BUY_OPEN', ?)
                    """,
                    (
                        now,
                        ticker,
                        str(direction) if direction else None,
                        float(meta["strike"]) if meta.get("strike") is not None else None,
                        str(meta["expiration"]) if meta.get("expiration") else None,
                        premium,
                        premium,
                        json.dumps(meta, default=str) if meta else None,
                        theoretical_slippage,
                    ),
                )
                conn.commit()
            print(
                f"[VirtualBroker] theoretical_slippage "
                f"{ticker or '?'} ask=${ask:.4f} entry=${premium:.2f} "
                f"→ ${theoretical_slippage:.2f}"
            )
        except Exception as slip_err:
            print(f"[VirtualBroker] WARNING: slippage DB log failed: {slip_err}")

    note_session_open(cost)
    print(
        f"[VirtualBroker] paper_buy "
        f"{ticker or meta.get('direction', '?')} "
        f"qty={qty} @ ${premium:.2f} → debit ${cost:.2f}; "
        f"buying_power ${buying_power:.2f} → ${new_bp:.2f}"
    )
    event: dict[str, Any] = {
        "type": "EXECUTE",
        "message": (
            f"BOUGHT: {ticker or '?'} {direction} qty={qty} @ ${premium}"
        ),
    }
    if theoretical_slippage is not None:
        event["slippage"] = theoretical_slippage
    ui_event_queue.put(event)
    result = {
        "ok": True,
        "cost": cost,
        "entry_price": premium,
        "quantity": qty,
        "buying_power": new_bp,
        "total_realized_pnl": realized,
    }
    if theoretical_slippage is not None:
        result["slippage"] = theoretical_slippage
    return result


def paper_sell(
    contract: Any,
    exit_price: float | int | None,
    direction: str | None,
    entry_price: float | int | None,
    notes: str | None = None,
    quantity: int | None = None,
) -> dict[str, Any]:
    """
    Close a virtual long option:
      * credit exit_price * 100 * qty back to buying_power
      * realized PnL = (exit_price - entry_price) * 100 * qty
      * append a row to trade_history

    notes: optional exit reason (e.g. EXIT:STOP_LOSS) for audit/analytics.
    quantity defaults to contract['quantity'] or 1.
    """
    ensure_ledger()
    try:
        entry = float(entry_price)
        exit_ = float(exit_price)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": f"invalid prices entry={entry_price!r} exit={exit_price!r}",
        }

    qty = resolve_quantity(contract, quantity)
    pnl = (exit_ - entry) * CONTRACT_MULTIPLIER * qty
    capital_back = exit_ * CONTRACT_MULTIPLIER * qty
    now = datetime.now(timezone.utc).isoformat()
    meta = _contract_meta(contract)

    # Prefer explicit direction arg; fall back to contract fields
    dir_str = direction or meta.get("direction") or ""
    ticker = (
        meta.get("ticker")
        or meta.get("symbol")
        or (meta.get("option_contract") or {}).get("ticker")
    )
    strike = meta.get("strike")
    expiration = meta.get("expiration")
    if isinstance(meta.get("option_contract"), dict):
        oc = meta["option_contract"]
        strike = strike if strike is not None else oc.get("strike")
        expiration = expiration or oc.get("expiration")
        dir_str = dir_str or oc.get("direction") or ""

    with _connect() as conn:
        row = conn.execute(
            "SELECT buying_power, total_realized_pnl FROM portfolio_ledger WHERE id = 1"
        ).fetchone()
        if not row:
            return {"ok": False, "error": "portfolio_ledger missing after ensure"}

        new_bp = float(row["buying_power"]) + capital_back
        new_pnl = float(row["total_realized_pnl"]) + pnl

        # Attach buy-time theoretical slippage if we still have it cached.
        fp = _entry_fingerprint(ticker, dir_str, entry)
        slippage = _pending_slippage.pop(fp, None)
        if slippage is None:
            # Fallback: recompute from ask if the contract still carries it.
            ask = _extract_ask(meta)
            if ask is not None:
                slippage = (entry - ask) * CONTRACT_MULTIPLIER * qty

        _ensure_slippage_column(conn)
        conn.execute(
            """
            UPDATE portfolio_ledger
            SET buying_power = ?, total_realized_pnl = ?, updated_at = ?
            WHERE id = 1
            """,
            (new_bp, new_pnl, now),
        )
        conn.execute(
            """
            INSERT INTO trade_history
                (closed_at, ticker, direction, strike, expiration,
                 entry_price, exit_price, pnl, contract_json, notes, slippage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                ticker,
                str(dir_str) if dir_str else None,
                float(strike) if strike is not None else None,
                str(expiration) if expiration else None,
                entry,
                exit_,
                pnl,
                json.dumps(meta, default=str) if meta else None,
                notes,
                slippage,
            ),
        )
        conn.commit()

    note_session_close(entry * CONTRACT_MULTIPLIER * qty)
    print(
        f"[VirtualBroker] paper_sell {ticker or '?'} {dir_str} "
        f"qty={qty} entry=${entry:.2f} exit=${exit_:.2f} PnL=${pnl:.2f}; "
        f"buying_power → ${new_bp:.2f}, realized → ${new_pnl:.2f}"
        + (f"; slippage=${slippage:.2f}" if slippage is not None else "")
    )
    close_event: dict[str, Any] = {
        "type": "CLOSE",
        "message": (
            f"SOLD: {ticker or '?'} {dir_str or '?'} qty={qty} @ ${exit_}"
        ),
    }
    if slippage is not None:
        close_event["slippage"] = slippage
    ui_event_queue.put(close_event)
    result = {
        "ok": True,
        "pnl": pnl,
        "capital_back": capital_back,
        "entry_price": entry,
        "exit_price": exit_,
        "quantity": qty,
        "buying_power": new_bp,
        "total_realized_pnl": new_pnl,
    }
    if slippage is not None:
        result["slippage"] = slippage
    return result


# Seed tables on import so first API hit never races an empty DB.
try:
    ensure_ledger()
except Exception as _init_err:
    print(f"[VirtualBroker] WARNING: ensure_ledger on import failed: {_init_err}")
