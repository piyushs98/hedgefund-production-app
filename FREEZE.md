# Measurement freeze

Frozen at 741584e2ba22e13724de8bf920106e099065c63f (`741584e`) on 2026-08-24.
Measurement period: 30 sessions.

That commit is the fill-accounting / TRADE / SESSION record change.
Nothing in it altered entry, sizing, stops, exits, or scoring. Equity
and the SESSION line are the fill series (buy ask / sell bid). Triggers
still fire on mid. Every TRADE and SESSION line and the boot log carry
the pinned trading stamp. Oct 2's posted lines are `v7a25760`. The pin
is now `v9c3cc4d` because freeze exception 11 settles an already-expired
contract at intrinsic value. That is an exit-rule change. A dashboard
commit or the Squawk shadow does not move it. Bump that stamp only when
scoring, sizing, exits, the gate, the universe, cadence, or chain-load
behavior changes.

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
| 2026-09-30 | Freeze exception 7: the night loop that was hammering Yahoo is fixed, and the Render IP needs quiet days before Yahoo is judged unusable. A full scan already ignored expiries past MAX_EXPIRY_CALENDAR_DTE. It still downloaded the expiration list (which includes the nearest chain) and then downloaded that nearest chain again, plus every other listed expiry from calendar DTE 0 through MAX. The list and the nearest chain are now one request. That nearest chain is kept when its DTE is inside 0..MAX, including today's expiry below MIN_DTE, so the scored set is the same and the selector still rejects it. Further option_chain calls are only MIN_DTE..MAX. One yfinance Ticker per symbol is kept for the Chicago date so the 5-minute exit pass does not re-download the expiration list. A held expiry outside 0..MAX stays off the scan chains: the selector ranks every loaded expiry and has no MAX cap, so that chain would be a new entry. The 5-minute pass already quotes the one held expiry. Daily history is not frozen for the session: pivot levels are already frozen, but close, pct_change, and ATR use the developing bar, and freezing that bar would change the score. Yahoo HTTP calls are spaced 0.5s. SESSION columns 1–18 are unchanged; four trailing fields are chain=ok\|blocked, news=ok\|blocked, futures=ok\|blocked, earnings=env\|yahoo\|none, so a blocked feed can be left out of the measurement. No scoring, sizing, stop, exit, universe, or cadence-knob change. |
| 2026-10-06 | Freeze exception 8, directed by the operator: Yahoo's news xhr returns 404, so the tech, macro, and politics desks save nothing. The public page https://x.com/FirstSquawk is read once per night entry and once before each full scan. A non-200 is not retried. A line that names one of the ten tickers is stored on that ticker. A Fed or macro line that names none of them is stored as FED and does not enter those ten scores. Anything else is dropped. No sizing, stop, exit, universe, or cadence change. The scoring formula is unchanged. Exception 10 keeps the stored text out of S. |
| 2026-10-06 | Freeze exception 9, directed by the operator: a single night read can miss a headline that scrolls off the public page. The night wait reads that same page between the hour sleeps (one GET, no retry, no Yahoo) and once more before the morning brief, only on the morning the brief is generated. The brief's tape starts at the prior cash close (Friday 16:00 ET when the next session is Monday) instead of a flat 15 hours. Rows stay in the database. FED rows still do not enter the ten scores. Other publishers stay on the 90-day read. A book whose only new rows are First Squawk still takes the Yahoo news fallback, the same path an empty book took on Oct 2. No lexicon change. No sizing, stop, exit, universe, or cadence change. The scoring formula is unchanged. Exception 10 is what the score does with those rows. |
| 2026-10-06 | Freeze exception 10, directed by the operator: First Squawk stays stored, and each scan logs the S those lines would have added (`squawk_s=` on the score line and the Discord telemetry bits). That contribution is 0 in the score, so it cannot move S or change who clears 70. The trading stamp was pinned at `v7a25760` (the Oct 2 session) and no longer follows git HEAD, `GIT_COMMIT`, or `RENDER_GIT_COMMIT`. Exception 11 moves that pin. The four lots left unmarked through Oct 5 and Oct 6 append `excluded_from_measurement=y` on their TRADE lines and are left out of ops.report. No sizing, stop, exit, universe, or cadence change. The scoring formula is unchanged. |
| 2026-10-06 | Freeze exception 11, directed by the operator, before the session count. Three record and schedule bugs, and one exit rule for contracts that have already expired. (1) The session criticals counter incremented on any Discord message containing the word CRITICAL, including the morning brief headers, so every session failed the go/no-go bar of zero unhandled CRITICALs. The count now increments only when a send is an explicit CRITICAL alert (`critical=True`): mark failure, UNRESOLVED, UNPROTECTED, earnings-blackout pages, account-size mismatch, durable-write failure, LLM dual-fail, the macro-loop error page, no-market-data, scan-discord failure, and score-data outage. The brief, TRADE lines, and local prints do not count. Oct 2's posted SESSION line still says `criticals 1`. Under this rule that session's count is 0: the log's only CRITICAL text is a local earnings print that was not delivered, and the brief was a normal send. (2) A contract whose expiration date is strictly before the session date has no quote. Exception 5 left that flatten open and paged UNRESOLVED, which holds a gate slot. It now settles at intrinsic value from the underlying's official close on the expiration date: call max(0, close − strike), put max(0, strike − close), per share. Reason is exactly `EXPIRED` (carry review does not prefix `CARRY_`). The lot is removed and the slot frees. A stale option mark is not the fill, including a last bid from an earlier session. Out-of-the-money intrinsic 0 still closes and is not tagged fill=est. If that daily close cannot be fetched, the lot stays open, no price is invented, and UNRESOLVED is not paged; the next exit pass tries once more, through the paced Yahoo client, and a crumb backoff is not retried. The four gap lots stay excluded by id. A later expired lot uses the same settlement and is not excluded unless its id is one of those four. This is the exit-rule change that moves the trading stamp from `v7a25760` to `v9c3cc4d`. Score, size, stops, the gate, the universe, and cadence are unchanged. Non-expired exits are unchanged. (3) The overnight wait slept a precomputed stack of hour-long chunks. macOS sleep froze that timer, so a 07:50 wake resumed the remainder and could miss the open. The live night wait now sleeps at most 60 seconds and compares the wall clock to the next 09:15 ET each time. A wake at 07:50 is back on that target within one leftover chunk. First Squawk is still read once on night entry, at most once per wall-clock hour (one catch-up after a multi-hour jump, not once per missed hour), and not after the target. The precomputed hour schedule is unchanged. This wait does not score, size, exit, or gate. |

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

3. Done 2026-10-06 as freeze exception 8, scoped by exception 9, and
   shadowed by exception 10. Public @FirstSquawk posts, read from the
   profile page because the timeline URL returns 429. Named lines are
   stored. The score contribution is 0 until the measurement ends.
   FED-only lines do not enter the ten scores.
