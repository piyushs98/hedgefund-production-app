"""Local read-only dashboard. Separate process from the bot.

Binds 127.0.0.1:8050 only. Does not import the trading loop and does not
write the databases, the position file, or the logs.

Open http://127.0.0.1:8050 on this Mac after ./start_dashboard.sh
"""

from __future__ import annotations

import html
import json
from datetime import datetime

from flask import Flask, Response

from ops.metrics import (
    BARS,
    MEASURED_VERSION,
    PRIOR_MEASURED_VERSION,
    attach_trade_stamps,
    load_closed_trades,
    quantity_of,
    summarize,
    today_activity,
)
from ops.status import (
    breaker_status,
    file_date_of,
    latest_marks,
    load_active_trades,
    open_debit,
    parse_log,
    position_rows,
    read_bot_log,
)
from ops.store import connect_ro, default_paths, reset_ledger_flag, tail_lines

HOST = "127.0.0.1"
PORT = 8050
REFRESH_SECONDS = 20

app = Flask(__name__)

# System fonts only, so the desk still renders with no network.
_CSS = """
:root {
  --ink: #100e0c;
  --panel: #18160f;
  --cream: #f4ecdc;
  --cream-dim: #b7ad9c;
  --brass: #c4a574;
  --line: rgba(196, 165, 116, 0.32);
  --line-soft: rgba(244, 236, 220, 0.10);
  --up: #d5e0c8;
  --down: #e7c4ba;
  --stale-bg: #3a221c;
  --stale-fg: #f6d7cf;
  --stale-line: #e7b2a4;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html { color-scheme: dark; scroll-behavior: smooth; }
body {
  margin: 0;
  color: var(--cream);
  background:
    radial-gradient(1100px 420px at 8% -12%, rgba(196, 165, 116, 0.16), transparent 58%),
    var(--ink);
  font: 15px/1.5 var(--sans);
}
.shell { width: 100%; max-width: 1180px; margin: 0 auto; padding: 28px 22px 72px; }
.mast > *, .split > *, .figures > *, .bars > *, .ops > *, .stack > * { min-width: 0; }
a { color: var(--cream-dim); text-decoration: none; }
a:hover { color: var(--brass); }
a:focus-visible { outline: 1px solid var(--brass); outline-offset: 3px; }
.mast {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 32px;
  padding-bottom: 22px;
  border-bottom: 1px solid var(--line);
}
.brand { display: flex; gap: 16px; align-items: flex-start; }
.monogram {
  flex: none;
  width: 46px;
  height: 46px;
  margin-top: 4px;
  border: 1px solid var(--brass);
  color: var(--brass);
  font-family: var(--serif);
  font-size: 28px;
  line-height: 44px;
  text-align: center;
}
.kicker {
  margin: 0;
  color: var(--brass);
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
}
h1 {
  margin: 4px 0 0;
  font-family: var(--serif);
  font-weight: 500;
  font-size: 68px;
  line-height: 0.9;
  letter-spacing: -0.03em;
}
.lede, .note { overflow-wrap: anywhere; }
.lede { max-width: 38rem; margin: 12px 0 0; color: var(--cream-dim); }
.mast-side { text-align: right; min-width: 240px; }
.clock {
  margin: 0;
  font-family: var(--serif);
  font-size: 26px;
  line-height: 1.1;
  letter-spacing: -0.02em;
}
.clock-sub { margin: 2px 0 0; color: var(--cream-dim); font-variant-numeric: tabular-nums; }
.pill {
  display: inline-block;
  margin: 12px 0 0;
  padding: 4px 8px;
  border: 1px solid var(--line);
  color: var(--cream-dim);
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.pill.open { color: var(--cream); border-color: var(--brass); }
nav { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 18px; }
nav a {
  font-size: 12px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}
.version {
  margin: 10px 0 0;
  color: var(--brass);
  font-family: var(--mono);
  font-size: 13px;
}
.stale {
  margin: 18px 0 0;
  padding: 14px 16px;
  border: 1px solid var(--stale-line);
  background: var(--stale-bg);
  color: var(--stale-fg);
  font-weight: 650;
}
.figures, .bars, .ops {
  display: grid;
  gap: 14px;
}
.figures { grid-template-columns: repeat(4, 1fr); margin-top: 22px; }
.bars { grid-template-columns: repeat(3, 1fr); margin-top: 16px; }
.figure, .bar, .panel {
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-top: 2px solid var(--brass);
  padding: 16px 16px 14px;
}
.figure-value, .bar-value {
  margin: 10px 0 6px;
  font-family: var(--serif);
  font-size: 34px;
  line-height: 1;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.sub { margin: 0; color: var(--cream-dim); font-size: 13px; }
.sub + .sub { margin-top: 4px; }
.follow { margin-top: 12px; }
.section-label { margin-top: 18px; }
.sub span {
  color: var(--brass);
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.up { color: var(--up); }
.down { color: var(--down); }
.block { margin-top: 42px; padding-top: 22px; border-top: 1px solid var(--line); }
.block-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: baseline;
  margin-bottom: 8px;
}
h2 {
  margin: 0;
  font-family: var(--serif);
  font-weight: 500;
  font-size: 32px;
  letter-spacing: -0.02em;
}
.split {
  display: grid;
  grid-template-columns: minmax(0, 1.75fr) minmax(260px, 0.8fr);
  gap: 18px;
  align-items: start;
}
.table-wrap { max-width: 100%; overflow-x: auto; margin-top: 8px; }
table { width: 100%; border-collapse: collapse; }
th, td {
  padding: 9px 8px;
  border-bottom: 1px solid var(--line-soft);
  text-align: left;
  vertical-align: top;
}
th {
  color: var(--cream-dim);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
td { font-variant-numeric: tabular-nums; }
td.ticker { font-weight: 650; letter-spacing: 0.04em; }
.num { text-align: right; }
.scroll-hint { display: none; margin: 0 0 6px; color: var(--cream-dim); font-size: 12px; }
.empty {
  margin: 8px 0 0;
  padding: 18px 16px;
  border: 1px solid var(--line-soft);
  color: var(--cream-dim);
}
.stack { display: grid; gap: 14px; }
.stat-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
.stat { border-top: 1px solid var(--line-soft); padding-top: 8px; }
.stat b {
  display: block;
  margin-top: 2px;
  font-family: var(--serif);
  font-size: 26px;
  font-weight: 500;
  letter-spacing: -0.02em;
}
.crit { list-style: none; margin: 8px 0 0; padding: 0; }
.crit li {
  margin: 0 0 8px;
  padding: 8px 10px;
  border-left: 2px solid var(--down);
  background: rgba(231, 196, 186, 0.06);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}
.ops { grid-template-columns: repeat(3, 1fr); }
.ops div { border-top: 1px solid var(--line-soft); padding-top: 8px; }
.ops strong {
  display: block;
  margin-top: 4px;
  font-weight: 550;
  font-size: 15px;
}
blockquote {
  margin: 12px 0 0;
  padding: 16px 18px;
  border-left: 2px solid var(--brass);
  background: var(--panel);
  font-family: var(--serif);
  font-size: 22px;
  line-height: 1.35;
}
pre {
  margin: 12px 0 0;
  padding: 14px 16px;
  max-height: 420px;
  overflow: auto;
  background: #0b0a08;
  border: 1px solid var(--line-soft);
  color: var(--cream-dim);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}
.note { margin: 8px 0 0; color: var(--cream-dim); }
.chart-box { position: relative; height: 360px; margin-top: 12px; }
.chart-box canvas { display: block; width: 100%; height: 100%; }
#equity-fallback[hidden],
#portfolio-fallback[hidden],
#portfolio-table[hidden] { display: none; }
#equity-fallback, #portfolio-fallback { margin-top: 8px; color: var(--down); }
#analysis { display: none; }
#analysis:target { display: block; }
#analysis:target + #portfolio { display: none; }
html:not(:has(#analysis:target)) a[href="#portfolio"] { color: var(--brass); }
html:has(#analysis:target) a[href="#analysis"] { color: var(--brass); }
.pv-row {
  display: grid;
  grid-template-columns: 1.4fr 1fr 1fr;
  gap: 22px 28px;
  margin-top: 18px;
}
.pv-value {
  margin: 8px 0 0;
  font-family: var(--serif);
  font-size: 52px;
  line-height: 0.95;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.pv-sub {
  margin: 8px 0 0;
  font-family: var(--serif);
  font-size: 26px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
details.equity-table { margin-top: 14px; }
details.equity-table summary { cursor: pointer; }
tr.open td { color: var(--cream-dim); }
footer {
  margin-top: 36px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
  color: var(--cream-dim);
  font-size: 12px;
}
footer p { margin: 4px 0; word-break: break-word; }
@media (max-width: 980px) {
  .figures, .bars, .ops { grid-template-columns: 1fr 1fr; }
  .pv-row { grid-template-columns: 1fr 1fr; }
  .pv-row article:first-child { grid-column: 1 / -1; }
  .split { grid-template-columns: 1fr; }
  h1 { font-size: 56px; }
  .pv-value { font-size: 44px; }
}
@media (max-width: 680px) {
  .shell { padding: 18px 14px 48px; }
  .mast, .block-head { flex-direction: column; align-items: flex-start; }
  .mast-side { text-align: left; min-width: 0; }
  .figures, .bars, .ops, .stat-row, .pv-row { grid-template-columns: 1fr; }
  .pv-row article:first-child { grid-column: auto; }
  h1 { font-size: 46px; }
  .figure-value, .bar-value, .pv-value { font-size: 36px; }
  .pv-sub { font-size: 22px; }
  .scroll-hint { display: block; }
  table.book { min-width: 760px; }
  .chart-box { height: 280px; }
}
@media print {
  :root {
    --ink: #fff;
    --panel: #fff;
    --cream: #1c1917;
    --cream-dim: #44403c;
    --brass: #6b5428;
    --line: #d6d3d1;
    --line-soft: #e7e5e4;
    --up: #3f6212;
    --down: #9f1239;
    --stale-bg: #fee2e2;
    --stale-fg: #7f1d1d;
    --stale-line: #b91c1c;
  }
  body { background: white; }
  pre { max-height: none; background: white; }
}
"""


