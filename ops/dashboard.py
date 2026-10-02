"""Local read-only dashboard. Separate process from the bot.

Binds 127.0.0.1:8050 only. Does not import the trading loop and does not
write the databases, the position file, or the logs.

Open http://127.0.0.1:8050 on this Mac after ./start_dashboard.sh
"""

from __future__ import annotations

import html
from datetime import datetime

from flask import Flask, Response

from ops.metrics import summarize, today_activity
from ops.status import (
    breaker_status,
    file_date_of,
    latest_marks,
    load_active_trades,
    parse_log,
    position_rows,
    read_bot_log,
)
from ops.store import default_paths, tail_lines

HOST = "127.0.0.1"
PORT = 8050
REFRESH_SECONDS = 20

app = Flask(__name__)


def _fmt_px(val) -> str:
    if val is None:
        return "—"
    return f"{val:.2f}"


def _fmt_money(val) -> str:
    if val is None:
        return "—"
    return f"${val:,.2f}"


def _fmt_r(val) -> str:
    if val is None:
        return "—"
    return f"{val:+.2f}R"


def _fmt_dt(val: datetime | None) -> str:
    if val is None:
        return "none in the current log"
    return val.strftime("%Y-%m-%d %H:%M Chicago")


def _fmt_dist(val) -> str:
    if val is None:
        return "—"
    return f"{val:+.2f}"


def build_snapshot(paths=None, now: datetime | None = None) -> dict:
    paths = paths or default_paths()
    now = now or datetime.now(tz=chicago_tz())
    log_path, text = read_bot_log(paths.logs_dir)
    status = parse_log(text, file_date=file_date_of(log_path), now=now)
    trades = load_active_trades(paths.active_trades)
    ids = [str(t.get("trade_id") or "") for t in trades]
    marks = latest_marks(paths.news_db, ids)
    positions = position_rows(trades, marks, now)
    summary = summarize(paths.news_db, paths.hedge_db)
    day = now.astimezone(chicago_tz()).date()
    today = today_activity(summary, day)
    tail = tail_lines(log_path, 50) if log_path is not None else []
    return {
        "now": now,
        "log_path": log_path,
        "status": status,
        "breaker": breaker_status(paths.hedge_db),
        "positions": positions,
        "today": today,
        "tail": tail,
        "news_db": paths.news_db,
        "hedge_db": paths.hedge_db,
    }


def chicago_tz():
    from ops.metrics import CHICAGO
    return CHICAGO


def render_page(snap: dict) -> str:
    status = snap["status"]
    now = snap["now"].astimezone(chicago_tz())
    stale = ""
    if status["stale"]:
        stale = (
            "<div class='stale'>"
            + html.escape(status["stale_text"])
            + "</div>"
        )
    window = (
        "Full-scan window is open (weekdays 08:45–15:00 Chicago)."
        if status["market_open"]
        else "Outside the weekday 08:45–15:00 Chicago full-scan window."
    )
    if status["holiday"]:
        window = "Log says the NYSE is closed today. Staleness check is off."

    crit_rows = status["criticals"]
    if crit_rows:
        crit_html = "".join(f"<li>{html.escape(line)}</li>" for line in crit_rows)
    else:
        crit_html = "<li>none</li>"

    pos_rows = snap["positions"]
    if pos_rows:
        body = []
        for row in pos_rows:
            body.append(
                "<tr>"
                f"<td>{html.escape(row['ticker'])}</td>"
                f"<td>{html.escape(row['contract'])}</td>"
                f"<td>{row['qty']}</td>"
                f"<td>{_fmt_px(row['entry'])}</td>"
                f"<td>{_fmt_px(row['mark'])}</td>"
                f"<td>{_fmt_money(row['pnl'])}</td>"
                f"<td>{_fmt_r(row['r'])}</td>"
                f"<td>{_fmt_dist(row['dist_sl'])}</td>"
                f"<td>{_fmt_dist(row['dist_tp'])}</td>"
                f"<td>{'—' if row['held'] is None else row['held']}</td>"
                "</tr>"
            )
        positions = (
            "<table><thead><tr>"
            "<th>Ticker</th><th>Contract</th><th>Qty</th><th>Entry</th>"
            "<th>Mark</th><th>P&amp;L</th><th>R</th>"
            "<th>To SL</th><th>To TP</th><th>Minutes</th>"
            "</tr></thead><tbody>"
            + "".join(body)
            + "</tbody></table>"
        )
    else:
        positions = "<p>No open positions.</p>"

    today = snap["today"]
    if today["closed"]:
        closed_body = []
        for trade in today["closed"]:
            closed_body.append(
                "<tr>"
                f"<td>{html.escape(str(trade.get('ticker') or '—'))}</td>"
                f"<td>{html.escape(str(trade.get('reason') or '—'))}</td>"
                f"<td>{_fmt_r(trade.get('r_fill'))}</td>"
                f"<td>{_fmt_money(trade.get('pnl_fill'))}</td>"
                "</tr>"
            )
        closed_html = (
            "<table><thead><tr><th>Ticker</th><th>Exit</th><th>R</th>"
            "<th>Fill P&amp;L</th></tr></thead><tbody>"
            + "".join(closed_body)
            + "</tbody></table>"
        )
    else:
        closed_html = "<p>No closes today.</p>"

    gate = status["gate"] or "none in the current log"
    log_name = snap["log_path"].name if snap["log_path"] else "no bot log"
    tail = "\n".join(snap["tail"]) if snap["tail"] else "(log empty)"
    day = now.date()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper book</title>
