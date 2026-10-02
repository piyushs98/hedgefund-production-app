"""Read-only paths for the dashboard and the measurement report.

SQLite is opened with URI mode=ro and PRAGMA query_only, so this process
cannot create a table, take a write lock, or checkpoint the bot's WAL.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Paths:
    root: Path
    news_db: Path
    hedge_db: Path
    active_trades: Path
    logs_dir: Path
    env_file: Path


def default_paths() -> Paths:
    """Repo-relative files. Env overrides match the bot's path knobs only."""
    root = ROOT

    def _p(env_name: str, default: Path) -> Path:
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            return default
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        return path

    return Paths(
        root=root,
        news_db=_p("NEWS_DB_PATH", root / "data" / "news_room.db"),
        hedge_db=_p("HEDGE_DB_PATH", root / "data" / "hedge_fund.db"),
        active_trades=_p("ACTIVE_TRADES_PATH", root / "active_trades.json"),
        logs_dir=root / "logs",
        env_file=root / ".env",
    )


def connect_ro(path: Path) -> sqlite3.Connection | None:
    """Open an existing database read-only. Missing files are not created."""
    if not path.is_file():
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    out = []
    for row in rows:
        name = str(row[0])
        if name.startswith("sqlite_"):
            continue
        if _NAME.fullmatch(name):
            out.append(name)
    return out


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not _NAME.fullmatch(table):
        return []
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]


def table_count(conn: sqlite3.Connection, table: str) -> int:
    if not _NAME.fullmatch(table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def tail_lines(path: Path, limit: int = 50, max_bytes: int = 256_000) -> list[str]:
    """Last `limit` lines. Reads only the tail of a large log."""
    if not path.is_file():
        return []
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    lines = [ln.rstrip("\n") for ln in text.splitlines() if ln.strip()]
    if limit <= 0:
        return []
    return lines[-limit:]


def newest_bot_log(logs_dir: Path) -> Path | None:
    if not logs_dir.is_dir():
        return None
    files = [p for p in logs_dir.glob("bot-*.log") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def reset_ledger_flag(env_file: Path) -> str:
    """Return only the RESET_LEDGER_ON_BOOT value. Other lines are ignored."""
    if not env_file.is_file():
        return "missing"
    try:
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "unreadable"
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        if key.strip() != "RESET_LEDGER_ON_BOOT":
            continue
        text = val.strip().strip('"').strip("'")
        return text if text else "empty"
    return "unset"