def _as_float(val):
    try:
        if val is None or val == "":
            return None
        out = float(val)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _fmt_px(val) -> str:
    num = _as_float(val)
    if num is None:
        return "—"
    return f"{num:.2f}"


def _fmt_money(val) -> str:
    num = _as_float(val)
    if num is None:
        return "—"
    sign = "-" if num < 0 else ""
    return f"{sign}${abs(num):,.2f}"


def _fmt_signed_money(val) -> str:
    num = _as_float(val)
    if num is None:
        return "—"
    if num > 0:
        return "+" + _fmt_money(num)
    return _fmt_money(num)


def _fmt_signed_pct(val) -> str:
    num = _as_float(val)
    if num is None:
        return "—"
    if num == 0:
        return "0.00%"
    return f"{num:+.2f}%"


def _fmt_r(val) -> str:
    num = _as_float(val)
    if num is None:
        return "—"
    return f"{num:+.2f}R"


def _fmt_num(val, digits=2) -> str:
    num = _as_float(val)
    if num is None:
        return "—"
    return f"{num:.{digits}f}"


def _fmt_factor(val) -> str:
    """Keep a small profit factor from rounding to 0.00."""
    num = _as_float(val)
    if num is None:
        return "—"
    text = f"{num:.2f}"
    if text in ("0.00", "-0.00"):
        return f"{num:.4f}"
    return text


def _fmt_pct(val) -> str:
    num = _as_float(val)
    if num is None:
        return "—"
    return f"{num:.2f}%"


def _fmt_dt(val: datetime | None) -> str:
    if val is None:
        return "none in the current log"
    return val.strftime("%Y-%m-%d %H:%M Chicago")


def _fmt_dist(val) -> str:
    num = _as_float(val)
    if num is None:
        return "—"
    return f"{num:+.2f}"


def _fmt_held(minutes) -> str:
    if minutes is None:
        return "—"
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes}m"
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {mins}m"


def _sign_class(val) -> str:
    num = _as_float(val)
    if num is None or num == 0:
        return ""
    return "up" if num > 0 else "down"


def chicago_tz():
    from ops.metrics import CHICAGO
    return CHICAGO


# Fill equity starts here. It is not portfolio_ledger buying power.
SEED_EQUITY = 10_000.0
MEASUREMENT_START = "2026-10-07"


def _closed_for_curve(news_db, logs_dir) -> list[dict]:
    """Every closed trade_history row. Read-only. PAPER_BUY_OPEN is omitted."""
    conn = connect_ro(news_db)
    if conn is None:
        return []
    try:
        rows = load_closed_trades(conn)
    finally:
        conn.close()
    attach_trade_stamps(rows, logs_dir)
    return rows


def _day_version(trades: list[dict]) -> str | None:
    versions = {str(t.get("version")) for t in trades if t.get("version")}
    if not versions:
        return None
    if len(versions) == 1:
        return next(iter(versions))
    return "mixed"


def unrealized_fill_at_bid(trades: list[dict], marks: dict) -> dict:
    """Open fill P&L at the current bid: (bid − entry ask) × 100 × qty."""
    pnl = 0.0
    marked = 0
    skipped = 0
    for trade in trades:
        trade_id = str(trade.get("trade_id") or "")
        mark = marks.get(trade_id) or {}
        bid = _as_float(mark.get("bid"))
        entry_ask = _as_float(trade.get("entry_ask"))
        try:
            qty = int(quantity_of(trade))
        except (TypeError, ValueError):
            qty = 0
        if bid is None or entry_ask is None or qty < 1:
            skipped += 1
            continue
        pnl += (bid - entry_ask) * 100.0 * qty
        marked += 1
    if not marked:
        return {"pnl": None, "marked": 0, "skipped": skipped}
    return {"pnl": round(pnl, 2), "marked": marked, "skipped": skipped}


