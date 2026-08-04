"""
Signal gating. Sits BETWEEN scoring and execution.

Scoring answers "is this setup good right now?"
Gating answers "should this become a trade?"

Stage 3 fixes vs the original reference design:
  1. Rank-before-admit: within one scan, candidates that clear persistence
     and direction lock are sorted by score DESC, then admitted against
     max_concurrent. Conviction wins the slot, not config list order.
  2. on_close() frees concurrent slots; callers must wire every tracker
     exit path. Cross-process safety: sync_open_from_book() reconciles
     the in-memory book with durable active_trades at each scan start
     (Master Bot and Tracker may be separate processes).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


def _norm_direction(direction: str | None) -> str | None:
    if direction is None:
        return None
    d = str(direction).strip().upper()
    if d in ("C", "CALL", "LONG", "BULL"):
        return "C"
    if d in ("P", "PUT", "SHORT", "BEAR"):
        return "P"
    return d[:1] if d else None


@dataclass
class TickerState:
    direction: str | None = None
    streak: int = 0
    last_entry_at: datetime | None = None
    last_flip_at: datetime | None = None
    position_open: bool = False
    entries_today: int = 0


@dataclass
class GateConfig:
    threshold: float = 70.0
    persist_cycles: int = 2
    flip_lock_minutes: int = 60
    flip_override_score: float = 85.0
    reentry_cooldown_minutes: int = 45
    max_entries_per_ticker: int = 3
    max_concurrent: int = 5
    allow_pyramiding: bool = False


@dataclass
class Observation:
    """One ticker's scored posture for a single scan cycle."""
    ticker: str
    score: float
    direction: str | None = None  # required for EXECUTE candidates
    action_flag: str = "PASS"  # "EXECUTE" or "PASS"


@dataclass
class GateDecision:
    ticker: str
    admit: bool
    reason: str
    score: float
    direction: str | None = None
    # rank among admit-eligible candidates this scan (1 = highest score); None if not eligible
    conviction_rank: int | None = None


