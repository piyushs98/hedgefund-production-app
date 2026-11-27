"""Print the measurement report from the persistent SQLite tables.

    python -m ops.report

Discord is not read. The command opens the databases URI mode=ro.
"""

from __future__ import annotations

from datetime import datetime

from ops.metrics import BARS, MEASURED_VERSION, PRIOR_MEASURED_VERSION, summarize
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
    summary = summarize(
        paths.news_db, paths.hedge_db,
        open_debit=open_debit(trades),
        logs_dir=paths.logs_dir,
    )
    flag = reset_ledger_flag(paths.env_file)
    lines: list[str] = []
    add = lines.append

    add("MEASUREMENT REPORT")
    add("Source: SQLite on this disk, opened read-only. Discord is not read.")
    add(f"news db: {paths.news_db}")
    add(f"hedge db: {paths.hedge_db}")
    add("")
    add(f"Closed trades: {summary['n_closed']}  "
        f"(measured sample: {MEASURED_VERSION}, excluded_from_measurement=n)")
    add(f"Closed sample {PRIOR_MEASURED_VERSION}, own section: "
        f"{summary.get('n_prior', 0)}")
    add("PAPER_BUY_OPEN markers are not closed trades.")
    add("Excluded from measurement (unmonitored Oct 5–6): "
        f"{summary.get('n_excluded', 0)}")
    add(f"Oct 2, version v7a25760, shown in its own section: {summary.get('n_oct2', 0)}")
    if summary.get("n_unstamped") or summary.get("n_other"):
        others = ", ".join(summary.get("other_versions") or []) or "none"
        add(f"Not in either section: unstamped {summary.get('n_unstamped', 0)}  "
            f"other versions {summary.get('n_other', 0)} ({others})")
    stamps = summary.get("stamps") or {}
    if stamps.get("stamp_rows"):
        add(f"TRADE-line version match: {stamps.get('matched', 0)} "
            f"of {stamps.get('stamp_rows', 0)}")
    add(f"Trades with planned risk: {summary['n_with_r']}  "
        f"missing stop or risk: {summary['n_missing_risk']}")
    add(f"Fills tagged estimated (bid or ask fell back to mid): {summary['fill_est']}")
    add("")

    add("EXPECTANCY  (mean fill-adjusted R per closed trade)")
    add(f"  value: {_r(summary['expectancy_r'])}")
    ci = summary.get("expectancy_ci") or {}
    if ci:
        add(f"  day-block 90% interval: {_r(ci.get('low'))} to {_r(ci.get('high'))}")
        seed = ci.get("seed")
        seed_text = f"0x{seed:x}" if isinstance(seed, int) else seed
        add(f"  {ci.get('draws')} resamples, seed {seed_text}, "
            f"{ci.get('sessions')} close-dates, "
            f"{ci.get('trades')} trades with R, percentile 5 and 95.")
        add("  Each resample draws that many close-dates with replacement")
        add("  and takes the mean R of the pooled trades.")
    else:
        add("  day-block 90% interval: n/a")
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
    add(f"  trade sequence: dollars: {_money(summary['max_dd_dollars'])}")
    add(f"  trade sequence: percent of seed: {_pct(summary['max_dd_pct'])}")
    add(f"  daily closes: dollars: {_money(summary.get('max_dd_daily_dollars'))}")
    add(f"  daily closes: percent of seed: {_pct(summary.get('max_dd_daily_pct'))}")
    add(f"  seed: {_money(summary['seed'])}  "
        "(buying power - realized mid + open debit at entry)")
    add(f"  bar:   <= {BARS['max_drawdown_pct']:.0f}% of starting equity")
    add("  The trade sequence steps once per close, in close order.")
    add("  Daily closes step once per Chicago close date, in date order.")
    add("  Open marks are not in either curve. Only closed fill P&L moves them.")
    add("")

    mfe = summary["mfe"]
    add("MFE CAPTURE  (sum of fill P&L / sum of peak dollars, trades with a positive peak)")
    add(f"  ratio: {_num(mfe.get('ratio'))}   median: {_num(mfe.get('median'))}   "
        f"n={mfe['n']}  "
        f"no positive peak={mfe['nonpositive']}  no mfe={mfe['missing']}")
    add(f"  sum pnl_fill {_money(mfe.get('pnl_fill'))}  "
        f"sum peak {_money(mfe.get('peak_dollars'))}")
    add(f"  bar:   >= {BARS['mfe_capture']}")
    add("  Peak is the max of position_marks.pnl_pct and the close mid print,")
    add("  the same inputs as the TRADE mfe_pct field. Marks are mid; P&L is fill.")
    add("  The ratio is one number for the set. The median is the middle per-trade")
    add("  ratio, or the average of the two middle ratios when the count is even.")
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

    add("REGIMES  (measured SESSION lines, spy_close vs spy_open)")
    add("  |change| >= 0.50% is trend up or trend down. Otherwise chop.")
    add("  P&L and mean R are the measured trades closed on those dates.")
    _emit_regimes(add, summary.get("regimes") or {})
    add("  The pre-registered bar is positive in at least 2 of 3 regimes.")
    add("")
    try:
        import shadow_regime
        for line in shadow_regime.weekly_lines(paths.news_db):
            add(line)
    except Exception as regime_err:
        add("SHADOW REGIME  (logged only; not a live rule; not the measured regime table)")
        add(f"  SHADOW regime: unavailable ({type(regime_err).__name__})")
    add("")
    try:
        import shadow_tape
        for line in shadow_tape.weekly_lines(paths.news_db):
            add(line)
    except Exception as tape_err:
        add("SHADOW POST-EXIT  (logged only; not a live rule)")
        add(f"  SHADOW tape: unavailable ({type(tape_err).__name__})")
    add("")

    add("CARRIED VS SAME-DAY  (fill P&L and mean R)")
    add("  Same-day means the Chicago entry date is the Chicago close date.")
    _emit_hold(add, summary.get("hold") or {})
    add("")

    _emit_score_split(
        add,
        f"SCORE SPLIT  ({MEASURED_VERSION}; entry_score 80+ and 70-79)",
        summary.get("buckets") or {},
    )
    add("")
    _emit_score_split(
        add,
        f"{PRIOR_MEASURED_VERSION}  (closed sample, not in the measured sample)",
        summary.get("prior_buckets") or {},
    )
    add("")
    _emit_shadow(add, summary.get("carry_shadow") or [])
    add("")

    _emit_oct2(add, summary.get("oct2") or {}, summary.get("n_oct2", 0))
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

    try:
        from shadow_news import x_spend_lines
        for line in x_spend_lines(paths.news_db):
            add(line)
    except Exception as spend_err:
        add("X SPEND (this Chicago week; posts × $0.005)")
        add(f"  not read ({type(spend_err).__name__})")
        add("  estimated cost: $0.000000")
    add("")
    try:
        from shadow_news import yahoo_week_lines
        for line in yahoo_week_lines(paths.news_db):
            add(line)
    except Exception as yahoo_err:
        add("YAHOO (this Chicago week)")
        add(f"  not read ({type(yahoo_err).__name__})")
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


