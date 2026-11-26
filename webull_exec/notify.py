"""CRITICAL and status posts. The paper outbox is not written."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


class Notifier:
    def __init__(self, store, opener=None, *, drill=False, drill_log=None):
        self.store = store
        self.drill = bool(drill)
        self.drill_log = Path(drill_log) if drill_log else None
        self.opener = opener or urllib.request.urlopen

    def critical(self, key: str, message: str) -> None:
        text = "CRITICAL webull: " + message
        if self.drill:
            text = "[DRILL] " + text
        if not self.store.alert_once(key, "CRITICAL", text):
            return
        self._emit(text)

    def info(self, key: str, message: str) -> None:
        text = str(message)
        if self.drill:
            text = "[DRILL] " + text
        if not self.store.alert_once(key, "INFO", text):
            return
        self._emit(text)

    def heartbeat(self) -> None:
        if self.drill:
            self._write_drill("[DRILL] heartbeat")
            return
        url = (os.environ.get("WEBULL_HEARTBEAT_URL") or "").strip()
        if not url:
            return
        try:
            self.opener(url, timeout=3)
        except Exception:
            print("[webull] heartbeat failed")

    def _emit(self, text: str) -> None:
        print(text)
        if self.drill:
            self._write_drill(text)
            return
        self._discord(text)

    def _write_drill(self, text: str) -> None:
        if self.drill_log is None:
            return
        self.drill_log.parent.mkdir(parents=True, exist_ok=True)
        with self.drill_log.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def _discord(self, text: str) -> None:
        url = (os.environ.get("DISCORD_WEBHOOK") or "").strip()
        if not url:
            return
        body = json.dumps({"content": text[:1900]}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "webull-exec"},
        )
        try:
            self.opener(req, timeout=5)
        except urllib.error.HTTPError as exc:
            print(f"[webull] discord post failed HTTP {exc.code}")
        except Exception:
            print("[webull] discord post failed")
