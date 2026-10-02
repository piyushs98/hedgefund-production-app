"""Print the measurement report from the persistent SQLite tables.

    python -m ops.report

Discord is not read. The command opens the databases URI mode=ro.
"""

from __future__ import annotations

from datetime import datetime

from ops.metrics import BARS, summarize
from ops.status import load_active_trades, open_debit
from ops.store import default_paths, reset_ledger_flag


def _money(val) -> str:
    if val is None:
        return "n/a"
    return f"${val:,.2f}"


def _r(val) -> str:
    if val is None:
        return "n/a"
    return f"{val:+.2f}R"


def _pct(val) -> str:
    if val is None:
        return "n/a"
    return f"{val:.2f}%"


def _num(val, digits=2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{digits}f}"


def render_report(paths=None) -> str:
    paths = paths or default_paths()
    trades = load_active_trades(paths.active_trades)
    summary = summarize(paths.news_db, paths.hedge_db, open_debit=open_debit(trades))
    flag = reset_ledger_flag(paths.env_file)
    lines: list[str] = []
    add = lines.append

    add("MEASUREMENT REPORT")
    add("Source: SQLite on this disk, opened read-only. Discord is not read.")
    add(f"news db: {paths.news_db}")
    add(f"hedge db: {paths.hedge_db}")
    add("")
    add(f"Closed trades: {summary['n_closed']}  "
        f"(PAPER_BUY_OPEN markers excluded)")
    add(f"Trades with planned risk: {summary['n_with_r']}  "
        f"missing stop or risk: {summary['n_missing_risk']}")
    add(f"Fills tagged estimated (bid or ask fell back to mid): {summary['fill_est']}")
    add("")

    add("EXPECTANCY  (mean fill-adjusted R per closed trade)")
    add(f"  value: {_r(summary['expectancy_r'])}")
    add(f"  bar:   >= {BARS['expectancy_r']:+.2f}R over {BARS['min_trades']}+ trades")
    add("  R = pnl_fill / ((entry_mid - stop) * 100 * qty). "
        "pnl_fill is exit bid minus entry ask.")
    add("")

    add("PROFIT FACTOR  (gross fill wins / gross fill losses)")
    if summary["n_closed"] == 0:
        add("  value: n/a (no closed trades)")
    elif summary["no_losses"]:
        add(f"  value: n/a (no losing trades; gross wins {_money(summary['gross_wins'])})")
    else:
        add(f"  value: {_num(summary['profit_factor'])}  "
            f"wins {_money(summary['gross_wins'])}  "
            f"losses {_money(summary['gross_losses'])}")
    add(f"  bar:   >= {BARS['profit_factor']}")
    add("")

    add("MAX DRAWDOWN  (peak to trough of starting equity + cumulative pnl_fill)")
    add(f"  dollars: {_money(summary['max_dd_dollars'])}")
    add(f"  percent of seed: {_pct(summary['max_dd_pct'])}")
    add(f"  seed: {_money(summary['seed'])}  "
        "(buying power - realized mid + open debit at entry)")
    add(f"  bar:   <= {BARS['max_drawdown_pct']:.0f}% of starting equity")
    add("  Open marks are not in this curve. Only closed fill P&L moves it.")
    add("")

    mfe = summary["mfe"]
    add("MFE CAPTURE  (pnl_fill / peak mid excursion, trades with a positive peak)")
    add(f"  value: {_num(mfe['mean'])}   n={mfe['n']}  "
        f"no positive peak={mfe['nonpositive']}  no mfe={mfe['missing']}")
    add(f"  bar:   >= {BARS['mfe_capture']}")
    add("  Peak is the max of position_marks.pnl_pct and the close mid print,")
    add("  the same inputs as the TRADE mfe_pct field. Marks are mid; P&L is fill.")
    add("")

    add("SLIPPAGE AND COIN_FLIP")
    add(f"  mean entry spread: {_pct(summary['entry_spread_pct'])}  "
        f"n={summary['n_entry_spread']}  "
        "((entry_ask - entry_mid) / entry_mid)")
    add(f"  mean stop slippage: {_pct(summary['stop_slippage_pct'])}  "
        f"n={summary['n_stop_slippage']}")
    add("  Stop slippage is (mid loss - planned risk) / planned risk on "
        "STOP_LOSS exits.")
    add(f"  bar:   average stop slippage <= {BARS['stop_slippage_pct']:.0f}%, "
        "and no coin_flip labels")
    add("  coin_flip count: unavailable. The label is computed at exit from")
    add("  the live 5-minute ATR and is not written to a table.")
    add("")

    add("P&L BY EXIT REASON  (fill dollars, and mean R where risk is known)")
    if not summary["by_reason"]:
        add("  (no closed trades)")
    else:
        for reason in sorted(summary["by_reason"]):
            bucket = summary["by_reason"][reason]
            mean_r = (
                bucket["r_sum"] / bucket["r_n"] if bucket["r_n"] else None
            )
            add(
                f"  {reason}: n={int(bucket['n'])}  "
                f"pnl_fill {_money(bucket['pnl_fill'])}  "
                f"mean R {_r(mean_r)}"
            )
    add("")

    add("REALIZED FILL VS REALIZED MID")
    add(f"  closed trades: fill {_money(summary['realized_fill'])}  "
        f"mid {_money(summary['realized_mid'])}  "
        f"spread cost {_money(summary['spread_cost'])}")
    ledger = summary["ledger"] or {}
    add(f"  portfolio_ledger: buying_power {_money(ledger.get('buying_power'))}  "
        f"realized_mid {_money(ledger.get('total_realized_pnl'))}  "
        f"realized_fill {_money(ledger.get('total_realized_pnl_fill'))}")
    add("  Spread cost is realized mid minus realized fill. "
        "The ledger cash series still moves on mid.")
    add("")

    add("REGIMES")
    add("  not stored in the persistent tables. "
        "The pre-registered bar is positive in at least 2 of 3 regimes.")
    add("")

    sessions = summary["sessions"]
    add("SESSIONS  (backtest_telemetry, Chicago date)")
    add(f"  counted: {len(sessions['counted'])}  "
        + (", ".join(sessions["counted"]) if sessions["counted"] else "(none)"))
    add(f"  excluded for data outage: {len(sessions['excluded'])}  "
        + (", ".join(sessions["excluded"]) if sessions["excluded"] else "(none)"))
    add(f"  scans stored: {sessions['scans']}")
    add(f"  {sessions['note']}")
    add("  Unhandled CRITICAL counts are not in these tables.")
    add("")

    add("DISK")
    add(f"  RESET_LEDGER_ON_BOOT={flag}")
    if str(flag).strip().lower() in ("1", "true", "yes", "on"):
        add("  WARNING: the next bot start wipes trade_history, "
            "active_trades_store, position_marks,")
        add("  and portfolio_ledger, and clears active_trades.json.")
    else:
        add("  A restart does not wipe the trade tables while this flag is off.")
    add("  ensure_ledger inserts a seed row only when portfolio_ledger is empty.")
    add("  It does not delete rows. reset_ledger_now does not touch hedge_fund.db.")
    add("  On a normal boot the only deletes are: headlines older than 90 days,")
    add("  and innovation_data rows tagged CHINA_MACRO or GOV_POLICY.")
    add("  active_trades_store is replaced to match active_trades.json when a")
    add("  position is saved. That sync is not a boot wipe.")
    add(f"  open positions in active_trades.json: {len(trades)}")
    orphan_ids = ", ".join(summary["orphan_ids"]) if summary["orphan_ids"] else "—"
    add(f"  position_marks rows: {summary['mark_rows']}  "
        f"not joined to a closed trade: {summary['orphan_marks']}  "
        f"trade_id {orphan_ids}")
    _emit_tables(add, "news_room.db", summary["news_tables"], paths.news_db)
    _emit_tables(add, "hedge_fund.db", summary["hedge_tables"], paths.hedge_db)
    add("")
    add("This sample is not a go/no-go decision until the trade count and the")
    add("session rules above are met. Nothing in this command changes the bot.")
    return "\n".join(lines) + "\n"


def _emit_tables(add, label: str, tables: list[dict], path) -> None:
    if not path.is_file():
        add(f"  {label}: file missing ({path})")
        return
    stat = path.stat()
    stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    add(f"  {label}: {stat.st_size} bytes, mtime {stamp}")
    if not tables:
        add("    (no tables)")
        return
    for table in tables:
        cols = ", ".join(table["columns"])
        add(f"    {table['name']}: {table['rows']} rows  [{cols}]")


def main() -> None:
    print(render_report(), end="")


if __name__ == "__main__":
    main()