def _change_pct(val) -> str:
    if val is None:
        return "n/a"
    return f"{val * 100:+.2f}%"


def _dates(bucket: dict) -> str:
    bits = []
    for row in bucket.get("dates") or []:
        if isinstance(row, dict):
            bits.append(f"{row.get('date')} {_change_pct(row.get('change'))}")
        else:
            bits.append(str(row))
    return ", ".join(bits) if bits else "(none)"


def _emit_regimes(add, regimes: dict) -> None:
    labels = (
        ("trend_up", "trend up"),
        ("trend_down", "trend down"),
        ("chop", "chop"),
    )
    for key, label in labels:
        bucket = regimes.get(key) or {}
        add(
            f"  {label}: sessions {bucket.get('sessions', 0)}  "
            f"trades {bucket.get('n', 0)}  "
            f"pnl_fill {_money(bucket.get('pnl_fill'))}  "
            f"mean R {_r(bucket.get('mean_r'))}"
        )
        add(f"    {_dates(bucket)}")
    missing = regimes.get("no_session_line") or {}
    if missing.get("sessions") or missing.get("n"):
        add(
            f"  no SESSION line: dates {missing.get('sessions', 0)}  "
            f"trades {missing.get('n', 0)}  "
            f"pnl_fill {_money(missing.get('pnl_fill'))}  "
            f"mean R {_r(missing.get('mean_r'))}"
        )
        add(f"    {_dates(missing)}")


def _emit_hold(add, hold: dict) -> None:
    for key, label in (("same_day", "same-day"), ("carried", "carried")):
        bucket = hold.get(key) or {}
        add(
            f"  {label}: n={bucket.get('n', 0)}  "
            f"pnl_fill {_money(bucket.get('pnl_fill'))}  "
            f"mean R {_r(bucket.get('mean_r'))}"
        )
    unknown = hold.get("unknown") or {}
    if unknown.get("n"):
        add(
            f"  entry date missing: n={unknown.get('n', 0)}  "
            f"pnl_fill {_money(unknown.get('pnl_fill'))}  "
            f"mean R {_r(unknown.get('mean_r'))}"
        )