def equity_curve(closed: list[dict], open_trades: list[dict], marks: dict, today) -> dict:
    """One point per Chicago close date, then a dotted open point when a bid exists.

    Fill equity is the $10,000 start plus cumulative pnl_fill.
    Mid equity is the same start plus cumulative pnl_mid.
    """
    by_day: dict[str, list[dict]] = {}
    for trade in closed:
        day = trade.get("session_date")
        if day is None:
            continue
        by_day.setdefault(day.isoformat(), []).append(trade)
    points = []
    cum_fill = 0.0
    cum_mid = 0.0
    peak = SEED_EQUITY
    prev_version = None
    version_change = None
    for key in sorted(by_day):
        rows = by_day[key]
        day_fill = round(sum((t.get("pnl_fill") or 0.0) for t in rows), 2)
        day_mid = round(sum((t.get("pnl_mid") or 0.0) for t in rows), 2)
        cum_fill = round(cum_fill + day_fill, 2)
        cum_mid = round(cum_mid + day_mid, 2)
        fill = round(SEED_EQUITY + cum_fill, 2)
        peak = round(max(peak, fill), 2)
        version = _day_version(rows)
        if version and prev_version and version != prev_version and version_change is None:
            version_change = {"date": key, "from": prev_version, "to": version}
        if version:
            prev_version = version
        points.append({
            "date": key,
            "label": key,
            "fill": fill,
            "mid": round(SEED_EQUITY + cum_mid, 2),
            "peak": peak,
            "day_fill": day_fill,
            "n": len(rows),
            "open": False,
            "unrealized": None,
            "version": version,
        })
    open_mark = unrealized_fill_at_bid(open_trades, marks)
    if open_mark["pnl"] is not None:
        if not points:
            points.append({
                "date": None,
                "label": "start",
                "fill": SEED_EQUITY,
                "mid": SEED_EQUITY,
                "peak": SEED_EQUITY,
                "day_fill": 0.0,
                "n": 0,
                "open": False,
                "unrealized": None,
                "version": None,
            })
        anchor_fill = points[-1]["fill"]
        anchor_mid = points[-1]["mid"]
        label_day = today.isoformat()
        day_fill = 0.0
        n_today = 0
        if points[-1].get("date") == label_day:
            day_fill = points[-1]["day_fill"]
            n_today = points[-1]["n"]
        points.append({
            "date": label_day,
            "label": f"{label_day} open",
            "fill": round(anchor_fill + open_mark["pnl"], 2),
            "mid": anchor_mid,
            "peak": None,
            "day_fill": day_fill,
            "n": n_today,
            "open": True,
            "unrealized": open_mark["pnl"],
            "marked": open_mark["marked"],
            "skipped": open_mark["skipped"],
            "version": None,
        })
    return {
        "seed": SEED_EQUITY,
        "measurement_start": MEASUREMENT_START,
        "version_change": version_change,
        "open_marked": open_mark["marked"] if open_mark["pnl"] is not None else 0,
        "open_skipped": open_mark["skipped"],
        "points": points,
    }


def build_snapshot(paths=None, now: datetime | None = None) -> dict:
    paths = paths or default_paths()
    now = now or datetime.now(tz=chicago_tz())
    log_path, text = read_bot_log(paths.logs_dir)
    status = parse_log(text, file_date=file_date_of(log_path), now=now)
    trades = load_active_trades(paths.active_trades)
    ids = [str(t.get("trade_id") or "") for t in trades]
    marks = latest_marks(paths.news_db, ids)
    positions = position_rows(trades, marks, now)
    summary = summarize(
        paths.news_db, paths.hedge_db,
        open_debit=open_debit(trades),
        logs_dir=paths.logs_dir,
    )
    day = now.astimezone(chicago_tz()).date()
    today = today_activity(summary, day)
    equity = equity_curve(
        _closed_for_curve(paths.news_db, paths.logs_dir),
        trades,
        marks,
        day,
    )
    tail = tail_lines(log_path, 50) if log_path is not None else []
    return {
        "now": now,
        "log_path": log_path,
        "status": status,
        "breaker": breaker_status(paths.hedge_db),
        "positions": positions,
        "summary": summary,
        "today": today,
        "equity": equity,
        "tail": tail,
        "news_db": paths.news_db,
        "hedge_db": paths.hedge_db,
        "reset_ledger": reset_ledger_flag(paths.env_file),
    }


def _e(val) -> str:
    return html.escape(str(val), quote=True)


def _sample_breakdown(summary: dict) -> str:
    """One line under sample R: measured stamp, Oct 2, and the four gap lots."""
    measured = int(summary.get("n_closed") or 0)
    older = int(summary.get("n_oct2") or 0)
    lots = int(summary.get("n_excluded") or 0)
    return (
        f"Measured: {measured} trades on {MEASURED_VERSION}. "
        f"Excluded: Oct 2 (older version, {older} trades), "
        f"unmonitored Oct 5-6 ({lots} lots)."
    )


def _bucket_cells(block: dict) -> list[str]:
    ci = block.get("expectancy_ci") or {}
    interval = "n/a"
    if ci:
        interval = f"{_fmt_r(ci.get('low'))} to {_fmt_r(ci.get('high'))}"
    mfe = block.get("mfe") or {}
    pf = "n/a" if block.get("no_losses") or block.get("profit_factor") is None else f"{block.get('profit_factor'):.2f}"
    return [
        str(block.get("n", 0)),
        _fmt_r(block.get("expectancy_r")),
        interval,
        pf,
        _fmt_money(block.get("realized_fill")),
        _fmt_money(block.get("max_dd_dollars")),
        _fmt_money(block.get("max_dd_daily_dollars")),
        "n/a" if mfe.get("ratio") is None else f"{mfe.get('ratio'):.2f}",
        "n/a" if block.get("entry_spread_pct") is None else f"{block.get('entry_spread_pct'):.2f}%",
        "n/a" if block.get("stop_slippage_pct") is None else f"{block.get('stop_slippage_pct'):.2f}%",
    ]


def _score_table(title: str, buckets: dict | None) -> str:
    buckets = buckets or {}
    headers = ["", "All", "80+", "70-79"]
    labels = [
        "Trades", "Expectancy", "Day-block 90%", "Profit factor",
        "Fill P&L", "Trade-sequence DD", "Daily DD", "MFE capture",
        "Entry spread", "Stop slippage",
    ]
    cols = [
        _bucket_cells(buckets.get("all") or {}),
        _bucket_cells(buckets.get("80+") or {}),
        _bucket_cells(buckets.get("70-79") or {}),
    ]
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = []
    for i, label in enumerate(labels):
        cells = "".join(f"<td class='num'>{_e(col[i])}</td>" for col in cols)
        body.append(f"<tr><td>{_e(label)}</td>{cells}</tr>")
    note = ""
    unscored = buckets.get("unscored") or 0
    if unscored:
        note = f"<p class='note'>{unscored} trades have no entry score in 70+ and are in All only.</p>"
    return (
        f"<h3 class='kicker section-label'>{_e(title)}</h3>"
        "<div class='table-wrap'><table class='book'><thead><tr>"
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
        + note
    )


def _figure(label: str, value: str, sub: str, value_class: str = "") -> str:
    cls = f" {value_class}" if value_class else ""
    return (
        "<article class='figure'>"
        f"<p class='kicker'>{label}</p>"
        f"<p class='figure-value{cls}'>{value}</p>"
        f"<p class='sub'>{sub}</p>"
        "</article>"
    )


