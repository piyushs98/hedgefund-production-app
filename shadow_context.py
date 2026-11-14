"""Shadow tags for one open. The live score is not changed.

context_adj is the points the admit line would move. It comes from the
headline month-over-month print only. Core month-over-month is recorded
beside it and is not added. would_flip tests a cross of 70.
would_flip_80 tests a cross of 80. would_block is true only when the
entry sits inside a tier-1 release window. Nothing here writes
trade_history, the gate, or the score.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import shadow_calendar
import shadow_news

_NUMBER = re.compile(r"([+-]?\d+(?:\.\d+)?)(%|[KkMmBb])?")


def inflation_family(name: str) -> str | None:
    """CPI, PPI, PCE, and import prices. Anything else stays off the map."""
    text = (name or "").lower()
    if "producer price" in text or re.search(r"\bppi\b", text):
        return "ppi"
    if "consumer price" in text or re.search(r"\bcpi\b", text):
        return "cpi"
    if "import" in text and "price" in text:
        return "import_prices"
    if "personal income" in text or re.search(r"\bpce\b", text):
        return "pce"
    return None


def parse_print(raw) -> tuple[float, str] | None:
    """One Actual/Forecast/Prior token. A mismatched shape stays None."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    match = _NUMBER.fullmatch(text)
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    if suffix is None:
        return value, "num"
    if suffix == "%":
        return value, "%"
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}[suffix.lower()]
    return value * mult, "suffix"


def surprise_label(actual, forecast) -> tuple[float | None, str | None]:
    """HOT, COLD, or IN LINE. Missing or mixed units stay (None, None)."""
    left = parse_print(actual)
    right = parse_print(forecast)
    if left is None or right is None or left[1] != right[1]:
        return None, None
    gap = left[0] - right[0]
    if left[0] == right[0]:
        return 0.0, "IN LINE"
    if gap > 0:
        return gap, "HOT"
    return gap, "COLD"


def bias_for(family: str | None, label: str | None) -> str | None:
    """Hot inflation is bearish. Cold inflation is bullish. Else null."""
    if family not in shadow_calendar.INFLATION_FAMILIES:
        return None
    if label == "HOT":
        return "bearish"
    if label == "COLD":
        return "bullish"
    return None


def context_adj(direction, bias: str | None, label: str | None, family: str | None) -> int | None:
    """±8 for a directional inflation bias. In-line inflation is 0. Otherwise null."""
    side = (direction or "").strip().upper()
    if side not in ("CALL", "PUT"):
        return None
    if bias == "bearish":
        return -shadow_calendar.CONTEXT_POINTS if side == "CALL" else shadow_calendar.CONTEXT_POINTS
    if bias == "bullish":
        return shadow_calendar.CONTEXT_POINTS if side == "CALL" else -shadow_calendar.CONTEXT_POINTS
    if family in shadow_calendar.INFLATION_FAMILIES and label == "IN LINE":
        return 0
    return None


def _crosses(score, adj, line: int) -> bool | None:
    """True when adding adj crosses line. The score is not moved."""
    if score is None or adj is None:
        return None
    try:
        live = float(score)
        moved = live + float(adj)
    except (TypeError, ValueError):
        return None
    return (live >= line) != (moved >= line)


def would_flip(score, adj) -> bool | None:
    """True when adding adj crosses 70."""
    return _crosses(score, adj, shadow_calendar.ADMIT_THRESHOLD)


def would_flip_80(score, adj) -> bool | None:
    """True when adding adj crosses 80. Same comparison as would_flip."""
    return _crosses(score, adj, shadow_calendar.ADMIT_LINE_80)


def _tier(event: dict) -> int | None:
    raw = event.get("tier")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def would_block(entry_at: datetime | None, events: list[dict]) -> bool | None:
    """True inside [release−30, release+15] for a tier-1 release.

    Tier 2 is searched and does not block. A missing entry time stays
    null. This flag is not a live gate.
    """
    if entry_at is None or entry_at.tzinfo is None:
        return None
    moment = entry_at.astimezone(timezone.utc)
    blocked = False
    for event in events:
        if _tier(event) != shadow_calendar.TIER_SEARCH_AND_BLOCK:
            continue
        release = event.get("release_at")
        if not isinstance(release, datetime) or release.tzinfo is None:
            continue
        start = release - timedelta(minutes=30)
        end = release + timedelta(minutes=15)
        if start <= moment <= end:
            blocked = True
    return blocked


def _parse(value) -> datetime | None:
    import first_squawk

    return first_squawk.parse_time(value)


def _pick_event(entry_at: datetime, events: list[dict]) -> dict | None:
    """Latest print at or before the entry. An inflation print wins a tie."""
    moment = entry_at.astimezone(timezone.utc)
    known = []
    for event in events:
        release = event.get("release_at")
        if not isinstance(release, datetime) or release.tzinfo is None:
            continue
        if release.astimezone(timezone.utc) <= moment:
            known.append(event)
    if not known:
        return None
    inflation = [
        event for event in known if inflation_family(event.get("name") or "")
    ]
    pool = inflation or known
    pool.sort(key=lambda event: (event["release_at"], event.get("event_id") or ""))
    return pool[-1]


