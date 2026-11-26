# Webull execution

Stamp stays `v91c6693`. The six stamped files stay untouched: `config.py`, `position_exits.py`, `scoring_engine.py`, `signal_gate.py`, `strike_selector.py`, `virtual_broker.py`. The executor is new modules outside that set. It reads the paper ledger and never writes it. It does not change scoring, gating, sizing, stops, exits, the universe, cadence, or LLM routing.

The package is `webull_exec/`. It runs from `.venv-webull` only. The paper bot's `.venv` does not install the SDK. Records go to `data/webull_exec.db` only.

## Phase B standing decisions

These override the Phase A paragraphs below where they disagree.

- Process: `launchd/com.hedgefund.webullexec.plist` runs `run_webull_exec.sh`. That script sources `.env`, sets `EXECUTION_MODE=sandbox` only when the name is unset, and always unsets `LIVE_TRADING_ENABLED`. A later `EXECUTION_MODE=off` wins. This build never sends a live order. `EXECUTION_MODE=live` logs CRITICAL and sleeps.
- Heartbeat: `WEBULL_HEARTBEAT_URL`. Skip the ping when it is unset. The URL is never logged. This is not the paper bot's `HEARTBEAT_URL`.
- Entry limit is the paper trade's `entry_ask` (Yahoo). The executor does not chase the Webull ask. Unfilled after 60 seconds: cancel and log a miss. At each entry and each exit it writes one `quote_compares` row in `data/webull_exec.db`: the Webull option bid, ask, and `quote_time` beside the Yahoo bid and ask the paper bot used. That row does not set the limit.
- Protection: place the entry as one combo, `MASTER` limit plus `STOP_LOSS` / `STOP_LOSS_LIMIT`, single leg, so the stop exists from the fill. If that combo is rejected, place one plain `LIMIT` and put the stop on immediately after the fill. The stop is `STOP_LOSS_LIMIT`, `DAY`, limit = stop × 0.92. A rejected stop is retried with the same `client_order_id`. Unprotected 60 seconds after the fill sends CRITICAL.
- Exit: cancel the stop, sell at the paper exit bid. Reprice after 30 seconds to the Webull snapshot bid, or the same paper bid if the snapshot has none. CRITICAL if the position is still open 2 minutes after the exit started. No bid sends CRITICAL and retries. The executor does not sell at 0.01.
- 14:45 CT flatten sells. 14:50 CT sweep only reports: `FLAT CONFIRMED (n positions)` or a CRITICAL that names each open position. Early-close dates already in `session_clock` flatten at 11:45 CT and sweep at 11:50 CT. The executor does not edit that clock.
- Boot reads Webull positions and open orders first, flattens anything open, and sends CRITICAL with the details. Paper trade ids already open at that moment are a baseline and are not chased.
- Caps live here, not in `config.py`: 1 contract, 3 open, the ten tickers, premium at or below $7.50, no new entry at or after 13:45 CT, daily realized R at or below −3 blocks new entries, cumulative realized loss at or below −$1,000 disables the executor, writes `data/webull_exec.halt`, sets `EXECUTION_MODE=off`, sends CRITICAL, and keeps exiting what is already open.
- Reconcile every 60 seconds. Any mismatch sends CRITICAL. The executor does not place a second order to repair it. Orders younger than 15 seconds are not required to be visible yet.
- Kill switch: `.venv-webull/bin/python -m webull_exec.kill`. With no arguments it cancels open orders, sells positions, sets `killed` in the executor database, writes the halt file, and sets `EXECUTION_MODE=off` in the production `.env`. The running loop stops so it does not sell the same position again. A drill passes `--account-suffix` and `--env-file`. That cancels and flattens only the account whose id ends with the suffix, and writes `EXECUTION_MODE=off` only to that env file. It does not write the production `.env`, the production halt file, or the production executor database. A suffix that matches nothing exits before any cancel or sell.
- `client_order_id` is stored in `live_orders` before the HTTP call. Shape: `w` + `YYYYMMDD` + 4-digit counter + one kind letter. A restart flattens broker orders and does not open a second order for a paper trade that was already in the baseline.
- Paper closes are read from `news_room.db` with `mode=ro`. The executor never writes that file, `hedge_fund.db`, `active_trades.json`, or `discord_outbox`. CRITICAL posts go out through `DISCORD_WEBHOOK` and are stored in `data/webull_exec.db`.
- Account choice: the list endpoint has no options flag. Its schema is account id, number, `CASH`/`MARGIN`, label, and class. When every row lacks an options flag, use the single `INDIVIDUAL_MARGIN` account. If there is no margin account and exactly one `INDIVIDUAL_CASH`, use that. Futures, crypto, and event accounts are not used. More than one margin account, or more than one explicit options flag, sends CRITICAL and stays flat. A drill may pass `--account-suffix` (the account id's ending characters) together with `--env-file` and `--db`. Those two paths must not be the production `.env` or `data/webull_exec.db`. The drill loads only that env file, writes only that database, and records notifications in `drill-notify.log` with a `[DRILL]` prefix. It does not post them to Discord. Zero matches, or more than one, exit before any order. A partial drill command exits before connect. `run_webull_exec.sh` and `com.hedgefund.webullexec` pass no suffix, so production stays on the default account.
- Friday 2026-11-27 order-type check is `tools/webull_order_probe.py`. The script exits before any HTTP call on every other Chicago date. It is the run that learns whether the sandbox accepts `STOP_LOSS` versus `STOP_LOSS_LIMIT`, and `DAY` versus `GTC`.

Probe record: `logs/webull_sandbox_probe.txt`. The read-only probe sends no order.

Sources, fetched 2026-11-25 from developer.webull.com:

- Trading API, Options: `apis/docs/trade-api/options.md`
- Trading API, Overview (feature matrix): `apis/docs/trade-api/overview.md`
- Trading API, FAQ: `apis/docs/trade-api/faq.md`
- Trading API, Event Contracts (TIF contrast only): `apis/docs/trade-api/event-contract.md`
- Token: `apis/docs/authentication/token.md`
- Rate limits: `apis/docs/rate-limits.md`
- Market Data overview, Options Market Data index, List Option Snapshots

SDK: `webull-openapi-python-sdk` 3.1.2 in `.venv-webull` only, pinned in `requirements-webull.txt`. It is not in the paper `requirements.txt`. The SDK's Python 3.12 pins (`cryptography<43`, `protobuf<6`) are why the two environments stay split. The paper `.venv` keeps `cryptography` 50.0.2 and `protobuf` 7.36.2.

## Phase A results that constrain this design

Phase A found the sandbox key names absent and did not call the host. Phase B reads those names from `.env` and records the read-only probe in `logs/webull_sandbox_probe.txt`. No sandbox order is part of that probe.

Phase A's SDK install downgraded the shared paper venv (`cryptography` 50.0.2 to 42.0.8, `protobuf` 7.36.2 to 5.29.6). Phase B uninstalled the SDK and the packages it added, restored those two versions, and put the SDK in `.venv-webull`. The paper bot was not restarted, so the running process still has whatever it loaded at start. The next start uses the restored venv.

Official docs disagree on two points that Phase A could not settle with an order:

- The options page lists `MARKET`, `LIMIT`, `STOP_LOSS`, and `STOP_LOSS_LIMIT`. It defines `STOP_LOSS` as "Trigger a market order when the stop price is reached." The feature matrix marks "Market / best-available-price execution" and "Stop Loss" as supported for options.
- The Trading API FAQ lists options as `LIMIT`, `STOP_LOSS`, `STOP_LOSS_LIMIT` and says: "Options do not support `MARKET` or `TRAILING_STOP_LOSS` order types. Options sell-side orders only support `DAY` as time in force (`GTC` is buy-side only)."

The options page shows sell-side `STOP_LOSS` and `STOP_PROFIT` examples with `time_in_force: GTC`. Phase B sandbox has to learn which document the gateway enforces. Phase A placed no order, so this design assumes the FAQ can reject a market-style stop and a sell-side `GTC`.

## Mode

Two environment names, read with `os.environ` inside the new module. They are not added to `config.py` and they are not trading knobs.

| Name | Values | Default when unset |
| --- | --- | --- |
| `EXECUTION_MODE` | `off`, `sandbox`, `live` | `off` |
| `LIVE_TRADING_ENABLED` | `true`, `false` | `false` |

Behavior:

- `off`: the executor does not open new trades. If the executor database still has an open intent, or the halt file is present, it stays connected and finishes the exits. Otherwise it sleeps and re-reads `.env`. It does not exit, because launchd `KeepAlive` would restart it.
- `sandbox`: authenticate to `api.sandbox.webull.com`. Orders are allowed only under the rules below, and only with the sandbox key pair.
- `live`: authenticate to `api.webull.com` only when `EXECUTION_MODE=live` and `LIVE_TRADING_ENABLED=true`. Either flag alone keeps live orders off.

Phase A does not write either name into `.env`. A future paper-bot restart with both unset changes nothing.

Phase B shows both values in two unstamped places, as text only:

- `master_bot.py` boot string, currently built near the `BOT UP` line (about line 1687). Append `execution=<mode> live_armed=<true|false>`.
- The 08:15 CT prep brief in `pre_market_meeting.py`, beside `shadow_news.brief_line()` (about line 388). Same two fields.

Those lines appear on Discord only after an operator restart of the paper bot. The Nov 30–Dec 9 sandbox test does not restart the paper bot. During that window the executor writes the same line to its own log and may send one Discord message from its own process. That send is recorded in the executor database. It does not insert into `discord_outbox` or any paper table.

## Process and modules

New package, all outside the stamp:

- `webull_exec/mode.py` — read the two flags.
- `webull_exec/broker.py` — official SDK only. `ApiClient(app_key, app_secret, "us")`, then `add_endpoint("us", host)`. Sandbox host `api.sandbox.webull.com`. Live host `api.webull.com`. Account list via `TradeClient.account_v2.get_account_list()`. Option chain and snapshots via the SDK market-data client (`DataClient.option_market_data`, contract category `US_OPTION`).
- `webull_exec/store.py` — the executor's own SQLite file, `data/webull_exec.db`. The paper bot never opens it.
- `webull_exec/paper.py` — read-only. Open paper trades come from `active_trades.json`. Closes come from the latest `EXIT:` rows in `news_room.db` opened `mode=ro`.
- `webull_exec/__main__.py` — poll loop, every 10 seconds.
- `webull_exec/kill.py` — `.venv-webull/bin/python -m webull_exec.kill`.

The executor is a separate process. It polls the paper ledger every 10 seconds, read-only (SQLite URI `mode=ro` on the paper files). It never calls the gate, the sizer, or the paper broker. A paper admit still happens exactly as it does now. A Webull miss leaves the paper position in place.

Account choice, once keys exist: list accounts, log type (`CASH` or `MARGIN`) and options permission, mask the account id to the last 4 characters. If one account has options enabled, use it. If none do, or more than one does, send CRITICAL and stay flat. Phase A could not see which type the sandbox issued.

Boot order, before any new order:

1. Read the mode. `off` exits.
2. Authenticate.
3. Read Webull positions and open orders.
4. Load the executor book.
5. Flatten anything already open at the broker. Send CRITICAL with ticker, expiry, strike, type, and quantity. Do not resume it.
6. Remember the paper trade ids that are already open. Do not chase them. New entries are paper opens that appear after this boot.

## Entry

Follow a paper admit whose score is at least 80. The signal is the new paper open (the paper ledger row for that admit), seen on a 10-second poll. The contract is the paper contract: same underlying, expiry, strike, and call/put. Quantity is 1.

Send `BUY`, `order_type=LIMIT`, `time_in_force=DAY`, `option_strategy=SINGLE`, `instrument_type=OPTION`, `position_intent=BUY_TO_OPEN`. `limit_price` is the paper `entry_ask`. The first try is a combo (`combo_type=MASTER`) with the protective stop attached. If the gateway rejects the combo, one plain `NORMAL` limit is sent. A second rejection is a logged reject and is not retried.

Cancel the order if it is still unfilled 60 seconds after the ack. Log a miss. A miss is an executor-book state. Reconciliation then expects Webull to be flat for that intent. The paper position may stay open. That divergence is recorded as a miss, and it is not a mismatch.

The entry is not a bracket. A `MASTER` + `STOP_LOSS` payload would attach protection before the fill is known, and the options take-profit/stop-loss combo also wants a `STOP_PROFIT` leg. Paper exits for take-profit, time stop, trail, breakeven, and flatten. A broker take-profit would sell against that path. Protection is a separate order after the fill.

## Protection

The standing stop is attached to the entry combo when the gateway accepts it. When the combo is rejected, place one sell stop at the paper stop price immediately after the fill (the price already stored on the paper position: 20% below the paper entry premium).

Recommendation: `STOP_LOSS_LIMIT`, `time_in_force=DAY`, `combo_type=NORMAL`, `side=SELL`, `position_intent=SELL_TO_CLOSE`.

Reasons, from the documents above, not from a sandbox order:

- `STOP_LOSS_LIMIT` is on the options page, in the FAQ options list, and on the feature matrix.
- `STOP_LOSS` is defined on the options page as a trigger for a market order. The FAQ says options do not support `MARKET`. A stop that becomes a market order can be rejected, and a market fill on an option can print far through a wide spread.
- The limit is the stop price times `0.92`, floored at `0.01`. That is one paper spread cap (8%) through the stop. Tick rounding follows what the sandbox accepts in Phase B. This distance is a proposal for review.
- `DAY` matches the FAQ rule that sell-side options orders support `DAY`. A `GTC` protective stop, which the options take-profit example uses, would rest overnight and would not flatten at 14:45. This design does not copy that `GTC`.

If the quote gaps through the limit, the stop-limit rests unfilled and the long option stays open. A dead Mac cannot reprice it. That hole is stated again under the overnight guard.

If the sell stop is rejected, retry on the next 10-second poll with the same `client_order_id`. If the position is still unprotected 60 seconds after the fill, send CRITICAL. The position stays under the exit rules. The executor does not switch it to `STOP_LOSS` (market-style) unless a later sandbox test shows the FAQ is wrong and this document is revised.

Phase B sandbox, still before any live key, must record two results: whether a sell `STOP_LOSS` is accepted, and whether a sell `GTC` is accepted. Those two calls are probes inside the test plan. They are not the standing order type.

A stop can be attached to an existing position. The options page shows that as a combo with no `MASTER`: `STOP_PROFIT` plus `STOP_LOSS`. It also shows a standalone `STOP_LOSS` / `STOP_LOSS_LIMIT` on a single order (`combo_type=NORMAL`). This design uses the standalone `STOP_LOSS_LIMIT` after the fill. `OTO`, `OCO`, and `OTOCO` are equity-only. The feature matrix marks them unsupported for options, and marks take-profit/stop-loss combinations supported for `SINGLE` only.

## Exit

When the paper position closes for any reason, cancel the resting stop, then sell the Webull contract.

Sell with `LIMIT`, `DAY`, `SELL_TO_CLOSE`, limit equal to the paper exit bid. The FAQ's market ban is why this is a limit and not a market order. If the paper close has no bid, use the Webull snapshot bid. If neither exists, send CRITICAL and retry on the next poll. Do not dump the option at 0.01.

If the sell is still open 30 seconds after the ack, cancel and replace. The new limit is the Webull snapshot bid when that bid exists, otherwise the same paper exit bid. The replace uses a new `client_order_id`. The cancelled id stays in `live_orders`.

If the Webull position is still open 2 minutes after the paper close, send CRITICAL and keep repricing every 30 seconds. New entries stay blocked while that CRITICAL is open.

## Session clock

America/Chicago, same instants the paper book already uses.

- No new Webull entry at or after 13:45 CT. This is stricter than the paper admit cutoff (14:15 CT). A paper admit after 13:45 is ignored. The paper gate is unchanged.
- 14:45 CT: cancel resting stops and sell every Webull position the executor owns, even if the paper close event has not arrived.
- 14:50 CT: safety sweep. Read positions. Post `FLAT CONFIRMED (n positions)` or send CRITICAL naming each open position. The sweep does not place a second sell. The 14:45 flatten is the sell.
- On the early-close dates already in `session_clock.py` (2026-11-27 and 2026-12-24), use 11:45 CT and 11:50 CT. The Nov 30–Dec 9 window contains neither date. The executor reads the clock; it does not edit it.
- An early-close date already blocks paper admits for the whole day. The executor also places no new entry on those dates.

## Overnight guard

US options time in force on the options page is `DAY` and `GTC` only. There is no good-till-time and no `GTD` for options. `GTD`, `IOC`, and `FOK` are event-contract time-in-force values. Event contracts are out of scope.

A `DAY` stop protects until the end of that session and then expires. It does not close a long option that never traded through the stop. A `GTC` stop would still be working the next morning and would not flatten at 14:45, and the FAQ may reject it on the sell side.

Best broker-side behavior Webull documents for this book: a `DAY` `STOP_LOSS_LIMIT` while the session is open, plus the executor's 14:45 close and 14:50 sweep while this Mac is up.

Remaining risk, plainly: if this Mac dies after the entry fills, and the option never trades through the stop, Webull will not flatten the position at 14:45. The long option is held overnight. Loss is limited to the premium paid. Under the premium cap below, that is up to $750 per contract and up to three contracts ($2,250) if three entries filled before the Mac died. That breaks the no-overnight rule. There is no time-triggered options order to close it. Recovery is a human close in the Webull app, or the executor's next boot, which reads positions first and flattens anything it finds before it does anything else.

## Hard caps

These live in the executor. They do not change paper sizing.

| Cap | Rule |
| --- | --- |
| Size | 1 contract per order |
| Open positions | 3 Webull option positions |
| Universe | SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA |
| Entry clock | No new entry at or after 13:45 CT |
| Daily stop | Realized Webull R for the Chicago date at or below −3R blocks new entries. Open positions are still exited. R is the paper planned risk of that fill (20% of premium × 100 for the 1-lot). |
| Loss cap | Cumulative realized Webull P&L at or below −$1,000: send CRITICAL, refuse new entries, write the halt file and set `EXECUTION_MODE=off`, and keep managing existing positions until flat. The sum is stored in `data/webull_exec.db` and survives a restart. |
| Premium | Skip the Webull order when the ask is above $7.50. Log a miss. |

Premium basis: `ACCOUNT_SIZE` default 10000 and `RISK_PER_TRADE_PCT` default 1.5 are not overridden in `.env`, so the paper 1-lot ceiling is $150. The paper stop is 20% of premium, so 1-lot dollar risk is `0.20 × premium × 100`. That stays inside $150 when the premium is at or below $7.50. Paper `MIN_PREMIUM` is $1.00 and is unchanged. A paper admit between those bounds can still be skipped by the executor when the live ask is above $7.50.

## Reconciliation and idempotency

Every 60 seconds, and on boot: compare Webull positions (contract, side, quantity) and open orders (`client_order_id`, status, remaining quantity) with the executor book. Any difference sends CRITICAL and blocks new entries. The executor does not "fix" a mismatch by placing a second order.

`client_order_id` is required, at most 32 characters, and unique per account for the life of the account. Generate it in the executor, insert the `live_orders` row, and only then call the API. A restart loads that id and queries order detail before placing. The same intent reuses the same id. A reprice after a cancel gets a new id. The id is not a fresh UUID per process start.

Suggested shape, 32 characters or fewer: `w` + Chicago date `YYYYMMDD` + intent counter + kind (`b` buy, `s` stop, `x` exit, `r` reprice, `f` flatten, `k` kill). The counter is in the executor database.

## Records

`data/webull_exec.db`, written only by the executor.

`live_orders`: `client_order_id`, intent, paper trade id, contract description, side, quantity, order type, limit price, stop price, time in force, request JSON, response JSON, status, fill price, filled quantity, submitted time, ack time, filled time, cancelled time.

`slippage`: one row when both the paper fill and the Webull fill exist for the same intent. Buy slippage in dollars is `(webull_fill - paper_fill) × 100`. Sell slippage is `(paper_fill - webull_fill) × 100`. Positive means the Webull fill was worse.

Neither table is written into the paper ledger. Phase A creates neither file.

Executor CRITICAL messages go out through a direct Discord post from the executor process and are logged in `data/webull_exec.db`. They are not inserted into the paper `discord_outbox`, so a second writer does not race the bot's flush. The paper bot remains the only writer of the paper database.

## Kill switch

One command: `.venv-webull/bin/python -m webull_exec.kill`

1. Read positions and open orders first.
2. Cancel every open Webull order.
3. Sell every open Webull option position with the same marketable limit used for exits (bid, reprice at 30 seconds).
4. If anything is still open after 2 minutes, send CRITICAL and leave the command in a failed state.
5. Write a halt file the executor reads on every poll, and set `EXECUTION_MODE=off` in `.env` without printing other lines.
6. Do not restart the paper bot. Do not write the paper ledger.

The kill switch and the cumulative-loss cap are the paths that edit `.env`, and only the `EXECUTION_MODE=off` assignment. Neither restarts the paper bot or writes the paper ledger.

## What Phase B must confirm in sandbox before any live key

- Account type and which account has options. Log last 4 only.
- Option snapshot bid/ask: present or absent, `quote_time` lag versus the local clock, and whether the feed is real-time, 15-minute delayed, or HTTP 403.
- Same-moment Yahoo bid/ask for that contract, from the bot's existing quote path, one pair, logged next to the Webull pair.
- Sell `STOP_LOSS` accepted or rejected.
- Sell `GTC` accepted or rejected.
- `STOP_LOSS_LIMIT` with `DAY` accepted, and the tick size the gateway requires.
- A 1-lot limit buy fills, misses at 60 seconds, or rejects, with one `client_order_id`.

Live keys are not issued. Phase B does not look for them. `EXECUTION_MODE=live` stays unreachable until those keys exist and `LIVE_TRADING_ENABLED=true` is set on purpose.

## Test plan, 2026-11-30 through 2026-12-09

Sandbox only, beside the paper extension. The paper bot stays up. The paper extension's rules are unchanged: 8 consecutive clean paper sessions from Monday 2026-11-30, expected end Wednesday 2026-12-09. Friday 2026-11-27 does not count. A paper fail extends the paper test. It does not edit this plan.

This calendar starts only when both sandbox key names are in `.env`. Phase B found both names set. The calendar stays 2026-11-30 through 2026-12-09. Do not start the order-following sessions early. Friday 2026-11-27 is the order-type probe only (`tools/webull_order_probe.py`), and paper entries are blocked that day. Go-live still needs 5 clean sandbox sessions, plus the paper extension passing.

Trading days in the window: Nov 30, Dec 1, Dec 2, Dec 3, Dec 4, Dec 7, Dec 8, Dec 9. Dec 5 and Dec 6 are the weekend.

| Day | What is tested | Counts as clean? |
| --- | --- | --- |
| Mon 2026-11-30 | Auth, account types (last 4 only), balances, positions, one SPY chain, one contract quote versus the bot's Yahoo quote, latency, entitlement. Executor in `sandbox` from before 08:30 CT through the 14:50 sweep. Follow any paper 80+ admit under the caps. | Yes, if the clean definition holds |
| Tue 2026-12-01 | Happy path. Follow the first paper 80+ admit: limit at the paper entry ask, 60-second miss cancel, attached `DAY` `STOP_LOSS_LIMIT` or the same stop after the fill, exit when the paper position closes. If the paper book has no 80+ admit, one supervised 1-lot round-trip, tagged `test` in `live_orders`, flattened the same day. | Yes, if the clean definition holds |
| Wed 2026-12-02 | Forced reject. One sandbox order the gateway should refuse (a sell `MARKET` if the FAQ is enforced, otherwise a limit outside the documented price range). Confirm no position, no second order, no reused-as-new `client_order_id`, and a logged reject. | No |
| Thu 2026-12-03 | Forced partial, then a mismatch drill. A 1-lot may be unable to partial. If the sandbox cannot produce a partial, record that and do not raise the size above 1. If a partial does occur, cancel the remainder and protect only the filled quantity. Then create one supervised mismatch (a sandbox position the executor book does not expect), confirm CRITICAL and a block on new entries, and flatten with the kill switch. The paper ledger is not edited to fake the mismatch. | No |
| Fri 2026-12-04 | Restart the executor only, mid-position, after the fill and the stop ack. The paper bot stays up. Confirm one position, one stop, and no second entry. | Yes, only if recovery meets the clean definition |
| Mon 2026-12-07 | Mac-offline drill. After the fill and the `DAY` stop ack, stop the executor process and leave it down through 14:50 CT. Do not stop the paper bot. Next boot, after the close, reads positions first and records whether the option is still open and whether the `DAY` stop has expired. Flatten before Tuesday. | No |
| Tue 2026-12-08 | Ordinary follow day. No injected fault. | Yes, if the clean definition holds |
| Wed 2026-12-09 | Ordinary follow day through the 14:45 close and the 14:50 sweep. Slippage rows for every dual fill. | Yes, if the clean definition holds |

Aimed clean days, if each of those five holds: Nov 30, Dec 1, Dec 4, Dec 8, Dec 9. The three drill days do not count. If Dec 4 duplicates an order, it does not count, and go-live waits for a replacement clean session after Dec 9. Drills are not compressed to manufacture the count.

### Clean sandbox session

All of the following:

- `EXECUTION_MODE=sandbox` for the whole cash session. `LIVE_TRADING_ENABLED` is not `true`.
- The executor is up from before 08:30 CT through the 14:50 sweep, except the Dec 4 restart, which must finish before the sweep.
- Boot read Webull positions and open orders before any new order logic.
- Every paper admit with score at least 80 is either one filled contract or a logged miss inside 60 seconds, subject to the caps. A cap skip is a logged miss.
- No order for a score under 80, a quantity other than 1, a ticker outside the ten, or a clock at or after 13:45 CT.
- Every fill has one accepted broker stop, or a CRITICAL if the stop is still rejected 60 seconds after the fill.
- Every paper close of an executor position produces a flat Webull position, or a CRITICAL inside 2 minutes.
- The 14:50 sweep shows zero Webull positions and zero open Webull orders.
- Reconciliation ran about every 60 seconds and recorded no mismatch.
- Each attempt has a `live_orders` row. Each dual fill has a `slippage` row. Neither row is in the paper ledger.
- A restart did not create a second order for an intent that already had a `client_order_id`.
- The stamp is still `v91c6693`. The paper bot was not restarted for the test.

A session with zero paper admits is clean when the rest holds and the book stays flat.

### Go-live

Both are required:

- At least 5 clean sandbox sessions, as defined above.
- The paper extension passing on its own pre-registered rules (8 consecutive clean paper sessions from 2026-11-30, Nov 27 excluded, Nov 18–19 excluded from the strategy sample).

Until both are true, `EXECUTION_MODE` stays `off` or `sandbox`, and `LIVE_TRADING_ENABLED` stays unset. A sandbox fail does not change the paper rules. A paper fail does not by itself fail a sandbox day that met the definition above. It does delay go-live.
