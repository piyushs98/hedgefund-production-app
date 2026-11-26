"""Follow the paper book. Decisions are tested with a fake broker."""

from __future__ import annotations

from datetime import datetime, timezone

from webull_exec.clock import at_or_after, entry_block, flat_at, now_ct
from webull_exec.envfile import set_mode_off
from webull_exec.mode import orders_allowed
from webull_exec.limits import (
    CUMULATIVE_LOSS,
    DAILY_STOP_R,
    ENTRY_WAIT_S,
    EXIT_CRITICAL_S,
    EXIT_REPRICE_S,
    MAX_OPEN,
    MAX_PREMIUM,
    STOP_UNPROTECTED_S,
    TICKERS,
    stop_limit_price,
)
from webull_exec.orders import entry_limit, entry_with_stop, protective_stop, sell_limit

HALT_NAME = "webull_exec.halt"


def _utc(now: datetime) -> str:
    current = now_ct(now)
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _elapsed(now: datetime, stamp: str | None) -> float | None:
    if not stamp:
        return None
    start = datetime.fromisoformat(stamp)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo is not None else now_ct(now)
    return (current - start).total_seconds()


def _same(pos: dict, intent: dict) -> bool:
    try:
        return (
            str(pos.get("ticker") or "").upper() == str(intent.get("ticker") or "").upper()
            and str(pos.get("option_type") or "").upper().startswith(str(intent.get("option_type") or "X")[:1])
            and abs(float(pos.get("strike")) - float(intent.get("strike"))) < 0.001
            and str(pos.get("expiration") or "")[:10] == str(intent.get("expiration") or "")[:10]
        )
    except (TypeError, ValueError):
        return False


def _describe(positions: list[dict], orders: list[dict]) -> str:
    parts = []
    for pos in positions:
        parts.append(
            f"{pos.get('ticker')} {pos.get('expiration')} {pos.get('option_type')} "
            f"{pos.get('strike')} x{pos.get('qty')}"
        )
    if orders:
        parts.append(f"{len(orders)} open orders")
    return "; ".join(parts) if parts else "empty"


