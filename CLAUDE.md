# CLAUDE.md

## This repo is FROZEN

Frozen 2026-08-24 at `741584e` for a 30-session measurement period.
See `FREEZE.md`.

Do not suggest, plan, or implement improvements to scoring, gating,
sizing, stops, exits, universe, cadence, LLM routing, or config/env
knobs. The point of the freeze is that the code under the TRADE and
SESSION lines does not move.

The only work that is in-bounds:

- a bug that stops trading entirely
- a bug that corrupts the TRADE / SESSION record
- a security or credential issue

Anything else, including "small" config tweaks
(`MAX_CONTRACT_SPREAD_PCT`, `RISK_PER_TRADE_PCT`, `EXECUTE_THRESHOLD`,
weights, day_cap, MIN_PREMIUM, blackout window, …), waits until the
30 sessions are done. Record any allowed exception in `FREEZE.md` with
date and reason.

Deploys wipe the on-disk ledger. Do not deploy unless the exception
rules above require it. Discord is the durable store.

Queued, not now: ATR/delta stops; pivot-weight question.