def _emit_headlines(add, block: dict) -> None:
    """Every headline, for one score bucket. Bars are printed once above."""
    add(f"  n={block.get('n', 0)}  with R={block.get('n_with_r', 0)}  "
        f"missing risk={block.get('n_missing_risk', 0)}  "
        f"fill_est={block.get('fill_est', 0)}")
    add(f"  expectancy: {_r(block.get('expectancy_r'))}")
    ci = block.get("expectancy_ci") or {}
    if ci:
        seed = ci.get("seed")
        seed_text = f"0x{seed:x}" if isinstance(seed, int) else seed
        add(f"  day-block 90% interval: {_r(ci.get('low'))} to {_r(ci.get('high'))}  "
            f"({ci.get('draws')} draws, seed {seed_text}, "
            f"{ci.get('sessions')} close-dates)")
    else:
        add("  day-block 90% interval: n/a")
    if block.get("n", 0) == 0:
        add("  profit factor: n/a")
    elif block.get("no_losses"):
        add(f"  profit factor: n/a (no losing trades; gross wins {_money(block.get('gross_wins'))})")
    else:
        add(f"  profit factor: {_num(block.get('profit_factor'))}  "
            f"wins {_money(block.get('gross_wins'))}  "
            f"losses {_money(block.get('gross_losses'))}")
    add(f"  max drawdown trade sequence: {_money(block.get('max_dd_dollars'))}  "
        f"{_pct(block.get('max_dd_pct'))}")
    add(f"  max drawdown daily closes: {_money(block.get('max_dd_daily_dollars'))}  "
        f"{_pct(block.get('max_dd_daily_pct'))}")
    mfe = block.get("mfe") or {}
    add(f"  MFE capture: {_num(mfe.get('ratio'))}  median {_num(mfe.get('median'))}  "
        f"n={mfe.get('n', 0)}")
    add(f"  mean entry spread: {_pct(block.get('entry_spread_pct'))}  "
        f"n={block.get('n_entry_spread', 0)}")
    add(f"  mean stop slippage: {_pct(block.get('stop_slippage_pct'))}  "
        f"n={block.get('n_stop_slippage', 0)}")
    reasons = block.get("by_reason") or {}
    if not reasons:
        add("  P&L by exit reason: (none)")
    else:
        for reason in sorted(reasons):
            bucket = reasons[reason]
            mean_r = bucket["r_sum"] / bucket["r_n"] if bucket["r_n"] else None
            add(f"  {reason}: n={int(bucket['n'])}  "
                f"pnl_fill {_money(bucket['pnl_fill'])}  mean R {_r(mean_r)}")
    add(f"  realized fill {_money(block.get('realized_fill'))}  "
        f"mid {_money(block.get('realized_mid'))}  "
        f"spread cost {_money(block.get('spread_cost'))}")
    add("  regimes:")
    _emit_regimes(add, block.get("regimes") or {})
    add("  carried vs same-day:")
    _emit_hold(add, block.get("hold") or {})


def _emit_score_split(add, title: str, buckets: dict) -> None:
    add(title)
    unscored = buckets.get("unscored")
    if unscored:
        add(f"  entry score missing or outside 70+: {unscored}")
    for key, label in (("all", "all trades"), ("80+", "80+"), ("70-79", "70-79")):
        add(f"  {label}")
        _emit_headlines(add, buckets.get(key) or {})


def _emit_shadow(add, rows: list) -> None:
    add("80+ CARRY SHADOW  (logging only; no position was held)")
    if not rows:
        add("  none yet")
        return
    closed = [r for r in rows if r.get("status") == "closed"]
    still = [r for r in rows if r.get("status") == "open"]
    missed = [r for r in rows if r.get("status") == "missed"]
    flat = sum(_num_or_zero(r.get("flatten_pnl")) for r in rows)
    shadow = sum(_num_or_zero(r.get("shadow_pnl")) for r in closed)
    add(f"  flattened 80+ at the close: {len(rows)}")
    add(f"  actual 14:45 flatten pnl_fill: {_money(flat)}")
    add(f"  shadow closed: {len(closed)}  pnl_fill {_money(shadow)}")
    flat_r = [r.get("flatten_r") for r in rows if r.get("flatten_r") is not None]
    sh_r = [r.get("shadow_r") for r in closed if r.get("shadow_r") is not None]
    add(f"  mean flatten R: {_r(_mean(flat_r))}  mean shadow R: {_r(_mean(sh_r))}")
    if still:
        add(f"  still tracking: {len(still)}")
    if missed:
        add(f"  missed session (no fill invented): {len(missed)}")
    for row in rows:
        add(
            f"  {row.get('ticker')} flatten {_money(row.get('flatten_pnl'))} "
            f"({_r(row.get('flatten_r'))}) -> "
            f"{row.get('status')} {row.get('shadow_reason') or ''} "
            f"{_money(row.get('shadow_pnl'))} ({_r(row.get('shadow_r'))})"
        )


def _num_or_zero(val) -> float:
    try:
        return float(val or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mean(values: list) -> float | None:
    nums = []
    for val in values:
        try:
            nums.append(float(val))
        except (TypeError, ValueError):
            continue
    if not nums:
        return None
    return sum(nums) / len(nums)


def _emit_oct2(add, oct2: dict, n: int) -> None:
    add("OCT 2  (version v7a25760, not in the measured sample)")
    add(f"  closed: {n}")
    add(f"  expectancy: {_r(oct2.get('mean_r'))}  "
        f"pnl_fill {_money(oct2.get('pnl_fill'))}")
    spy = (
        f"spy_open {_num(oct2.get('spy_open'))}  "
        f"spy_close {_num(oct2.get('spy_close'))}  "
        f"change {_change_pct(oct2.get('change'))}  "
        f"class {oct2.get('regime') or 'n/a'}"
    )
    add(f"  {spy}")


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
