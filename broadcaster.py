"""
broadcaster.py — Hardened Discord broadcaster.

Fixes vs. the previous version:
  1. Discord rejects any message over 2000 characters with HTTP 400. The
     CEO decisions and morning briefings routinely exceed that, which is
     why alerts kept "failing with error code 400". Messages are now
     chunked at <=1900 chars on line boundaries.
  2. requests.post had NO timeout — one stalled webhook call could hang
     the entire trading loop indefinitely. Now 10s timeout + 3 retries
     with backoff, honoring Discord 429 rate-limit responses.
  3. The webhook URL was hardcoded in source (leaked credential). It now
     comes exclusively from the DISCORD_WEBHOOK environment variable.
"""

import os
import time

import requests

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK", "")

MAX_CHUNK = 1900          # headroom under Discord's 2000-char hard limit
REQUEST_TIMEOUT = 10      # seconds
MAX_RETRIES = 3


def _chunk_message(message, limit=MAX_CHUNK):
    """Split on newlines first, hard-split any single oversized line."""
    if len(message) <= limit:
        return [message]
    chunks, current = [], ""
    for line in message.split("\n"):
        while len(line) > limit:               # pathological single line
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _post_chunk(chunk):
    data = {"content": chunk, "username": "Options AI 🤖"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(WEBHOOK_URL, json=data, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429:        # rate limited — honor retry_after
                try:
                    wait = float(resp.json().get("retry_after", 2.0))
                except Exception:
                    wait = 2.0
                print(f"[Broadcaster] Rate limited; waiting {wait:.1f}s "
                      f"(attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(wait + 0.25)
                continue
            print(f"[Broadcaster] Discord returned HTTP {resp.status_code}: "
                  f"{resp.text[:200]}")
            if 500 <= resp.status_code < 600:
                time.sleep(1.5 * attempt)
                continue
            return False                       # 4xx other than 429: don't retry
        except requests.RequestException as e:
            print(f"[Broadcaster] Network error (attempt {attempt}/{MAX_RETRIES}): {e}")
            time.sleep(1.5 * attempt)
    return False


def send_discord_alert(message):
    """Send a message to Discord, chunking as needed. Returns True only if
    every chunk delivered. Never raises — a webhook outage must not be able
    to crash the trading loop."""
    if not message or not str(message).strip():
        return False
    if not WEBHOOK_URL:
        print("[Broadcaster] DISCORD_WEBHOOK env var not set — printing locally instead:")
        print(str(message)[:2000])
        return False

    chunks = _chunk_message(str(message))
    ok = True
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"[Broadcaster] Sending chunk {i}/{len(chunks)} "
                  f"({len(chunk)} chars)...")
        if not _post_chunk(chunk):
            ok = False
        if i < len(chunks):
            time.sleep(0.6)                    # gentle pacing between chunks
    print("[Broadcaster] Delivery complete." if ok
          else "[Broadcaster] Delivery finished with errors.")
    return ok


if __name__ == "__main__":
    send_discord_alert("🚨 **TRADE ALERT (TEST)** 🚨\nThe hardened Broadcaster is online.")
    send_discord_alert("A" * 4500)             # chunking self-test
