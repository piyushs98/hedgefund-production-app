"""
Signal gating. Sits BETWEEN scoring and execution.

Scoring answers "is this setup good right now?"
Gating answers "should this become a trade?"

Stage 3 fixes vs the original reference design:
  1. Rank-before-admit: within one scan, candidates that clear persistence
     and direction lock are sorted by score DESC, then admitted against
     max_concurrent. Conviction wins the slot, not config list order.
  2. on_close() frees concurrent slots on REAL position exits only.
  3. rollback_admit() undoes a gate admit when strike/contract selection
     fails after admit (MIN_DTE / required_move / decay / no liquid) so
     entries_today, last_entry_at, and the concurrent slot are restored.
     Cross-process safety: sync_open_from_book() reconciles the in-memory
     book with durable active_trades at each scan start.
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
    last_exit_at: datetime | None = None  # real close only (not rollback)
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
    post_exit_cooldown_minutes: int = 45
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
    # Scorer hard-block (e.g. dead_zone) — surfaces on GATE as distinct reason
    block_reason: str | None = None


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
        # Pre-admit snapshots for rollback_admit (ticker -> prior last_entry_at, entries_today)
        self._admit_snapshots: dict[str, tuple[datetime | None, int]] = {}

    def _st(self, ticker: str) -> TickerState:
        key = str(ticker).upper().strip()
        return self.state.setdefault(key, TickerState())

    def on_close(self, ticker: str, closed_at: datetime | None = None) -> None:
        """
        Free a concurrent slot when a REAL position is closed.

        Sets last_exit_at for GATE_POST_EXIT_COOLDOWN (anti-churn).
        Does not touch entries_today. For failed contract selection after
        admit, use rollback_admit() instead.
        """
        from datetime import timezone as _tz

        key = str(ticker).upper().strip()
        st = self._st(key)
        st.position_open = False
        when = closed_at or datetime.now(_tz.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_tz.utc)
        st.last_exit_at = when
        self._open.discard(key)
        # Real exit: drop any unused admit snapshot
        self._admit_snapshots.pop(key, None)

    def rollback_admit(self, ticker: str) -> bool:
        """
        Reverse a gate admit when no position was actually opened
        (strike selection / Part C filter failure).

        Restores entries_today, last_entry_at (pre-admit values), and
        clears position_open / concurrent slot. Returns True if a snapshot
        was restored.
        """
        key = str(ticker).upper().strip()
        st = self._st(key)
        snap = self._admit_snapshots.pop(key, None)
        st.position_open = False
        self._open.discard(key)
        if snap is not None:
            prev_last, prev_entries = snap
            st.last_entry_at = prev_last
            st.entries_today = max(0, int(prev_entries))
            print(
                f"[Gate] rollback_admit({key}): entries_today→{st.entries_today} "
                f"last_entry_at→{st.last_entry_at}"
            )
            return True
        # Fallback if snapshot missing (should not happen on normal paths)
        if st.entries_today > 0:
            st.entries_today -= 1
        st.last_entry_at = None
        print(f"[Gate] rollback_admit({key}): no snapshot — decremented entries only")
        return False

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

    def _try_admit(
        self,
        ticker: str,
        direction: str,
        score: float,
        now: datetime,
        *,
        closed_this_scan: set[str] | None = None,
    ) -> GateDecision:
        """Apply book/cooldown/cap checks and admit if possible. Call after ranking."""
        c, st = self.cfg, self._st(ticker)
        direction = _norm_direction(direction) or direction
        key = str(ticker).upper().strip()

        # Same-scan close → never re-open (churn / free round-trip)
        blocked = {str(t).upper().strip() for t in (closed_this_scan or ()) if t}
        if key in blocked:
            return GateDecision(
                ticker=ticker, admit=False,
                reason="same_scan_exit: closed this scan — no re-admit",
                score=score, direction=direction,
            )

        if st.position_open and not c.allow_pyramiding:
            return GateDecision(
                ticker=ticker, admit=False, reason="position already open on this ticker",
                score=score, direction=direction,
            )

        # Primary anti-churn: cooldown from last EXIT (not entry)
        if st.last_exit_at is not None and c.post_exit_cooldown_minutes > 0:
            exit_at = st.last_exit_at
            now_cmp = now
            if exit_at.tzinfo is None and now_cmp.tzinfo is not None:
                exit_at = exit_at.replace(tzinfo=now_cmp.tzinfo)
            elif exit_at.tzinfo is not None and now_cmp.tzinfo is None:
                now_cmp = now_cmp.replace(tzinfo=exit_at.tzinfo)
            try:
                cool_x = (now_cmp - exit_at).total_seconds() / 60.0
            except TypeError:
                cool_x = (
                    now.replace(tzinfo=None) - exit_at.replace(tzinfo=None)
                ).total_seconds() / 60.0
            if cool_x < c.post_exit_cooldown_minutes:
                return GateDecision(
                    ticker=ticker, admit=False,
                    reason=(
                        f"post_exit_cooldown {cool_x:.0f}/"
                        f"{c.post_exit_cooldown_minutes}m"
                    ),
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

        # Snapshot pre-admit state so strike failures can fully roll back
        self._admit_snapshots[key] = (st.last_entry_at, int(st.entries_today))
        st.direction = direction
        st.last_entry_at = now
        st.position_open = True
        st.entries_today += 1
        self._open.add(key)
        return GateDecision(
            ticker=ticker, admit=True,
            reason=f"admitted (score {score:.1f}, streak {st.streak})",
            score=score, direction=direction,
        )

    def process_scan(
        self,
        observations: list[Observation],
        now: datetime,
        closed_this_scan: Iterable[str] | None = None,
    ) -> list[GateDecision]:
        """
        Process one full scan cycle.

        1. Prefilter every observation (updates streaks; PASS resets).
        2. Sort eligible candidates by score descending.
        3. Admit in conviction order against concurrent / cooldown / caps.
        closed_this_scan: tickers closed earlier in this scan — never re-admit.
        """
        decisions: dict[str, GateDecision] = {}
        eligible: list[tuple[str, str, float, str]] = []  # ticker, dir, score, pre_reason
        closed_set = {str(t).upper().strip() for t in (closed_this_scan or ()) if t}

        for obs in observations:
            ticker = str(obs.ticker).upper().strip()
            score = float(obs.score)
            flag = (obs.action_flag or "PASS").upper()
            direction = _norm_direction(obs.direction)
            block_reason = (obs.block_reason or "").strip().lower() or None

            # Scorer hard blocks (dead zone, etc.) — distinct GATE reason, reset streak
            if block_reason:
                use_score = min(score, self.cfg.threshold - 0.1)
                self._prefilter(ticker, None, use_score, now)
                decisions[ticker] = GateDecision(
                    ticker=ticker, admit=False, reason=block_reason,
                    score=score, direction=None,
                )
                continue

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
            dec = self._try_admit(
                ticker, direction, score, now, closed_this_scan=closed_set
            )
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
    r = (reason or "").lower().strip()
    # Exact data-failure tags from scorer block_reason (must stay distinct)
    if r in (
        "no_liq_data",
        "spread_untradeable",
        "no_momentum_data",
        "dead_zone",
    ):
        return r
    if "no_liq_data" in r or "no liq" in r:
        return "no_liq_data"
    if "spread_untradeable" in r or "untradeable" in r:
        return "spread_untradeable"
    if "no_momentum_data" in r or "no momentum" in r:
        return "no_momentum_data"
    if "dead_zone" in r or "dead zone" in r:
        return "dead_zone"
    if "same_scan" in r:
        return "same_scan"
    if "post_exit" in r:
        return "post_exit_cd"
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


def gate_config_from_env() -> GateConfig:
    """
    Build GateConfig from config.py / environment.

    Daily cap and other limits are env-tunable without a code change:
      GATE_MAX_ENTRIES_PER_TICKER, GATE_MAX_CONCURRENT, GATE_PERSIST_CYCLES,
      GATE_FLIP_LOCK_MINUTES, GATE_FLIP_OVERRIDE_SCORE,
      GATE_REENTRY_COOLDOWN_MINUTES. Restart the process after changing env.
    """
    try:
        import config as _cfg
    except Exception:
        return GateConfig()
    return GateConfig(
        threshold=float(getattr(_cfg, "EXECUTE_THRESHOLD", 70.0)),
        persist_cycles=int(getattr(_cfg, "GATE_PERSIST_CYCLES", 2)),
        flip_lock_minutes=int(getattr(_cfg, "GATE_FLIP_LOCK_MINUTES", 60)),
        flip_override_score=float(getattr(_cfg, "GATE_FLIP_OVERRIDE_SCORE", 85.0)),
        reentry_cooldown_minutes=int(
            getattr(_cfg, "GATE_REENTRY_COOLDOWN_MINUTES", 45)
        ),
        post_exit_cooldown_minutes=int(
            getattr(_cfg, "GATE_POST_EXIT_COOLDOWN_MINUTES", 45)
        ),
        max_entries_per_ticker=int(
            getattr(_cfg, "GATE_MAX_ENTRIES_PER_TICKER", 3)
        ),
        max_concurrent=int(getattr(_cfg, "GATE_MAX_CONCURRENT", 5)),
        allow_pyramiding=False,
    )


def get_gate() -> SignalGate:
    global _GATE
    if _GATE is None:
        cfg = gate_config_from_env()
        _GATE = SignalGate(cfg)
        print(
            f"[Gate] init max_entries/ticker={cfg.max_entries_per_ticker} "
            f"max_concurrent={cfg.max_concurrent} persist={cfg.persist_cycles} "
            f"flip_lock={cfg.flip_lock_minutes}m "
            f"entry_cd={cfg.reentry_cooldown_minutes}m "
            f"post_exit_cd={cfg.post_exit_cooldown_minutes}m "
            f"threshold={cfg.threshold}"
        )
        log_entry_filter_config()
        try:
            import config as _cfg
            if hasattr(_cfg, "log_scoring_config"):
                _cfg.log_scoring_config()
        except Exception as sc_err:
            print(f"[Scoring] config unavailable: {sc_err}")
    return _GATE


def log_entry_filter_config() -> None:
    """Boot / init: print resolved Part C knobs (env-overridable at process start)."""
    try:
        import config as _cfg
        print(
            f"[EntryFilters] MIN_DTE={getattr(_cfg, 'MIN_DTE', 1)} "
            f"REQUIRED_MOVE_ATR_K={getattr(_cfg, 'REQUIRED_MOVE_ATR_K', 0.5)} "
            f"EXIT_MAX_DECAY_DENSITY={getattr(_cfg, 'EXIT_MAX_DECAY_DENSITY', 8.0)}%/hr "
            f"MAX_CONTRACT_SPREAD_PCT={getattr(_cfg, 'MAX_CONTRACT_SPREAD_PCT', 8.0)}% "
            f"MIN_EXTRINSIC_PCT={getattr(_cfg, 'MIN_EXTRINSIC_PCT', 10.0)} "
            f"MAX_EXPIRY_CALENDAR_DTE={getattr(_cfg, 'MAX_EXPIRY_CALENDAR_DTE', 10)} "
            f"(env-tunable; restart process to apply)"
        )
    except Exception as e:
        print(f"[EntryFilters] config unavailable: {e}")


def reset_gate_for_tests(cfg: GateConfig | None = None) -> SignalGate:
    """Replace singleton — tests only."""
    global _GATE
    _GATE = SignalGate(cfg or GateConfig())
    return _GATE
