# Measurement freeze

Frozen at COMMIT_PLACEHOLDER on 2026-08-24.
Measurement period: 30 sessions.

This freeze starts after the fill-accounting / TRADE / SESSION record
change. Nothing in that change altered entry, sizing, stops, exits, or
scoring. Equity and the SESSION line are the fill series (buy ask / sell
bid). Triggers still fire on mid.

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
