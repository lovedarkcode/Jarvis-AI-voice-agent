"""A per-session bridge from hosted JARVIS to a guest's browser extension.

The cloud service never receives control of a browser by itself.  The extension
is installed by, and runs inside, the guest's own Chrome/Edge profile.  Each
tool request crosses the visible JARVIS page, where the guest approves or
rejects it before it reaches the extension.
"""
from __future__ import annotations

import asyncio
import threading
import uuid


class BrowserBridge:
    """Thread-safe browser tool requests for one live WebSocket session."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._outbound: asyncio.Queue[dict] = asyncio.Queue()
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self._lock = threading.Lock()
        self.connected = False

    def set_connected(self, connected: bool) -> None:
        self.connected = connected

    def request(self, action: str, args: dict, timeout: float = 45) -> str:
        """Ask the page to offer an extension action, then wait for its result."""
        if not self.connected:
            return ("The Browser Link extension is not connected. Ask the person to install "
                    "it from the JARVIS page and reload this session.")

        request_id = uuid.uuid4().hex
        ready = threading.Event()
        result: dict = {}
        with self._lock:
            self._pending[request_id] = (ready, result)
        self._loop.call_soon_threadsafe(
            self._outbound.put_nowait,
            {"type": "browser_action", "request_id": request_id,
             "action": action, "args": args},
        )
        if not ready.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            return "The browser action timed out or was not approved."

        if result.get("ok"):
            return str(result.get("result") or "Browser action completed.")
        return str(result.get("error") or "The browser action was declined or failed.")

    async def next_action(self) -> dict:
        return await self._outbound.get()

    def resolve(self, payload: dict) -> None:
        request_id = str(payload.get("request_id") or "")
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if not pending:
            return
        ready, result = pending
        result.update(payload)
        ready.set()