class Engine:
    def __init__(self, broker, store, notify, paper_opens, paper_close, env_path=None, halt_path=None):
        self.broker = broker
        self.store = store
        self.notify = notify
        self.paper_opens = paper_opens
        self.paper_close = paper_close
        self.env_path = env_path
        self.halt_path = halt_path
        self.boot_done = False
        self.boot_clearing = False
        self.baseline: set[str] = set()
        self._last_reconcile: datetime | None = None
        self._last_heartbeat: datetime | None = None

    def boot(self, now: datetime) -> None:
        self.baseline = {row.trade_id for row in self.paper_opens()}
        positions = self.broker.positions()
        orders = self.broker.open_orders()
        if positions or orders:
            self.boot_clearing = True
            self.notify.critical(
                "boot-open",
                "boot found broker positions or orders and is flattening them. " + _describe(positions, orders),
            )
            self._cancel_orders(orders)
            self._sell_positions(positions, now, intent_name="boot")
        for row in self.store.open_intents():
            row["state"] = "boot_flattened"
            row["miss_reason"] = "boot"
            self.store.upsert_intent(row)
        self.boot_done = True
        self._last_reconcile = now
        self._last_heartbeat = now
        self.notify.heartbeat()

    def tick(self, now: datetime) -> None:
        if not self.boot_done:
            self.boot(now)
        self._maybe_heartbeat(now)
        if self.store.killed():
            return
        if self.boot_clearing:
            self._finish_boot(now)
            if self.boot_clearing:
                return
        self._clock(now)
        self._entries(now)
        self._manage(now)
        self._maybe_reconcile(now)
        self._loss_caps(now)

    def _finish_boot(self, now: datetime) -> None:
        positions = self.broker.positions()
        orders = self.broker.open_orders()
        if not positions and not orders:
            self.boot_clearing = False
            return
        self._cancel_orders(orders)
        self._sell_positions(positions, now, intent_name="boot")
        if _elapsed(now, self.store.get_control("boot_started")) is None:
            self.store.set_control("boot_started", _utc(now))
        age = _elapsed(now, self.store.get_control("boot_started")) or 0
        if age >= EXIT_CRITICAL_S:
            self.notify.critical(
                "boot-stuck",
                "boot flatten still open after 2 min. " + _describe(positions, orders),
            )

    def _entries(self, now: datetime) -> None:
        if self.store.disabled() or not orders_allowed():
            return
        if entry_block(now):
            return
        day = now_ct(now).date().isoformat()
        if self.store.daily_r(day) <= DAILY_STOP_R:
            self.notify.critical(f"daily-stop-{day}", f"daily R {self.store.daily_r(day):.3f} blocks new entries")
            return
        open_n = len(self.store.open_intents())
        for paper in self.paper_opens():
            if paper.trade_id in self.baseline or paper.trade_id in self.store.known_ids():
                continue
            reason = self._skip_reason(paper, open_n)
            if reason:
                self._miss(paper, reason)
                continue
            self._submit_entry(paper, now)
            open_n += 1

    def _skip_reason(self, paper, open_n: int) -> str | None:
        if paper.ticker not in TICKERS:
            return "ticker"
        if paper.entry_score is None or paper.entry_score < 80:
            return "score"
        if paper.entry_ask is None or paper.entry_ask <= 0 or paper.entry_ask > MAX_PREMIUM:
            return "premium"
        if paper.stop_loss is None or paper.stop_loss <= 0 or paper.stop_loss >= paper.entry_ask:
            return "stop"
        if open_n >= MAX_OPEN:
            return "max_open"
        return None

    def _miss(self, paper, reason: str) -> None:
        self.store.upsert_intent(
            {
                "trade_id": paper.trade_id,
                "ticker": paper.ticker,
                "option_type": paper.option_type,
                "strike": paper.strike,
                "expiration": paper.expiration,
                "entry_ask": paper.entry_ask,
                "stop_price": paper.stop_loss,
                "entry_score": paper.entry_score,
                "state": "miss",
                "miss_reason": reason,
                "combo": 0,
                "repriced": 0,
            }
        )
        print(f"[webull] miss {paper.ticker} {paper.trade_id} {reason}")

    def _submit_entry(self, paper, now: datetime) -> None:
        day = now_ct(now).date().strftime("%Y%m%d")
        buy_id = self.store.next_id(day, "b")
        stop_id = self.store.next_id(day, "s")
        limit = stop_limit_price(paper.stop_loss)
        combo = entry_with_stop(
            buy_id=buy_id,
            stop_id=stop_id,
            ticker=paper.ticker,
            strike=paper.strike,
            expiration=paper.expiration,
            option_type=paper.option_type,
            entry_ask=paper.entry_ask,
            stop_price=paper.stop_loss,
            stop_limit=limit,
        )
        combo_id = self.store.next_id(day, "c")
        self._save_quote(
            paper.trade_id,
            "entry",
            getattr(paper, "entry_bid", None),
            paper.entry_ask,
            self._option_quote(paper),
        )
        row = self._row(paper, now)
        row["entry_client_order_id"] = buy_id
        row["stop_client_order_id"] = stop_id
        row["combo"] = 1
        row["state"] = "working"
        self.store.upsert_intent(row)
        self._pending(buy_id, paper.trade_id, "entry", combo[0])
        self._pending(stop_id, paper.trade_id, "stop", combo[1])
        result = self.broker.place(combo, combo_id)
        self.store.remember_order(buy_id, paper.trade_id, "entry", combo[0], result, "ack" if result.get("ok") else "rejected")
        self.store.remember_order(stop_id, paper.trade_id, "stop", combo[1], result, "ack" if result.get("ok") else "rejected")
        if result.get("ok"):
            return
        plain_id = self.store.next_id(day, "b")
        plain = entry_limit(
            client_order_id=plain_id,
            ticker=paper.ticker,
            strike=paper.strike,
            expiration=paper.expiration,
            option_type=paper.option_type,
            entry_ask=paper.entry_ask,
        )
        row["entry_client_order_id"] = plain_id
        row["stop_client_order_id"] = None
        row["combo"] = 0
        self.store.upsert_intent(row)
        self._pending(plain_id, paper.trade_id, "entry", plain[0])
        second = self.broker.place(plain, None)
        self.store.remember_order(plain_id, paper.trade_id, "entry", plain[0], second, "ack" if second.get("ok") else "rejected")
        row["state"] = "working" if second.get("ok") else "rejected"
        row["miss_reason"] = None if second.get("ok") else "rejected"
        self.store.upsert_intent(row)

    def _pending(self, client_order_id: str, trade_id: str, intent: str, request: dict) -> None:
        """Insert the id before the HTTP call so a crash cannot mint a second one."""
        self.store.remember_order(client_order_id, trade_id, intent, request, {}, "pending")

    def _row(self, paper, now: datetime) -> dict:
        return {
            "trade_id": paper.trade_id,
            "ticker": paper.ticker,
            "option_type": paper.option_type,
            "strike": paper.strike,
            "expiration": paper.expiration,
            "entry_ask": paper.entry_ask,
            "stop_price": paper.stop_loss,
            "entry_score": paper.entry_score,
            "state": "working",
            "combo": 0,
            "repriced": 0,
            "submitted_at": _utc(now),
        }

    def _manage(self, now: datetime) -> None:
        live_ids = {row.trade_id for row in self.paper_opens()}
        for row in self.store.open_intents():
            state = row["state"]
            if state == "working":
                self._manage_working(row, now, live_ids)
            elif state == "filled_unprotected":
                self._manage_unprotected(row, now, live_ids)
            elif state == "protected":
                if row["trade_id"] not in live_ids:
                    self._start_exit(row, now, "paper_close")
            elif state == "exiting":
                self._manage_exit(row, now)

    def _manage_working(self, row: dict, now: datetime, live_ids: set[str]) -> None:
        filled, price = self._filled(row)
        if filled:
            row["state"] = "protected" if row.get("combo") else "filled_unprotected"
            row["fill_price"] = None if price is None else str(price)
            row["filled_at"] = _utc(now)
            self.store.upsert_intent(row)
            if price is not None and row.get("entry_ask") is not None:
                self.store.add_slippage(row["entry_client_order_id"], row["trade_id"], "buy", float(row["entry_ask"]), float(price))
            self.store.mark_order(row["entry_client_order_id"], "FILLED", fill_price=row["fill_price"], filled=True)
            if row["state"] == "filled_unprotected":
                self._place_stop(row, now)
            return
        status = self.broker.order_status(row["entry_client_order_id"])
        if str(status.get("status") or "").upper() == "PARTIAL":
            self._on_partial(row, status, now)
            return
        age = _elapsed(now, row.get("submitted_at")) or 0
        gone = row["trade_id"] not in live_ids and row["trade_id"] not in self.baseline
        if age >= ENTRY_WAIT_S or gone:
            self.broker.cancel(row["entry_client_order_id"])
            if row.get("combo") and row.get("stop_client_order_id"):
                self.broker.cancel(row["stop_client_order_id"])
            self.store.mark_order(row["entry_client_order_id"], "CANCELLED", cancelled=True)
            row["state"] = "miss"
            row["miss_reason"] = "unfilled_60s" if not gone else "paper_gone"
            self.store.upsert_intent(row)
            print(f"[webull] miss {row['ticker']} {row['trade_id']} {row['miss_reason']}")

    def _on_partial(self, row: dict, status: dict, now: datetime) -> None:
        self.broker.cancel(row["entry_client_order_id"])
        filled_qty = float(status.get("filled_qty") or 0)
        if filled_qty >= 1:
            row["state"] = "protected" if row.get("combo") else "filled_unprotected"
            row["filled_at"] = _utc(now)
            row["fill_price"] = None if status.get("avg_price") is None else str(status.get("avg_price"))
            self.store.upsert_intent(row)
            if row["state"] == "filled_unprotected":
                self._place_stop(row, now)
            return
        row["state"] = "miss"
        row["miss_reason"] = "partial"
        self.store.upsert_intent(row)
        print(f"[webull] miss {row['ticker']} {row['trade_id']} partial")

    def _filled(self, row: dict) -> tuple[bool, float | None]:
        status = self.broker.order_status(row["entry_client_order_id"])
        state = str(status.get("status") or "").upper()
        filled_qty = float(status.get("filled_qty") or 0)
        if state == "FILLED" or filled_qty >= 1:
            return True, status.get("avg_price")
        for pos in self.broker.positions():
            if _same(pos, row) and int(pos.get("qty") or 0) != 0:
                return True, status.get("avg_price")
        return False, None

    def _place_stop(self, row: dict, now: datetime) -> None:
        day = now_ct(now).date().strftime("%Y%m%d")
        stop_id = row.get("stop_client_order_id") or self.store.next_id(day, "s")
        limit = stop_limit_price(float(row["stop_price"]))
        payload = protective_stop(
            client_order_id=stop_id,
            ticker=row["ticker"],
            strike=float(row["strike"]),
            expiration=row["expiration"],
            option_type=row["option_type"],
            stop_price=float(row["stop_price"]),
            stop_limit=limit,
        )
        self._pending(stop_id, row["trade_id"], "stop", payload[0])
        result = self.broker.place(payload, None)
        self.store.remember_order(stop_id, row["trade_id"], "stop", payload[0], result, "ack" if result.get("ok") else "rejected")
        row["stop_client_order_id"] = stop_id
        if result.get("ok"):
            row["state"] = "protected"
        else:
            row["state"] = "filled_unprotected"
            age = _elapsed(now, row.get("filled_at")) or 0
            if age >= STOP_UNPROTECTED_S:
                self.notify.critical(f"stop-{row['trade_id']}", f"stop rejected for {row['ticker']} {row['trade_id']}")
        self.store.upsert_intent(row)

    def _manage_unprotected(self, row: dict, now: datetime, live_ids: set[str]) -> None:
        status = {}
        if row.get("stop_client_order_id"):
            status = self.broker.order_status(row["stop_client_order_id"])
        if str(status.get("status") or "").upper() in ("WORKING", "ACK", "PENDING", "SUBMITTED"):
            row["state"] = "protected"
            self.store.upsert_intent(row)
            return
        self._place_stop(row, now)
        if row["trade_id"] not in live_ids:
            fresh = self.store.intent(row["trade_id"]) or row
            self._start_exit(fresh, now, "paper_close")

    def _option_quote(self, paper_or_row) -> dict:
        fn = getattr(self.broker, "snapshot_option", None)
        if fn is None:
            return {}
        try:
            snap = fn(
                paper_or_row.ticker if hasattr(paper_or_row, "ticker") else paper_or_row["ticker"],
                paper_or_row.expiration if hasattr(paper_or_row, "expiration") else paper_or_row["expiration"],
                paper_or_row.option_type if hasattr(paper_or_row, "option_type") else paper_or_row["option_type"],
                float(paper_or_row.strike if hasattr(paper_or_row, "strike") else paper_or_row["strike"]),
            )
        except Exception:
            return {}
        return snap if isinstance(snap, dict) else {}

    def _save_quote(self, trade_id: str, side: str, yahoo_bid, yahoo_ask, snap: dict) -> None:
        """Store the two quotes. The order price is chosen by the caller."""
        self.store.add_quote(
            trade_id,
            side,
            yahoo_bid,
            yahoo_ask,
            snap.get("bid"),
            snap.get("ask"),
            snap.get("quote_time"),
            snap.get("symbol"),
        )

    def _start_exit(self, row: dict, now: datetime, why: str) -> None:
        if row.get("stop_client_order_id"):
            self.broker.cancel(row["stop_client_order_id"])
            self.store.mark_order(row["stop_client_order_id"], "CANCELLED", cancelled=True)
        close = self.paper_close(row["trade_id"])
        yahoo_bid = None if close is None else close.exit_bid
        snap = self._option_quote(row)
        self._save_quote(row["trade_id"], "exit", yahoo_bid, None, snap)
        bid = yahoo_bid
        if bid is None or bid <= 0:
            bid = snap.get("bid")
        if bid is None or bid <= 0:
            self.notify.critical(f"nobid-{row['trade_id']}", f"no exit bid for {row['ticker']} {row['trade_id']} ({why})")
            row["state"] = "exiting"
            row["exit_started_at"] = row.get("exit_started_at") or _utc(now)
            self.store.upsert_intent(row)
            return
        self._send_exit(row, now, float(bid), why)

    def _send_exit(self, row: dict, now: datetime, limit: float, why: str) -> None:
        day = now_ct(now).date().strftime("%Y%m%d")
        kind = "r" if row.get("exit_client_order_id") else "x"
        if why == "flatten":
            kind = "f"
        exit_id = self.store.next_id(day, kind)
        payload = sell_limit(
            client_order_id=exit_id,
            ticker=row["ticker"],
            strike=float(row["strike"]),
            expiration=row["expiration"],
            option_type=row["option_type"],
            limit_price=limit,
        )
        self._pending(exit_id, row["trade_id"], "exit", payload[0])
        result = self.broker.place(payload, None)
        self.store.remember_order(exit_id, row["trade_id"], "exit", payload[0], result, "ack" if result.get("ok") else "rejected")
        row["exit_client_order_id"] = exit_id
        row["exit_limit"] = limit
        row["state"] = "exiting"
        row["exit_started_at"] = row.get("exit_started_at") or _utc(now)
        row["exit_order_at"] = _utc(now)
        row["paper_exit_bid"] = limit
        self.store.upsert_intent(row)

    def _manage_exit(self, row: dict, now: datetime) -> None:
        status = self.broker.order_status(row["exit_client_order_id"]) if row.get("exit_client_order_id") else {"status": "UNKNOWN", "filled_qty": 0}
        state = str(status.get("status") or "").upper()
        if state == "FILLED" or float(status.get("filled_qty") or 0) >= 1:
            self._record_flat(row, status.get("avg_price"), now)
            return
        still = any(_same(pos, row) for pos in self.broker.positions())
        if row.get("exit_client_order_id") and not still and state in ("CANCELLED", "CANCELED", "FILLED"):
            self._record_flat(row, status.get("avg_price") or row.get("exit_limit"), now)
            return
        age_order = _elapsed(now, row.get("exit_order_at")) or 0
        age_exit = _elapsed(now, row.get("exit_started_at")) or 0
        if not row.get("repriced") and age_order >= EXIT_REPRICE_S and row.get("exit_client_order_id"):
            self.broker.cancel(row["exit_client_order_id"])
            self.store.mark_order(row["exit_client_order_id"], "CANCELLED", cancelled=True)
            snap = self._option_quote(row)
            self._save_quote(row["trade_id"], "exit", row.get("paper_exit_bid"), None, snap)
            fresh = snap.get("bid")
            limit = fresh if fresh and fresh > 0 else row.get("paper_exit_bid") or row.get("exit_limit")
            row["repriced"] = 1
            self.store.upsert_intent(row)
            if limit:
                self._send_exit(row, now, float(limit), "reprice")
            return
        if age_exit >= EXIT_CRITICAL_S:
            self.notify.critical(
                f"exit-{row['trade_id']}",
                f"still open 2 min after exit {row['ticker']} {row['expiration']} {row['option_type']} {row['strike']}",
            )

    def _record_flat(self, row: dict, fill_price, now: datetime) -> None:
        price = None
        try:
            if fill_price is not None and fill_price != "":
                price = float(fill_price)
        except (TypeError, ValueError):
            price = None
        if price is not None and row.get("paper_exit_bid") is not None:
            self.store.add_slippage(row.get("exit_client_order_id") or "", row["trade_id"], "sell", float(row["paper_exit_bid"]), price)
        entry = float(row["entry_ask"]) if row.get("entry_ask") is not None else None
        pnl = None if price is None or entry is None else (price - entry) * 100.0
        risk = None
        if entry is not None and row.get("stop_price") is not None:
            risk = (entry - float(row["stop_price"])) * 100.0
        r_multiple = None if pnl is None or not risk else pnl / risk
        if pnl is not None:
            self.store.add_realized(row["trade_id"], now_ct(now).date().isoformat(), pnl, r_multiple)
        if row.get("exit_client_order_id"):
            self.store.mark_order(row["exit_client_order_id"], "FILLED", fill_price=None if price is None else str(price), filled=True)
        row["state"] = "flat"
        row["fill_price"] = None if price is None else str(price)
        self.store.upsert_intent(row)

    def _clock(self, now: datetime) -> None:
        ct = now_ct(now)
        day = ct.date()
        day_s = day.isoformat()
        if at_or_after(now, flat_at(day)) and not self.store.saw_flatten(day_s):
            self.store.mark_flatten(day_s)
            self._flatten_all(now)
        sweep = flat_at(day).hour * 60 + flat_at(day).minute + 5
        wall = ct.hour * 60 + ct.minute
        if wall >= sweep and not self.store.saw_sweep(day_s):
            self.store.mark_sweep(day_s)
            positions = self.broker.positions()
            if positions:
                self.notify.critical("sweep-" + day_s, "sweep still open. " + _describe(positions, []))
            else:
                self.notify.info("sweep-" + day_s, "FLAT CONFIRMED (0 positions)")

    def _flatten_all(self, now: datetime) -> None:
        for row in list(self.store.open_intents()):
            if row["state"] == "working":
                self.broker.cancel(row["entry_client_order_id"])
                if row.get("stop_client_order_id"):
                    self.broker.cancel(row["stop_client_order_id"])
                row["state"] = "miss"
                row["miss_reason"] = "flatten"
                self.store.upsert_intent(row)
            elif row["state"] in ("protected", "filled_unprotected"):
                self._start_exit(row, now, "flatten")

    def _maybe_reconcile(self, now: datetime) -> None:
        if self._last_reconcile is not None and (_elapsed(now, _utc(self._last_reconcile)) or 0) < 60:
            return
        self._last_reconcile = now
        problems = reconcile(self.store.open_intents(), self.broker.positions(), self.broker.open_orders(), now)
        if problems:
            self.notify.critical("reconcile", "; ".join(problems))

    def _maybe_heartbeat(self, now: datetime) -> None:
        if self._last_heartbeat is not None and (_elapsed(now, _utc(self._last_heartbeat)) or 0) < 60:
            return
        self._last_heartbeat = now
        self.notify.heartbeat()

    def _loss_caps(self, now: datetime) -> None:
        pnl = self.store.cumulative_pnl()
        if pnl <= CUMULATIVE_LOSS and not self.store.disabled():
            self.store.set_control("disabled", "1")
            self._write_halt()
            if self.env_path is not None:
                set_mode_off(self.env_path)
            self.notify.critical("loss-cap", f"cumulative loss {pnl:.2f} disables the executor")

    def _write_halt(self) -> None:
        if self.halt_path is None:
            return
        self.halt_path.parent.mkdir(parents=True, exist_ok=True)
        self.halt_path.write_text("disabled\n", encoding="utf-8")

    def _cancel_orders(self, orders: list[dict]) -> None:
        for order in orders:
            client_order_id = order.get("client_order_id")
            if client_order_id:
                self.broker.cancel(client_order_id)

    def _sell_positions(self, positions: list[dict], now: datetime, intent_name: str) -> None:
        day = now_ct(now).date().strftime("%Y%m%d")
        for pos in positions:
            if not pos.get("ticker") or pos.get("strike") is None or not pos.get("expiration"):
                self.notify.critical("boot-unknown", "open position missing contract fields")
                continue
            bid = self.broker.snapshot_bid(pos["ticker"], pos["expiration"], pos.get("option_type") or "CALL", float(pos["strike"]))
            if bid is None or bid <= 0:
                self.notify.critical(
                    f"{intent_name}-nobid-{pos.get('ticker')}",
                    f"no bid to flatten {pos.get('ticker')} {pos.get('expiration')} {pos.get('option_type')} {pos.get('strike')}",
                )
                continue
            exit_id = self.store.next_id(day, "f")
            payload = sell_limit(
                client_order_id=exit_id,
                ticker=pos["ticker"],
                strike=float(pos["strike"]),
                expiration=pos["expiration"],
                option_type=pos.get("option_type") or "CALL",
                limit_price=float(bid),
            )
            self._pending(exit_id, intent_name, "flatten", payload[0])
            result = self.broker.place(payload, None)
            self.store.remember_order(exit_id, intent_name, "flatten", payload[0], result, "ack" if result.get("ok") else "rejected")


