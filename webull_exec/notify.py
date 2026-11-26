"""CRITICAL and status posts. The paper outbox is not written."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class Notifier:
    def __init__(self, store, opener=None):
        self.store = store
        self.opener = opener or urllib.request.urlopen

    def critical(self, key: str, message: str) -> None:
        text = "CRITICAL webull: " + message
        if not self.store.alert_once(key, "CRITICAL", text):
            return
        print(text)
        self._discord(text)

    def info(self, key: str, message: str) -> None:
        if not self.store.alert_once(key, "INFO", message):
            return
        print(message)
        self._discord(message)

    def heartbeat(self) -> None:
        url = (os.environ.get("WEBULL_HEARTBEAT_URL") or "").strip()
        if not url:
            return
        try:
            self.opener(url, timeout=3)
        except Exception:
            print("[webull] heartbeat failed")

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
