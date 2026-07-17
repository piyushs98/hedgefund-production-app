"""
circuit_breaker.py — Safety Gate / Kill-Switch (Task 3b).

Administrative circuit breaker for the data-ingestion layer. If real-time
web requests (options chains, pivots, news) fail N times consecutively,
the breaker OPENS: portfolio scans are suspended, one alert is broadcast,
and the system waits out a cooldown before probing again (half-open).

State is persisted to hedge_fund.db so a tmux restart cannot silently
reset a tripped breaker.

Usage:
    breaker = CircuitBreaker()
    if breaker.is_open(): skip scan
    breaker.record_failure("options_chain:AAPL")
    breaker.record_success("options_chain:AAPL")
"""

import sqlite3
import time
import os

import config

try:
    import broadcaster
except Exception:  # keeps module importable in stripped-down test envs
    broadcaster = None


class CircuitBreaker:
    def __init__(self, failure_threshold=5, cooldown_seconds=900, db_path=None):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.db_path = db_path or config.HEDGE_DB_PATH
        self._init_table()

    def _init_table(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    consecutive_failures INTEGER DEFAULT 0,
                    state TEXT DEFAULT 'CLOSED',
                    opened_at_epoch REAL,
                    last_failure_source TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO circuit_breaker_state (id, consecutive_failures, state)
                VALUES (1, 0, 'CLOSED')
            """)
            conn.commit()

    def _read(self):
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            row = conn.execute(
                "SELECT consecutive_failures, state, opened_at_epoch, last_failure_source "
                "FROM circuit_breaker_state WHERE id = 1"
            ).fetchone()
        return {
            "failures": row[0] or 0,
            "state": row[1] or "CLOSED",
            "opened_at": row[2],
            "last_source": row[3],
        }

    def _write(self, failures=None, state=None, opened_at=None, source=None):
        sets, params = ["updated_at = CURRENT_TIMESTAMP"], []
        if failures is not None:
            sets.append("consecutive_failures = ?"); params.append(failures)
        if state is not None:
            sets.append("state = ?"); params.append(state)
        if opened_at is not None:
            sets.append("opened_at_epoch = ?"); params.append(opened_at)
        if source is not None:
            sets.append("last_failure_source = ?"); params.append(source)
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute(
                f"UPDATE circuit_breaker_state SET {', '.join(sets)} WHERE id = 1", params
            )
            conn.commit()

    # ------------------------------------------------------------------
    def record_failure(self, source="unknown"):
        s = self._read()
        failures = s["failures"] + 1
        print(f"🔌 [Circuit Breaker] Failure {failures}/{self.failure_threshold} from '{source}'.")
        if failures >= self.failure_threshold and s["state"] != "OPEN":
            self._write(failures=failures, state="OPEN", opened_at=time.time(), source=source)
            msg = (
                f"🛑 **[KILL-SWITCH ENGAGED]** Circuit breaker OPEN after {failures} consecutive "
                f"data-extraction failures (last: `{source}`). Portfolio scans suspended for "
                f"{self.cooldown_seconds // 60} minutes; a single probe will test recovery afterwards."
            )
            print(msg)
            if broadcaster:
                try:
                    broadcaster.send_discord_alert(msg)
                except Exception as e:
                    print(f"[Circuit Breaker] Alert broadcast failed: {e}")
        else:
            self._write(failures=failures, source=source)

    def record_success(self, source="unknown"):
        s = self._read()
        if s["state"] == "HALF_OPEN":
            print(f"✅ [Circuit Breaker] Probe via '{source}' succeeded — breaker CLOSED.")
            self._write(failures=0, state="CLOSED", opened_at=0.0)
            if broadcaster:
                try:
                    broadcaster.send_discord_alert(
                        "✅ **[KILL-SWITCH RELEASED]** Data feeds recovered; circuit breaker closed. "
                        "Resuming portfolio scans."
                    )
                except Exception:
                    pass
        elif s["failures"]:
            self._write(failures=0)

    def is_open(self):
        """True while scans must stay suspended. Transitions OPEN -> HALF_OPEN
        automatically after cooldown so the next scan acts as the probe."""
        s = self._read()
        if s["state"] == "OPEN":
            opened = s["opened_at"] or 0
            if time.time() - opened >= self.cooldown_seconds:
                print("🔌 [Circuit Breaker] Cooldown elapsed — entering HALF_OPEN probe mode.")
                self._write(state="HALF_OPEN")
                return False
            return True
        return False

    def status(self):
        return self._read()