def reconcile(intents: list[dict], positions: list[dict], orders: list[dict], now: datetime) -> list[str]:
    """Return mismatch descriptions. Orders younger than 15s are not required yet."""
    mature = []
    for row in intents:
        age = _elapsed(now, row.get("submitted_at"))
        if age is not None and age < 15:
            continue
        mature.append(row)
    held = ("protected", "filled_unprotected", "exiting")
    expected_pos = [row for row in intents if row["state"] in held]
    mature_pos = [row for row in mature if row["state"] in held]
    expected_ids = set()
    for row in intents:
        if row["state"] == "working" and row.get("entry_client_order_id"):
            expected_ids.add(row["entry_client_order_id"])
            if row.get("combo") and row.get("stop_client_order_id"):
                expected_ids.add(row["stop_client_order_id"])
        if row["state"] == "protected" and row.get("stop_client_order_id"):
            expected_ids.add(row["stop_client_order_id"])
        if row["state"] == "exiting" and row.get("exit_client_order_id"):
            expected_ids.add(row["exit_client_order_id"])
    problems = []
    for pos in positions:
        if not any(_same(pos, row) for row in expected_pos):
            problems.append(
                f"broker position without a book row {pos.get('ticker')} {pos.get('expiration')} "
                f"{pos.get('option_type')} {pos.get('strike')} x{pos.get('qty')}"
            )
    for row in mature_pos:
        if not any(_same(pos, row) for pos in positions):
            problems.append(f"book row without a broker position {row.get('ticker')} {row.get('trade_id')}")
    for order in orders:
        client_order_id = order.get("client_order_id")
        if client_order_id and client_order_id not in expected_ids:
            problems.append(f"open order without a book row {client_order_id}")
    return problems