def _pick_item(event_id: str, items: list[dict]) -> dict | None:
    rows = [item for item in items if item.get("event_id") == event_id]
    rows.sort(key=lambda item: item.get("published_at") or "")
    for item in rows:
        if item.get("actual") is not None or item.get("forecast") is not None:
            return item
    return rows[0] if rows else None


def _component_of(item: dict) -> str | None:
    """Stored component, else the post text. A bare number stays untagged."""
    stored = item.get("component")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    text = item.get("raw_text") or ""
    if not str(text).strip():
        return None
    import first_squawk

    return first_squawk.release_component(text)


def _pick_named(rows: list[dict], component: str) -> dict | None:
    matched = [item for item in rows if _component_of(item) == component]
    matched.sort(key=lambda item: item.get("published_at") or "")
    for item in matched:
        if item.get("actual") is not None or item.get("forecast") is not None:
            return item
    return matched[-1] if matched else None


def _print_fields(direction, family, item, score) -> dict:
    actual = item.get("actual") if item else None
    forecast = item.get("forecast") if item else None
    prior = item.get("prior") if item else None
    gap, label = surprise_label(actual, forecast)
    bias = bias_for(family, label)
    adj = context_adj(direction, bias, label, family)
    return {
        "actual": actual,
        "forecast": forecast,
        "prior": prior,
        "surprise": gap,
        "label": label,
        "bias": bias,
        "context_adj": adj,
        "would_flip": would_flip(score, adj),
        "would_flip_80": would_flip_80(score, adj),
    }


def tag_trade(
    *,
    direction,
    score,
    entry_at: datetime | None,
    events: list[dict],
    items: list[dict],
) -> dict:
    """One shadow tag. score is copied through and not replaced.

    When the posts name a component, the top-level adjustment is the
    headline month-over-month print. Core month-over-month is stored
    on core_mom and is not added to that adjustment. A post with no
    component still fills the top level, as before.
    """
    family = None
    chosen = None
    headline = None
    core = None
    if entry_at is not None and entry_at.tzinfo is not None:
        chosen = _pick_event(entry_at, events)
    if chosen is not None:
        family = inflation_family(chosen.get("name") or "")
        rows = [
            item for item in items if item.get("event_id") == chosen.get("event_id")
        ]
        if any(_component_of(item) for item in rows):
            headline = _pick_named(rows, "headline_mom")
            core = _pick_named(rows, "core_mom")
        else:
            headline = _pick_item(chosen.get("event_id"), items)
    fields = _print_fields(direction, family, headline, score)
    core_fields = _print_fields(direction, family, core, score) if core else None
    adj = fields["context_adj"]
    return {
        "event_id": None if chosen is None else chosen.get("event_id"),
        "event_name": None if chosen is None else chosen.get("name"),
        "release_at": None if chosen is None else chosen.get("release_at"),
        "actual": fields["actual"],
        "forecast": fields["forecast"],
        "prior": fields["prior"],
        "surprise": fields["surprise"],
        "label": fields["label"],
        "bias": fields["bias"],
        "context_adj": adj,
        "would_flip": fields["would_flip"],
        "would_flip_80": fields["would_flip_80"],
        "core_mom": core_fields,
        "would_block": would_block(entry_at, events),
        "score": score,
        "score_plus_context": None if score is None or adj is None else float(score) + adj,
    }


def _load(db_path) -> tuple[list[dict], list[dict]]:
    path = shadow_news._db_path(db_path)
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, timeout=5.0
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        event_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(calendar_events)")
        }
        tier_sql = ", tier" if "tier" in event_cols else ""
        events = []
        for row in conn.execute(
            f"""
            SELECT event_id, name, release_at, kind{tier_sql}
            FROM calendar_events
            ORDER BY release_at, event_id
            """
        ):
            release = _parse(row["release_at"])
            if release is None:
                continue
            events.append(
                {
                    "event_id": row["event_id"],
                    "name": row["name"],
                    "release_at": release,
                    "kind": row["kind"],
                    "tier": row["tier"] if "tier" in event_cols else None,
                }
            )
        item_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(shadow_items)")
        }
        component_sql = ", component" if "component" in item_cols else ""
        items = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT event_id, actual, forecast, prior, published_at,
                       raw_text{component_sql}
                FROM shadow_items
                WHERE source=?
                ORDER BY published_at
                """,
                (shadow_news.first_squawk.ITEM_SOURCE,),
            )
        ]
    finally:
        conn.close()
    return events, items


def tag_open_from_db(*, direction, score, entry_at, db_path=None) -> dict:
    """Read the shadow store and tag. Does not write."""
    events, items = _load(db_path)
    return tag_trade(
        direction=direction,
        score=score,
        entry_at=entry_at,
        events=events,
        items=items,
    )


def note_open(*, ticker, direction, score, entry_at, db_path=None) -> None:
    """Log one tag after a paper open. A failure does not change the open."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        tag = tag_open_from_db(
            direction=direction,
            score=score,
            entry_at=entry_at,
            db_path=db_path,
        )
    except Exception as exc:
        print(f"[Shadow] context tag failed: {type(exc).__name__}")
        return
    print(
        f"[Shadow] context {ticker} {direction} score={tag['score']} "
        f"event={tag['event_id']} label={tag['label']} bias={tag['bias']} "
        f"context_adj={tag['context_adj']} would_flip={tag['would_flip']} "
        f"would_flip_80={tag['would_flip_80']} "
        f"would_block={tag['would_block']}"
    )