def _bar_card(label: str, value: str, sample: str, bar: str, note: str = "",
              value_class: str = "") -> str:
    cls = f" {value_class}" if value_class else ""
    extra = f"<p class='sub'>{note}</p>" if note else ""
    return (
        "<article class='bar'>"
        f"<p class='kicker'>{label}</p>"
        f"<p class='bar-value{cls}'>{value}</p>"
        f"<p class='sub'><span>Sample</span> {sample}</p>"
        f"<p class='sub'><span>Bar</span> {bar}</p>"
        f"{extra}"
        "</article>"
    )


def _positions_html(rows: list) -> str:
    if not rows:
        return "<p class='empty'>No open positions.</p>"
    body = []
    for row in rows:
        held = row["held"]
        title = "" if held is None else f" title='{_e(str(held) + ' minutes')}'"
        body.append(
            "<tr>"
            f"<td class='ticker'>{_e(row['ticker'])}</td>"
            f"<td>{_e(row['contract'])}</td>"
            f"<td class='num'>{_e(row['qty'])}</td>"
            f"<td class='num'>{_fmt_px(row['entry'])}</td>"
            f"<td class='num'>{_fmt_px(row['mark'])}</td>"
            f"<td class='num {_sign_class(row['pnl'])}'>{_fmt_money(row['pnl'])}</td>"
            f"<td class='num {_sign_class(row['r'])}'>{_fmt_r(row['r'])}</td>"
            f"<td class='num'>{_fmt_dist(row['dist_sl'])}</td>"
            f"<td class='num'>{_fmt_dist(row['dist_tp'])}</td>"
            f"<td class='num'{title}>{_fmt_held(held)}</td>"
            "</tr>"
        )
    return (
        "<p class='scroll-hint'>Scroll sideways for mark, R, and distances.</p>"
        "<div class='table-wrap'><table class='book'><thead><tr>"
        "<th>Ticker</th><th>Contract</th><th class='num'>Qty</th>"
        "<th class='num'>Entry</th><th class='num'>Mark</th>"
        "<th class='num'>P&amp;L</th><th class='num'>R</th>"
        "<th class='num'>To SL</th><th class='num'>To TP</th>"
        "<th class='num'>Held</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
        "<p class='note'>Mark is the latest position_marks mid for that trade id. "
        "P&amp;L is (mark − entry) × 100 × qty. R uses (entry mid − stop) × 100 × qty. "
        "Distance is premium points: mark − stop, and target − mark.</p>"
    )