class SignalGate:
    def __init__(self, cfg: GateConfig | None = None):
        self.cfg = cfg or GateConfig()
        self.state: dict[str, TickerState] = {}
        self._open: set[str] = set()

    def _st(self, ticker: str) -> TickerState:
        key = str(ticker).upper().strip()
        return self.state.setdefault(key, TickerState())

    def on_close(self, ticker: str) -> None:
        """Free a concurrent slot when a position is closed (any exit path)."""
        key = str(ticker).upper().strip()
        st = self._st(key)
        st.position_open = False
        self._open.discard(key)

    def sync_open_from_book(self, open_tickers: Iterable[str]) -> None:
        """
        Reconcile in-memory book with durable open positions.

        Master Bot and Tracker may run in different processes; on_close in
        Tracker cannot update the scan process. Call this at the start of
        every portfolio scan with tickers currently in active_trades.
        """
        wanted = {str(t).upper().strip() for t in open_tickers if t}
        # Close anything we thought open but book no longer holds
        for t in list(self._open):
            if t not in wanted:
                self.on_close(t)
        # Mark book positions open (without counting new entries)
        for t in wanted:
            st = self._st(t)
            st.position_open = True
            self._open.add(t)

    def reset_day(self) -> None:
        self.state.clear()
        self._open.clear()

    def _prefilter(self, ticker: str, direction: str | None, score: float,
                   now: datetime) -> tuple[bool, str, TickerState]:
        """
        Update streak / direction-lock state. Return (eligible_for_rank, reason, state).
        Does NOT admit and does NOT check concurrent / cooldown / daily cap.
        """
        c, st = self.cfg, self._st(ticker)
        direction = _norm_direction(direction)

        if score < c.threshold or direction is None:
            st.streak = 0
            return False, f"score {score:.1f} below {c.threshold:.0f}", st

        # Persistence streak (same direction)
        if st.direction in (None, direction):
            st.streak = st.streak + 1
        else:
            st.streak = 1

        # Direction lock after a real entry
        if st.direction and direction != st.direction and st.last_entry_at:
            age = (now - st.last_entry_at).total_seconds() / 60.0
            if age < c.flip_lock_minutes and score < c.flip_override_score:
                st.streak = 0
                # Keep prior direction as the locked thesis until unlock
                return False, (
                    f"direction lock: flipped {st.direction}->{direction} after "
                    f"{age:.0f}m, score {score:.1f} < {c.flip_override_score:.0f}"
                ), st
            st.last_flip_at = now

        # Record latest observed direction intent (even if not yet admitted)
        st.direction = direction

        if st.streak < c.persist_cycles:
            return False, f"persistence {st.streak}/{c.persist_cycles}", st

        return True, f"eligible (score {score:.1f}, streak {st.streak})", st

    def _try_admit(self, ticker: str, direction: str, score: float,
                   now: datetime) -> GateDecision:
        """Apply book/cooldown/cap checks and admit if possible. Call after ranking."""
        c, st = self.cfg, self._st(ticker)
        direction = _norm_direction(direction) or direction

        if st.position_open and not c.allow_pyramiding:
            return GateDecision(
                ticker=ticker, admit=False, reason="position already open on this ticker",
                score=score, direction=direction,
            )

        if st.last_entry_at:
            cool = (now - st.last_entry_at).total_seconds() / 60.0
            if cool < c.reentry_cooldown_minutes:
                return GateDecision(
                    ticker=ticker, admit=False,
                    reason=f"cooldown {cool:.0f}/{c.reentry_cooldown_minutes}m",
                    score=score, direction=direction,
                )

        if st.entries_today >= c.max_entries_per_ticker:
            return GateDecision(
                ticker=ticker, admit=False,
                reason=f"daily cap {c.max_entries_per_ticker} reached",
                score=score, direction=direction,
            )

        if len(self._open) >= c.max_concurrent:
            return GateDecision(
                ticker=ticker, admit=False,
                reason=f"book full ({c.max_concurrent} concurrent)",
                score=score, direction=direction,
            )

        st.direction = direction
        st.last_entry_at = now
        st.position_open = True
        st.entries_today += 1
        self._open.add(str(ticker).upper().strip())
        return GateDecision(
            ticker=ticker, admit=True,
            reason=f"admitted (score {score:.1f}, streak {st.streak})",
            score=score, direction=direction,
        )

    def process_scan(
        self,
        observations: list[Observation],
        now: datetime,
    ) -> list[GateDecision]:
        """
        Process one full scan cycle.

        1. Prefilter every observation (updates streaks; PASS resets).
        2. Sort eligible candidates by score descending.
        3. Admit in conviction order against concurrent / cooldown / caps.
        """
        decisions: dict[str, GateDecision] = {}
        eligible: list[tuple[str, str, float, str]] = []  # ticker, dir, score, pre_reason

        for obs in observations:
            ticker = str(obs.ticker).upper().strip()
            score = float(obs.score)
            flag = (obs.action_flag or "PASS").upper()
            direction = _norm_direction(obs.direction)

            # Non-EXECUTE: still observe so streaks reset when thesis dies
            if flag != "EXECUTE" or direction is None or score < self.cfg.threshold:
                # Force below-threshold path when scorer already said PASS
                use_score = score if flag == "EXECUTE" else min(score, self.cfg.threshold - 0.1)
                use_dir = direction if flag == "EXECUTE" else None
                ok, reason, _st = self._prefilter(ticker, use_dir, use_score, now)
                decisions[ticker] = GateDecision(
                    ticker=ticker, admit=False, reason=reason,
                    score=score, direction=use_dir,
                )
                continue

            ok, reason, _st = self._prefilter(ticker, direction, score, now)
            if not ok:
                decisions[ticker] = GateDecision(
                    ticker=ticker, admit=False, reason=reason,
                    score=score, direction=direction,
                )
            else:
                eligible.append((ticker, direction or "", score, reason))

        # Rank by conviction (score DESC), stable tie-break by ticker name
        eligible.sort(key=lambda x: (-x[2], x[0]))

        for rank, (ticker, direction, score, _pre) in enumerate(eligible, start=1):
            dec = self._try_admit(ticker, direction, score, now)
            dec.conviction_rank = rank
            decisions[ticker] = dec

        # Preserve observation order in output
        ordered: list[GateDecision] = []
        seen = set()
        for obs in observations:
            t = str(obs.ticker).upper().strip()
            if t in decisions and t not in seen:
                ordered.append(decisions[t])
                seen.add(t)
        for t, dec in decisions.items():
            if t not in seen:
                ordered.append(dec)
        return ordered

    def format_scan_summary(self, decisions: list[GateDecision]) -> str:
        """
        Compact one-line Discord / log summary of what the gate did this scan.
        """
        admitted = [d for d in decisions if d.admit]
        rejected = [d for d in decisions if not d.admit]

        # Collapse rejection reasons into counts
        reason_counts: dict[str, int] = {}
        for d in rejected:
            key = _compact_reason(d.reason)
            reason_counts[key] = reason_counts.get(key, 0) + 1

        admit_bits = []
        for d in sorted(admitted, key=lambda x: (-(x.score), x.ticker)):
            admit_bits.append(f"{d.ticker}:{d.direction}@{d.score:.0f}")

        if admit_bits:
            admit_s = "ADMIT " + ",".join(admit_bits)
        else:
            admit_s = "ADMIT none"

        if reason_counts:
            # stable sort by count desc then name
            parts = [
                f"{k}×{v}"
                for k, v in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
            rej_s = "BLOCK " + " ".join(parts)
        else:
            rej_s = "BLOCK none"

        open_n = len(self._open)
        return (
            f"GATE [{open_n}/{self.cfg.max_concurrent} open] {admit_s} | {rej_s}"
        )


def _compact_reason(reason: str) -> str:
    r = reason.lower()
    if "persistence" in r:
        return "persist"
    if "direction lock" in r:
        return "dirlock"
    if "book full" in r:
        return "book_full"
    if "position already open" in r:
        return "pos_open"
    if "cooldown" in r:
        return "cooldown"
    if "daily cap" in r:
        return "day_cap"
    if "below" in r:
        return "below_thr"
    return "other"


# Process-wide singleton (Master Bot scan path). Tracker calls on_close via
# the same module; if processes differ, sync_open_from_book is authoritative.
_GATE: SignalGate | None = None


def get_gate() -> SignalGate:
    global _GATE
    if _GATE is None:
        _GATE = SignalGate()
    return _GATE


def reset_gate_for_tests() -> SignalGate:
    """Replace singleton — tests only."""
    global _GATE
    _GATE = SignalGate()
    return _GATE
