# Measurement freeze

Frozen at 741584e2ba22e13724de8bf920106e099065c63f (`741584e`) on 2026-08-24.
Measurement period: 30 sessions.

That commit is the fill-accounting / TRADE / SESSION record change.
Nothing in it altered entry, sizing, stops, exits, or scoring. Equity
and the SESSION line are the fill series (buy ask / sell bid). Triggers
still fire on mid. Every TRADE and SESSION line and the boot log carry
`v` + the first 7 of HEAD so a later change is visible in Discord.

No changes permitted except:

- a bug that stops trading entirely
- a bug that corrupts the TRADE / SESSION record
- a security or credential issue

Anything else waits. Config/env changes also break the freeze — record
any in this file with date and reason.

Deploys reset the ledger's filesystem, so avoid deploying at all. Discord
is the durable store. Reconstruct from TRADE and SESSION lines.

## Allowed exceptions log

| Date | Reason |
|---|---|
| 2026-08-24 | Before first counted session: SESSION trailing `bp` (col 18); TRADE prints unrounded entry_mid/ask so recovered spread on $1–$2.50 names is not blurred by round(mid, 2). Trading debit/SL/TP still use rounded premium. `MAX_CONTRACT_SPREAD_PCT` still 8.0. |
| 2026-08-30 | Freeze exception 1: Aug 28 option-mark outage left two carried lots unmarked 08:55–13:01 (NVDA −4.58R vs −1.04R stop; MSFT TP luck). Underlying-based fallback stop/TP (`UNDERLYING_STOP`, fill=est) when chain is dark but spot is live; UNPROTECTED CRITICAL after MARK_BLACKOUT_MINUTES (45) with no mark and no spot; mark/kill-switch CRITICAL spam throttled (first, then double / every 10th). Exit-only no longer fans out full chains on a quote failure. |
| 2026-08-30 | Freeze exception 2: THESIS_VOID requires a clean score (pivot, ATR, pct_change, usable chain) and two consecutive clean prints below 55. Dirty open prints (no_liq_data / no_momentum_data / no_pivot_data / no_atr_data / usable=0) log THESIS_SKIP and HOLD. SL/TP/expiry unchanged. Aug 28 discarded; count restarts on the next clean session. Sizing, spread cap, and scoring formula unchanged. |
| 2026-09-29 | Freeze exception 4: night harvest looped every ~95s because the sleep broke on `time() >= 09:15`, which is true all evening. Each pass re-hit Yahoo (~21 ticker calls). Crumb HTTP 429 then quoteSummary 401, and the retries kept the IP throttled into the open, so option chains do not load. Harvest once per upcoming pre-market date; sleep until that 09:15 ET (state checks at least 60 minutes apart, exact remainder only when the pre-market is closer); crumb 429 backs off 15 min, doubling, cap 4 h, and further getcrumb calls in the window are not sent; a failed earnings fetch keeps the last-known print date, and a calendar with no known dates pages CRITICAL instead of reading as a clear book. Boot banner reads `EXIT_INTERVAL_SECONDS` / `FULL_SCAN_INTERVAL_SECONDS`. No scoring, sizing, stop, exit, universe, or cadence-knob change. |
| 2026-09-29 | Freeze exception 5: position and session records could lie before a new 30-session count. A failed paper sell removed the lot and emitted TRADE; a second sell of the same trade_id credited cash again; a buy whose active-trade write failed left the debit and the gate slot; a missing expiry mark was taken from another expiry at the same strike; EOD / 0DTE / expiry / earnings flatten with no mark closed at the entry premium; entries_today never rolled to the next Chicago session, so the daily cap stayed shut and open positions plus exit cooldowns were not wiped to fix it; a flat book (including a breaker-open pass) skipped the 14:45 BOOK/SESSION emit; that emit latched the day before Discord accepted it; an LLM deadline waited out the hung call because the executor context manager joins the worker. Failed closes keep the lot and emit no TRADE. A repeated close of the same trade_id does not credit again. A failed position write reverses the paper debit and the admit. Marks require the exact expiry. A forced flatten with no mark stays open and pages UNRESOLVED, and that EOD day is not latched. entries_today resets on the Chicago date change only. BOOK/SESSION fires on a flat book and latches only after Discord returns success. The LLM deadline returns when the budget expires. /health stays HTTP 200; /status reports whether the macro thread is alive. No scoring, sizing, stop, target, spread, universe, or cadence-knob change. |
| 2026-09-30 | Freeze exception 6: Sep 30 fetched zero option chains (Yahoo crumb HTTP 429 from the Render IP all session) and the GATE line read `BLOCK below_thr×10` with telemetry=0, so a data outage was recorded as ten weak setups. A ticker with no score card (options error, timeout, or exception before a card) is `data_unavailable`. The scan posts one CRITICAL, `NO MARKET DATA — 0/N tickers fetched, trading suspended`, and latches it for the Chicago session only after Discord accepts it. Pivot and ATR hard-fails stay `no_pivot_data` / `no_atr_data`. No scoring, sizing, stop, exit, universe, or cadence-knob change. |

## Post-measurement candidates (do not implement during the freeze)

These wait until 30 sessions of TRADE/SESSION data exist. Not live
options. Not config changes. Written down so they are not lost.

1. `MAX_CONTRACT_SPREAD_PCT` 8.0 → 6.0 pending selected-contract spread
   distribution. Chain-median suggested 34% of planned_risk on Aug 24;
   selected-contract data will settle it. Left at 8.0 for the freeze
   because the 6.0 recommendation was inferred from chain-median, and
   selected bid/ask was never logged. TRADE `entry_mid` / `entry_ask`
   is the dataset.

2. 15-minute entry cadence. Today: full score/admit every 30 minutes
   (`FULL_SCAN_INTERVAL_SECONDS=1800`), exit-only marks every 5 minutes
   (`EXIT_INTERVAL_SECONDS=300`). Candidate: admit on a 15-minute clock
   so entries are not delayed a full scan after a 70+ print. Separate
   from the spread-cap question. Do not change cadence during the freeze.