def _closed_html(trades: list) -> str:
    if not trades:
        return "<p class='empty'>No closes today.</p>"
    body = []
    for trade in trades:
        pnl = trade.get("pnl_fill")
        r_val = trade.get("r_fill")
        body.append(
            "<tr>"
            f"<td class='ticker'>{_e(trade.get('ticker') or '—')}</td>"
            f"<td>{_e(trade.get('reason') or '—')}</td>"
            f"<td class='num {_sign_class(r_val)}'>{_fmt_r(r_val)}</td>"
            f"<td class='num {_sign_class(pnl)}'>{_fmt_money(pnl)}</td>"
            "</tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Ticker</th><th>Exit</th><th class='num'>R</th>"
        "<th class='num'>Fill P&amp;L</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _reasons_html(by_reason: dict) -> str:
    if not by_reason:
        return "<p class='empty'>No closed trades in the sample.</p>"
    body = []
    for reason in sorted(by_reason):
        bucket = by_reason[reason]
        mean_r = None
        if bucket.get("r_n"):
            mean_r = bucket["r_sum"] / bucket["r_n"]
        body.append(
            "<tr>"
            f"<td>{_e(reason)}</td>"
            f"<td class='num'>{int(bucket.get('n') or 0)}</td>"
            f"<td class='num {_sign_class(bucket.get('pnl_fill'))}'>"
            f"{_fmt_money(bucket.get('pnl_fill'))}</td>"
            f"<td class='num {_sign_class(mean_r)}'>{_fmt_r(mean_r)}</td>"
            "</tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Exit</th><th class='num'>N</th>"
        "<th class='num'>Fill P&amp;L</th><th class='num'>Mean R</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _regime_value(regimes: dict) -> str:
    bits = []
    for key, label in (("trend_up", "up"), ("trend_down", "down"), ("chop", "chop")):
        n = (regimes.get(key) or {}).get("sessions") or 0
        if n:
            bits.append(f"{label} {n}")
    return " · ".join(bits) if bits else "Not stored"


def _regime_sample(regimes: dict) -> str:
    if _regime_value(regimes) == "Not stored":
        return "Not in the persistent tables."
    return "SESSION spy_open and spy_close. |change| ≥ 0.50% is a trend."


def _sample_cards(summary: dict) -> str:
    mfe = summary.get("mfe") or {}
    n_closed = summary.get("n_closed") or 0
    if n_closed == 0:
        pf_sample = "No closed trades."
    elif summary.get("no_losses"):
        pf_sample = (
            "No losing trades. Gross wins "
            f"{_fmt_money(summary.get('gross_wins'))}."
        )
    else:
        pf_sample = (
            f"Wins {_fmt_money(summary.get('gross_wins'))} · "
            f"losses {_fmt_money(summary.get('gross_losses'))}."
        )
    if n_closed == 0:
        pf_value = "—"
    elif summary.get("no_losses"):
        pf_value = "n/a"
    else:
        pf_value = _fmt_factor(summary.get("profit_factor"))
    slip_sample = (
        f"n={summary.get('n_stop_slippage') or 0} stop exits · "
        f"entry spread {_fmt_pct(summary.get('entry_spread_pct'))} "
        f"(n={summary.get('n_entry_spread') or 0})"
    )
    return "".join([
        _bar_card(
            "Expectancy",
            _fmt_r(summary.get("expectancy_r")),
            f"{summary.get('n_with_r') or 0} trades with planned risk",
            f"≥ {BARS['expectancy_r']:+.2f}R over {BARS['min_trades']}+ trades",
            value_class=_sign_class(summary.get("expectancy_r")),
        ),
        _bar_card(
            "Profit factor",
            pf_value,
            pf_sample,
            f"≥ {BARS['profit_factor']}",
        ),
        _bar_card(
            "Max drawdown",
            _fmt_pct(summary.get("max_dd_pct")),
            (
                f"{_fmt_money(summary.get('max_dd_dollars'))} trade sequence · "
                f"daily closes {_fmt_pct(summary.get('max_dd_daily_pct'))} · "
                f"seed {_fmt_money(summary.get('seed'))}"
            ),
            f"≤ {BARS['max_drawdown_pct']:.0f}% of starting equity",
        ),
        _bar_card(
            "MFE capture",
            _fmt_num(mfe.get("ratio")),
            (
                f"median {_fmt_num(mfe.get('median'))} · "
                f"n={mfe.get('n', 0)} · no positive peak {mfe.get('nonpositive', 0)} "
                f"· no mfe {mfe.get('missing', 0)}"
            ),
            f"≥ {BARS['mfe_capture']}",
            value_class=_sign_class(mfe.get("ratio")),
        ),
        _bar_card(
            "Stop slippage",
            _fmt_pct(summary.get("stop_slippage_pct")),
            slip_sample,
            f"≤ {BARS['stop_slippage_pct']:.0f}% average, and no coin_flip labels",
            note="coin_flip count: unavailable.",
        ),
        _bar_card(
            "Regimes",
            _regime_value(summary.get("regimes") or {}),
            _regime_sample(summary.get("regimes") or {}),
            "Positive in at least 2 of 3 regimes.",
        ),
    ])


def _open_figure(rows: list) -> str:
    marked = [row for row in rows if row.get("pnl") is not None]
    if not rows:
        return _figure("Open mark P&amp;L", "—", "No open positions.")
    if not marked:
        return _figure(
            "Open mark P&amp;L",
            "—",
            f"{len(rows)} open, no mark yet.",
        )
    total = sum(row["pnl"] for row in marked)
    return _figure(
        "Open mark P&amp;L",
        _fmt_money(total),
        f"{len(marked)} of {len(rows)} lots marked. Unrealized.",
        _sign_class(total),
    )


def _session_line(summary: dict) -> str:
    sessions = summary.get("sessions") or {}
    counted = list(sessions.get("counted") or [])
    excluded = list(sessions.get("excluded") or [])
    if len(counted) <= 8:
        counted_text = ", ".join(counted) if counted else "none"
    else:
        counted_text = f"{counted[0]} through {counted[-1]}"
    return (
        f"Telemetry days counted: {len(counted)} ({_e(counted_text)}). "
        f"Excluded for data outage: {len(excluded)}."
    )


_CHART_SRC = "https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"

# Chart.js is deferred so a slow CDN cannot block the other panels.
# The inline boot waits for DOMContentLoaded, after that tag has run.
# An inline defer attribute is ignored, so the boot must not run at parse time.
_EQUITY_BOOT = r"""
(function () {
  function boot() {
  var note = document.getElementById("equity-fallback");
  var node = document.getElementById("equity-data");
  var canvas = document.getElementById("equity-chart");
  function showFallback() {
    if (note) note.hidden = false;
  }
  if (!node || !canvas) {
    showFallback();
    return;
  }
  var data;
  try {
    data = JSON.parse(node.textContent || "");
  } catch (err) {
    showFallback();
    return;
  }
  if (!window.Chart) {
    showFallback();
    return;
  }
  var points = data.points || [];
  if (!points.length) return;
  function money(val) {
    var num = Number(val);
    if (!isFinite(num)) return "\u2014";
    var sign = num < 0 ? "-" : "";
    var body = Math.abs(num).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
    return sign + "$" + body;
  }
  function values(key, skipOpen) {
    return points.map(function (p) {
      if (skipOpen && p.open) return null;
      var v = p[key];
      return v === null || v === undefined ? null : v;
    });
  }
  function xAt(chart, idx) {
    var count = (chart.data.datasets || []).length;
    var d;
    for (d = 0; d < count; d++) {
      var meta = chart.getDatasetMeta(d);
      var el = meta && meta.data && meta.data[idx];
      if (el && isFinite(el.x)) return el.x;
    }
    return null;
  }
  var hasOpen = points.some(function (p) { return !!p.open; });
  var openValues = points.map(function (p, i) {
    if (p.open) return p.fill;
    var next = points[i + 1];
    if (next && next.open) return p.fill;
    return null;
  });
  var datasets = [
    {
      label: "Peak",
      data: values("peak", true),
      borderColor: "transparent",
      backgroundColor: "transparent",
      pointRadius: 0,
      pointHoverRadius: 0,
      borderWidth: 0,
      spanGaps: false,
      fill: false
    },
    {
      label: "Fill equity",
      data: values("fill", true),
      borderColor: "#f4ecdc",
      backgroundColor: "rgba(231, 196, 186, 0.28)",
      pointBackgroundColor: "#f4ecdc",
      pointRadius: 3,
      pointHoverRadius: 5,
      borderWidth: 2,
      tension: 0,
      spanGaps: false,
      fill: "-1"
    },
    {
      label: "Mid equity",
      data: values("mid", true),
      borderColor: "rgba(183, 173, 156, 0.55)",
      backgroundColor: "transparent",
      pointBackgroundColor: "rgba(183, 173, 156, 0.55)",
      pointRadius: 2,
      pointHoverRadius: 4,
      borderWidth: 1.5,
      tension: 0,
      spanGaps: false,
      fill: false
    },
    {
      label: "$10,000 start",
      data: points.map(function () { return data.seed; }),
      borderColor: "rgba(196, 165, 116, 0.8)",
      backgroundColor: "transparent",
      pointRadius: 0,
      pointHoverRadius: 0,
      borderWidth: 1,
      borderDash: [6, 4],
      tension: 0,
      spanGaps: false,
      fill: false
    }
  ];
  if (hasOpen) {
    datasets.push({
      label: "Open at bid",
      data: openValues,
      borderColor: "#f4ecdc",
      backgroundColor: "transparent",
      pointBackgroundColor: "#f4ecdc",
      pointRadius: points.map(function (p) { return p.open ? 4 : 0; }),
      pointHoverRadius: points.map(function (p) { return p.open ? 6 : 0; }),
      borderWidth: 1.5,
      borderDash: [2, 3],
      tension: 0,
      spanGaps: false,
      fill: false
    });
  }
  var marker = {
    id: "equityMarkers",
    afterDraw: function (chart) {
      var area = chart.chartArea;
      if (!area) return;
      var labels = chart.data.labels || [];
      var ctx = chart.ctx;
      var startIdx = labels.indexOf(data.measurement_start);
      var change = data.version_change;
      ctx.save();
      ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
      ctx.fillStyle = "#c4a574";
      ctx.strokeStyle = "rgba(196, 165, 116, 0.9)";
      ctx.lineWidth = 1;
      if (startIdx >= 0) {
        var x = xAt(chart, startIdx);
        if (x !== null) {
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(x, area.top);
          ctx.lineTo(x, area.bottom);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillText("measurement start", x + 6, area.top + 14);
        }
      }
      if (change && change.date) {
        var vIdx = labels.indexOf(change.date);
        var vx = vIdx >= 0 ? xAt(chart, vIdx) : null;
        if (vx !== null) {
          var y = area.top + (vIdx === startIdx ? 28 : 14);
          ctx.fillText(String(change.from || "") + " \u2192 " + String(change.to || ""), vx + 6, y);
        }
      }
      ctx.restore();
    }
  };
  try {
    new window.Chart(canvas, {
      type: "line",
      plugins: [marker],
      data: {
        labels: points.map(function (p) { return p.label; }),
        datasets: datasets
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: "bottom",
            labels: {
              color: "#b7ad9c",
              boxWidth: 18,
              filter: function (item) { return item.text !== "Peak"; }
            }
          },
          tooltip: {
            backgroundColor: "#18160f",
            titleColor: "#f4ecdc",
            bodyColor: "#f4ecdc",
            borderColor: "rgba(196, 165, 116, 0.45)",
            borderWidth: 1,
            displayColors: false,
            filter: function (item) {
              return item.dataset.label === "Fill equity" || item.dataset.label === "Open at bid";
            },
            callbacks: {
              title: function (items) {
                if (!items.length) return "";
                var p = points[items[0].dataIndex];
                return p ? String(p.label || "") : "";
              },
              label: function () { return null; },
              afterBody: function (items) {
                if (!items.length) return [];
                var p = points[items[0].dataIndex];
                if (!p) return [];
                var lines = [
                  "Fill equity " + money(p.fill),
                  "Mid equity " + money(p.mid),
                  "Day fill P&L " + money(p.day_fill),
                  "Trades " + String(p.n)
                ];
                if (p.open) lines.push("Unrealized at bid " + money(p.unrealized));
                return lines;
              }
            }
          }
        },
        scales: {
          x: {
            ticks: { color: "#b7ad9c", maxRotation: 0, autoSkip: true },
            grid: { color: "rgba(244, 236, 220, 0.08)" }
          },
          y: {
            ticks: {
              color: "#b7ad9c",
              callback: function (value) { return money(value); }
            },
            grid: { color: "rgba(244, 236, 220, 0.08)" }
          }
        }
      }
    });
  } catch (err) {
    showFallback();
  }
  requestAnimationFrame(function () {
    var chart = window.Chart && Chart.getChart("equity-chart");
    if (chart) chart.resize();
  });
  window.addEventListener("hashchange", function () {
    var chart = window.Chart && Chart.getChart("equity-chart");
    if (chart) chart.resize();
  });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
"""

_PORTFOLIO_BOOT = r"""
(function () {
  function boot() {
    var note = document.getElementById("portfolio-fallback");
    var table = document.getElementById("portfolio-table");
    var node = document.getElementById("portfolio-data");
    var canvas = document.getElementById("portfolio-chart");
    function showFallback() {
      if (note) note.hidden = false;
      if (table) table.hidden = false;
    }
    if (!canvas) return;
    if (!node) {
      showFallback();
      return;
    }
    var data;
    try {
      data = JSON.parse(node.textContent || "");
    } catch (err) {
      showFallback();
      return;
    }
    if (!window.Chart) {
      showFallback();
      return;
    }
    var points = data.points || [];
    if (!points.length) return;
    function money(val) {
      var num = Number(val);
      if (!isFinite(num)) return "\u2014";
      var sign = num < 0 ? "-" : "";
      var body = Math.abs(num).toLocaleString("en-US", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      });
      return sign + "$" + body;
    }
    try {
      new window.Chart(canvas, {
        type: "line",
        data: {
          labels: points.map(function (p) { return p.label; }),
          datasets: [{
            label: "Portfolio value",
            data: points.map(function (p) { return p.value; }),
            borderColor: "#f4ecdc",
            backgroundColor: "transparent",
            pointBackgroundColor: "#f4ecdc",
            pointRadius: 3,
            pointHoverRadius: 5,
            borderWidth: 2,
            tension: 0,
            spanGaps: false,
            fill: false
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: "#18160f",
              titleColor: "#f4ecdc",
              bodyColor: "#f4ecdc",
              borderColor: "rgba(196, 165, 116, 0.45)",
              borderWidth: 1,
              displayColors: false,
              callbacks: {
                title: function (items) {
                  if (!items.length) return "";
                  var p = points[items[0].dataIndex];
                  return p ? String(p.label || "") : "";
                },
                label: function (item) { return money(item.parsed.y); }
              }
            }
          },
          scales: {
            x: {
              ticks: { color: "#b7ad9c", maxRotation: 0, autoSkip: true },
              grid: { color: "rgba(244, 236, 220, 0.08)" }
            },
            y: {
              ticks: {
                color: "#b7ad9c",
                callback: function (value) { return money(value); }
              },
              grid: { color: "rgba(244, 236, 220, 0.08)" }
            }
          }
        }
      });
    } catch (err) {
      showFallback();
    }
    requestAnimationFrame(function () {
      var chart = window.Chart && Chart.getChart("portfolio-chart");
      if (chart) chart.resize();
    });
    window.addEventListener("hashchange", function () {
      var chart = window.Chart && Chart.getChart("portfolio-chart");
      if (chart) chart.resize();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
"""


def _json_for_html(payload) -> str:
    text = json.dumps(payload, separators=(",", ":"))
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _lot_phrase(n: int) -> str:
    return "1 open lot" if n == 1 else f"{n} open lots"


def portfolio_now(equity: dict, today) -> dict:
    """Portfolio value is fill cash plus open positions at the current bid.

    Each past close is $10,000 plus cumulative pnl_fill. Today's point adds
    the open lots at bid. Cash is that value minus the bid marks.
    """
    today_key = today.isoformat() if hasattr(today, "isoformat") else str(today or "")
    simple = []
    for point in equity.get("points") or []:
        if point.get("open"):
            row = {
                "date": point.get("date"),
                "label": str(point.get("date") or ""),
                "value": point.get("fill"),
            }
            if simple and simple[-1]["date"] == row["date"]:
                simple[-1]["value"] = row["value"]
            elif row["date"]:
                simple.append(row)
            continue
        if not point.get("date"):
            continue
        simple.append({
            "date": point.get("date"),
            "label": point.get("label") or point.get("date"),
            "value": point.get("fill"),
        })
    if simple:
        value = simple[-1]["value"]
        last_date = simple[-1]["date"]
    else:
        value = SEED_EQUITY
        last_date = None
    if last_date == today_key:
        prior = simple[-2]["value"] if len(simple) > 1 else SEED_EQUITY
    else:
        prior = value
    value = round(float(value), 2)
    prior = round(float(prior), 2)
    today_change = round(value - prior, 2)
    since = round(value - SEED_EQUITY, 2)
    today_pct = (today_change / prior * 100.0) if prior else None
    since_pct = since / SEED_EQUITY * 100.0
    return {
        "value": value,
        "today_change": today_change,
        "today_pct": today_pct,
        "since_change": since,
        "since_pct": since_pct,
        "points": simple,
    }


def _headline(label: str, amount: str, sub: str = "", value_class: str = "") -> str:
    cls = f" {value_class}" if value_class else ""
    under = f"<p class='pv-sub{_sign_class_attr(sub)}'>{sub}</p>" if sub else ""
    return (
        "<article>"
        f"<p class='kicker'>{label}</p>"
        f"<p class='pv-value{cls}'>{amount}</p>"
        f"{under}"
        "</article>"
    )


def _sign_class_attr(text: str) -> str:
    if text.startswith("+"):
        return " up"
    if text.startswith("-"):
        return " down"
    return ""


def _portfolio_section(stats: dict) -> str:
    points = list(stats.get("points") or [])
    value_cls = _sign_class((stats.get("value") or 0) - SEED_EQUITY)
    change_cls = _sign_class(stats.get("today_change"))
    since_cls = _sign_class(stats.get("since_change"))
    heads = (
        "<div class='pv-row'>"
        + _headline("Portfolio value now", _fmt_money(stats.get("value")), value_class=value_cls)
        + _headline(
            "Today's change",
            _fmt_signed_money(stats.get("today_change")),
            _fmt_signed_pct(stats.get("today_pct")),
            change_cls,
        )
        + _headline(
            "Since start",
            _fmt_signed_money(stats.get("since_change")),
            _fmt_signed_pct(stats.get("since_pct")),
            since_cls,
        )
        + "</div>"
    )
    if not points:
        chart = "<p class='empty'>No closed trades yet.</p>"
    else:
        rows = []
        for point in points:
            rows.append(
                "<tr>"
                f"<td>{_e(point.get('label') or '—')}</td>"
                f"<td class='num {_sign_class((point.get('value') or 0) - SEED_EQUITY)}'>"
                f"{_fmt_money(point.get('value'))}</td>"
                "</tr>"
            )
        chart = (
            "<div class='chart-box'>"
            "<canvas id='portfolio-chart' aria-label='Portfolio value'></canvas>"
            "</div>"
            "<p id='portfolio-fallback' hidden>The chart library did not load. "
            "The table is this series.</p>"
            "<div id='portfolio-table' hidden><div class='table-wrap'><table><thead><tr>"
            "<th>Date</th><th class='num'>Portfolio value</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div></div>"
        )
    return (
        "<section class='block' id='portfolio-value'>"
        + heads
        + chart
        + "</section>"
    )


def _equity_section(equity: dict) -> str:
    points = list(equity.get("points") or [])
    rows = []
    for point in points:
        label = point.get("label") or "—"
        open_row = bool(point.get("open"))
        row_cls = " class='open'" if open_row else ""
        title = " title='Unrealized at the current bid'" if open_row else ""
        rows.append(
            f"<tr{row_cls}{title}>"
            f"<td>{_e(label)}</td>"
            f"<td class='num {_sign_class((point.get('fill') or 0) - SEED_EQUITY)}'>"
            f"{_fmt_money(point.get('fill'))}</td>"
            f"<td class='num {_sign_class((point.get('mid') or 0) - SEED_EQUITY)}'>"
            f"{_fmt_money(point.get('mid'))}</td>"
            f"<td class='num {_sign_class(point.get('day_fill'))}'>"
            f"{_fmt_money(point.get('day_fill'))}</td>"
            f"<td class='num'>{int(point.get('n') or 0)}</td>"
            "</tr>"
        )
    if points:
        table = (
            "<details class='equity-table' open>"
            "<summary class='kicker'>Daily closes</summary>"
            "<div class='table-wrap'><table><thead><tr>"
            "<th>Date</th><th class='num'>Fill equity</th>"
            "<th class='num'>Mid equity</th><th class='num'>Day fill P&amp;L</th>"
            "<th class='num'>Trades</th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div></details>"
        )
        chart = (
            "<div class='chart-box'>"
            "<canvas id='equity-chart' "
            "aria-label='Daily portfolio value, fill and mid'></canvas>"
            "</div>"
        )
    else:
        table = ""
        chart = "<p class='empty'>No closed trades yet.</p>"
    change = equity.get("version_change") or None
    change_note = ""
    if change:
        change_note = (
            "<p class='note'>Version change on "
            f"{_e(change.get('date'))}: {_e(change.get('from'))} → {_e(change.get('to'))}.</p>"
        )
    marked = int(equity.get("open_marked") or 0)
    skipped = int(equity.get("open_skipped") or 0)
    open_note = ""
    if marked:
        open_note = (
            f"<p class='note'>The dotted segment marks {_lot_phrase(marked)} "
            "at the current bid. That segment is unrealized.</p>"
        )
    if skipped:
        where = "the dotted segment" if marked else "this chart"
        open_note += (
            f"<p class='note'>{_lot_phrase(skipped).capitalize()} "
            f"{'is' if skipped == 1 else 'are'} off {where} because a bid "
            "or an entry ask is missing.</p>"
        )
    return (
        "<section class='block' id='analysis'>"
        "<div id='equity'>"
        "<div class='block-head'><h2>Analysis</h2>"
        "<p class='kicker'>Fill and mid</p></div>"
        "<p class='note'>One point per Chicago close. Fill equity is $10,000 "
        "plus cumulative pnl_fill. The fainter line is $10,000 plus cumulative "
        "pnl_mid. The dashed line is the $10,000 start. Shading is the fill "
        "line under its running peak, and that peak starts at $10,000. "
        "Measurement start is 2026-10-07.</p>"
        + change_note
        + open_note
        + "<p id='equity-fallback' hidden>The chart library did not load. "
        "The table is the same series, and the other panels on this page stay up.</p>"
        "<noscript><p class='note'>The chart script did not run. "
        "The table is the series.</p></noscript>"
        + chart
        + table
        + "</div></section>"
    )


def _chart_scripts(equity: dict, stats: dict) -> str:
    parts = []
    simple = list(stats.get("points") or [])
    detailed = list(equity.get("points") or [])
    if simple:
        parts.append(
            "<script type=\"application/json\" id=\"portfolio-data\">"
            + _json_for_html({"points": simple})
            + "</script>"
        )
    if detailed:
        parts.append(
            "<script type=\"application/json\" id=\"equity-data\">"
            + _json_for_html({
                "seed": equity.get("seed", SEED_EQUITY),
                "measurement_start": equity.get("measurement_start", MEASUREMENT_START),
                "version_change": equity.get("version_change"),
                "points": detailed,
            })
            + "</script>"
        )
    if not parts:
        return ""
    boots = []
    if simple:
        boots.append("<script id=\"portfolio-boot\">\n" + _PORTFOLIO_BOOT + "</script>")
    if detailed:
        boots.append("<script id=\"equity-boot\">\n" + _EQUITY_BOOT + "</script>")
    return (
        "\n".join(parts)
        + f'\n<script src="{_CHART_SRC}" defer></script>\n'
        + "\n".join(boots)
        + "\n"
    )


# Static copy must not include the staleness phrases. Those are emitted
# only from status["stale_text"], which the tests look for by exact words.
def render_page(snap: dict) -> str:
    status = snap["status"]
    now = snap["now"].astimezone(chicago_tz())
    summary = snap.get("summary") or {}
    today = snap["today"]
    ledger = summary.get("ledger") or {}
    window = (
        "Full-scan window is open (weekdays 08:45–15:00 Chicago)."
        if status["market_open"]
        else "Outside the weekday 08:45–15:00 Chicago full-scan window."
    )
    if status["holiday"]:
        window = "Log says the NYSE is closed today. Staleness check is off."
    pill_class = "pill open" if status["market_open"] else "pill"
    if status["holiday"]:
        pill = "NYSE closed"
    elif status["market_open"]:
        pill = "Full-scan window"
    else:
        pill = "Outside the window"

    stale = ""
    if status["stale"]:
        stale = "<div class='stale'>" + _e(status["stale_text"]) + "</div>"

    crit_rows = status["criticals"]
    if crit_rows:
        crit_html = (
            "<ul class='crit'>"
            + "".join(f"<li>{_e(line)}</li>" for line in crit_rows)
            + "</ul>"
        )
    else:
        crit_html = "<p class='empty'>None in this log.</p>"

    gate = status["gate"] or "none in the current log"
    log_name = snap["log_path"].name if snap["log_path"] else "no bot log"
    tail = "\n".join(snap["tail"]) if snap["tail"] else "(log empty)"
    day = now.date()
    date_line = f"{now.strftime('%A')} {now.day} {now.strftime('%B %Y')}"
    clock_line = now.strftime("%H:%M:%S")
    n_closed = summary.get("n_closed") or 0
    sample_meta = (
        f"Closed in the sample: {n_closed}. "
        f"Excluded from measurement (unmonitored Oct 5–6): "
        f"{summary.get('n_excluded') or 0}. "
        f"Estimated fills: {summary.get('fill_est') or 0}. "
        f"Missing stop or risk: {summary.get('n_missing_risk') or 0}."
    )
    ledger_line = (
        f"Ledger realized fill {_fmt_money(ledger.get('total_realized_pnl_fill'))} · "
        f"realized mid {_fmt_money(ledger.get('total_realized_pnl'))}."
    )
    if ledger.get("updated_at"):
        ledger_line += f" Updated {_e(ledger.get('updated_at'))}."
    seed_note = (
        "Drawdown seed is buying power minus realized mid plus open debit at entry. "
        "Open marks are not on that curve. Spread cost is realized mid minus realized fill. "
        "The ledger cash series still moves on mid."
    )
    equity = snap.get("equity") or {}
    stats = portfolio_now(equity, day)
    portfolio_html = _portfolio_section(stats)
    analysis_html = _equity_section(equity)
    chart_scripts = _chart_scripts(equity, stats)

    figures = "".join([
        _figure(
            "PAPER CASH (MID BASIS)",
            _fmt_money(ledger.get("buying_power")),
            "Counts trades at mid, so it's overstated by the spread paid. "
            "Portfolio value above is the real figure.",
        ),
        _open_figure(snap["positions"]),
        _figure(
            "Today, realized",
            _fmt_money(today["realized_fill"]),
            (
                f"{today['entries']} entries · {today['closes']} closes · "
                f"spread {_fmt_money(today['spread_cost'])}"
            ),
            _sign_class(today["realized_fill"]),
        ),
        _figure(
            "Sample expectancy",
            _fmt_r(summary.get("expectancy_r")),
            _sample_breakdown(summary),
            _sign_class(summary.get("expectancy_r")),
        ),
    ])

    desk = (
        "<div class='ops'>"
        f"<div><span class='kicker'>Version</span><strong>{_e(status['version'])}</strong></div>"
        f"<div><span class='kicker'>Last scan</span><strong>{_e(_fmt_dt(status['last_scan']))}</strong></div>"
        f"<div><span class='kicker'>Last exit pass</span><strong>{_e(_fmt_dt(status['last_exit']))}</strong></div>"
        f"<div><span class='kicker'>Circuit breaker</span><strong>{_e(snap['breaker'])}</strong></div>"
        f"<div><span class='kicker'>Yahoo crumb</span><strong class='{_sign_class(-1 if status['crumb_hot'] else 0)}'>{_e(status['crumb'])}</strong></div>"
        f"<div><span class='kicker'>Ledger reset flag</span><strong>RESET_LEDGER_ON_BOOT={_e(snap.get('reset_ledger') or 'missing')}</strong></div>"
        "</div>"
    )

    body = f"""
<header class="mast">
  <div>
    <div class="brand">
      <div class="monogram" aria-hidden="true">B</div>
      <div>
        <p class="kicker">Paper book · private desk</p>
        <h1>The Book</h1>
      </div>
    </div>
    <p class="lede">Paper options on this Mac. Read only. This process is not the bot.
    Not client capital, and not a registered adviser.</p>
    <p class="lede">{_e(window)} Refreshes every {REFRESH_SECONDS}s.</p>
    <nav>
      <a href="#portfolio">Portfolio</a>
      <a href="#analysis">Analysis</a>
      <a href="#book">Book</a>
      <a href="#sample">Sample</a>
      <a href="#desk">Desk</a>
      <a href="#tape">Tape</a>
    </nav>
  </div>
  <div class="mast-side">
    <p class="clock">{_e(date_line)}</p>
    <p class="clock-sub">{_e(clock_line)} Chicago</p>
    <p class="{pill_class}">{_e(pill)}</p>
    <p class="version">{_e(status["version"])}</p>
  </div>
</header>
{stale}
{analysis_html}
<div id="portfolio">
{portfolio_html}
<section class="figures" aria-label="Book figures">
{figures}
</section>
<section class="block" id="book">
  <div class="block-head">
    <h2>Open book</h2>
    <p class="kicker">{len(snap["positions"])} open</p>
  </div>
  <div class="split">
    <div>
      {_positions_html(snap["positions"])}
    </div>
    <aside class="stack">
      <article class="panel">
        <p class="kicker">Today · {_e(str(day))}</p>
        <div class="stat-row">
          <div class="stat"><span class="kicker">Entries</span><b>{today["entries"]}</b></div>
          <div class="stat"><span class="kicker">Closes</span><b>{today["closes"]}</b></div>
        </div>
        <p class="sub follow">Realized fill {_fmt_money(today["realized_fill"])}.
        Spread cost {_fmt_money(today["spread_cost"])}.</p>
        <h3 class="kicker section-label">Closed today</h3>
        {_closed_html(today["closed"])}
      </article>
      <article class="panel">
        <p class="kicker">CRITICAL lines in this log</p>
        {crit_html}
        <p class="note">These are log lines. The session counter counts explicit CRITICAL sends.</p>
      </article>
    </aside>
  </div>
</section>
<section class="block" id="sample">
  <div class="block-head">
    <h2>Sample against the bars</h2>
    <p class="kicker">Not a go-live call</p>
  </div>
  <p class="note">Pre-registered bars, printed beside this sample. They do not size a trade, move a stop, or decide that real money is warranted.</p>
  <div class="bars">
    {_sample_cards(summary)}
  </div>
  {_score_table(f"Score split · {MEASURED_VERSION}", summary.get("buckets"))}
  {_score_table(f"Closed sample · {PRIOR_MEASURED_VERSION}", summary.get("prior_buckets"))}
  <p class="note">{_e(sample_meta)}</p>
  <p class="note">{ledger_line}</p>
  <p class="note">{seed_note}</p>
  <p class="note">{_session_line(summary)}</p>
  <h3 class="kicker section-label">By exit</h3>
  {_reasons_html(summary.get("by_reason") or {})}
</section>
<section class="block" id="desk">
  <div class="block-head">
    <h2>Desk</h2>
    <p class="kicker">This machine</p>
  </div>
  {desk}
  <h3 class="kicker section-label">Latest gate</h3>
  <blockquote>{_e(gate)}</blockquote>
</section>
<section class="block" id="tape">
  <div class="block-head">
    <h2>Tape</h2>
    <p class="kicker">{_e(log_name)} · last {len(snap["tail"])} lines</p>
  </div>
  <pre>{_e(tail)}</pre>
</section>
</div>
{chart_scripts}
<footer>
  <p>Databases opened read-only: {_e(snap["news_db"])} and {_e(snap["hedge_db"])}.</p>
  <p>Listening on 127.0.0.1:{PORT} only. Discord is the durable store. A restart of this page does not restart the bot.</p>
  <p><a href="#book">Back to the book</a></p>
</footer>
"""
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<meta http-equiv=\"refresh\" content=\"{REFRESH_SECONDS}\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"description\" content=\"Private paper desk. Read only. This machine only.\">\n"
        "<title>The Book · Paper desk</title>\n"
        "<style>\n" + _CSS + "</style>\n"
        "</head>\n<body>\n<div class=\"shell\">\n"
        + body
        + "</div>\n</body>\n</html>\n"
    )


@app.get("/")
def index():
    page = render_page(build_snapshot())
    return Response(page, mimetype="text/html", headers={"Cache-Control": "no-store"})


def main() -> None:
    print(
        f"Local dashboard at http://{HOST}:{PORT} "
        "(this machine only). Read-only. Ctrl-C stops this process, not the bot."
    )
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