<style>
  body {{ font: 15px/1.45 ui-sans-serif, system-ui, sans-serif;
         margin: 24px auto; max-width: 1080px; color: #1c1917;
         background: #fafaf9; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 8px; }}
  p, li {{ margin: 4px 0; }}
  table {{ border-collapse: collapse; width: 100%; background: white; }}
  th, td {{ border-bottom: 1px solid #e7e5e4; text-align: left;
           padding: 6px 8px; vertical-align: top; }}
  th {{ font-size: 12px; letter-spacing: 0.03em; color: #57534e; }}
  .stale {{ background: #fee2e2; border: 1px solid #b91c1c; color: #7f1d1d;
           padding: 12px 14px; margin: 12px 0; font-weight: 650; }}
  .meta {{ color: #57534e; font-size: 13px; }}
  pre {{ background: white; border: 1px solid #e7e5e4; padding: 10px;
        overflow: auto; max-height: 360px; white-space: pre-wrap; }}
  dl {{ display: grid; grid-template-columns: 180px 1fr; gap: 4px 12px; }}
  dt {{ color: #57534e; }}
  dd {{ margin: 0; }}
</style>
</head>
<body>
<h1>Paper book</h1>
<p class="meta">Read-only. This process is not the bot. Refreshes every {REFRESH_SECONDS}s.
As of {html.escape(now.strftime("%Y-%m-%d %H:%M:%S"))} Chicago.
{html.escape(window)}</p>
{stale}
<h2>Bot</h2>
<dl>
  <dt>Version</dt><dd>{html.escape(status["version"])}</dd>
  <dt>Last scan</dt><dd>{html.escape(_fmt_dt(status["last_scan"]))}</dd>
  <dt>Last exit pass</dt><dd>{html.escape(_fmt_dt(status["last_exit"]))}</dd>
  <dt>Circuit breaker</dt><dd>{html.escape(snap["breaker"])}</dd>
  <dt>Yahoo crumb</dt><dd>{html.escape(status["crumb"])}</dd>
</dl>
<h2>CRITICAL today</h2>
<ul>{crit_html}</ul>
<h2>Open positions</h2>
<p class="meta">Mark is the latest position_marks mid for that trade id.
P&amp;L is (mark − entry) × 100 × qty. R uses (entry mid − stop) × 100 × qty.
Distance is premium points: mark − stop, and target − mark.</p>
{positions}
<h2>Today ({html.escape(str(day))})</h2>
<dl>
  <dt>Entries</dt><dd>{today["entries"]}</dd>
  <dt>Closes</dt><dd>{today["closes"]}</dd>
  <dt>Realized fill P&amp;L</dt><dd>{_fmt_money(today["realized_fill"])}</dd>
  <dt>Spread cost</dt><dd>{_fmt_money(today["spread_cost"])}</dd>
</dl>
<h2>Latest gate</h2>
<p>{html.escape(gate)}</p>
<h2>Closed today</h2>
{closed_html}
<h2>Log ({html.escape(log_name)}, last {len(snap["tail"])} lines)</h2>
<pre>{html.escape(tail)}</pre>
<p class="meta">Databases opened read-only:
{html.escape(str(snap["news_db"]))} and {html.escape(str(snap["hedge_db"]))}.
Listening on 127.0.0.1:{PORT} only.</p>
</body>
</html>
"""


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
