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

Deploys still wipe the on-disk ledger. Discord is the durable store:
BOOK_STATE is restored at boot (open lots, equity, buying power, realized,
session counter). TRADE and SESSION lines remain the audit tape.

Deploy is allowed when it is required by the exception rules, or to pick
up a freeze-allowed fix, **provided a BOOK_STATE line is already in the
channel** (or BOOK_STATE_BOOTSTRAP is set). Prefer deploying outside RTH
so intra-session mark history and gate cooldowns are not cut mid-scan.
Do not deploy if the last BOOK_STATE is older than 4 calendar days
without setting CONFIRM_STALE_BOOK.

## Allowed exceptions log

| Date | Reason |
|---|---|
| 2026-08-24 | Before first counted session: SESSION trailing `bp` (col 18); TRADE prints unrounded entry_mid/ask so recovered spread on $1–$2.50 names is not blurred by round(mid, 2). Trading debit/SL/TP still use rounded premium. `MAX_CONTRACT_SPREAD_PCT` still 8.0. |
| 2026-08-30 | Freeze exception 1: Aug 28 option-mark outage left two carried lots unmarked 08:55–13:01 (NVDA −4.58R vs −1.04R stop; MSFT TP luck). Underlying-based fallback stop/TP (`UNDERLYING_STOP`, fill=est) when chain is dark but spot is live; UNPROTECTED CRITICAL after MARK_BLACKOUT_MINUTES (45) with no mark and no spot; mark/kill-switch CRITICAL spam throttled (first, then double / every 10th). Exit-only no longer fans out full chains on a quote failure. |
| 2026-08-30 | Freeze exception 2: THESIS_VOID requires a clean score (pivot, ATR, pct_change, usable chain) and two consecutive clean prints below 55. Dirty open prints (no_liq_data / no_momentum_data / no_pivot_data / no_atr_data / usable=0) log THESIS_SKIP and HOLD. SL/TP/expiry unchanged. Aug 28 discarded; count restarts on the next clean session. Sizing, spread cap, and scoring formula unchanged. |
| 2026-09-14 | Freeze exception 3: Render free-tier 750h cap killed the bot (down since 2026-08-31). Discord BOOK_STATE is now the durable open-book store (write on open/close/14:45/shutdown; restore before first scan). Scoring, gating, sizing, stops, exits, universe, cadence, LLM routing, and config knobs unchanged. |
| 2026-09-29 | Freeze exception 4: night harvest looped every ~95s because the sleep broke on `time() >= 09:15`, which is true all evening. Each pass re-hit Yahoo (~21 ticker calls). Crumb HTTP 429 then quoteSummary 401, and the retries kept the IP throttled into the open, so option chains do not load. Harvest once per upcoming pre-market date; sleep until that 09:15 ET (state checks at least 60 minutes apart, exact remainder only when the pre-market is closer); crumb 429 backs off 15 min, doubling, cap 4 h, and further getcrumb calls in the window are not sent; a failed earnings fetch keeps the last-known print date, and a calendar with no known dates pages CRITICAL instead of reading as a clear book. Boot banner reads `EXIT_INTERVAL_SECONDS` / `FULL_SCAN_INTERVAL_SECONDS`. No scoring, sizing, stop, exit, universe, or cadence-knob change. |

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
