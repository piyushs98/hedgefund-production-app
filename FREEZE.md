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
| _(none)_ | |

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
