"""
Spend and abuse limits for the public demo.

These are not hardening-for-its-own-sake. The demo talks to Gemini Live, which
bills per minute of audio, against one API key belonging to one person. A public
URL with no ceiling is an open tap on that person's card, and a link that gets
posted anywhere will find bots within hours.

So every limit here answers a specific way the bill runs away:

  session length   one visitor leaving a tab open all night
  idle timeout     a tab left open with nobody talking
  concurrency      a crowd, or one script opening many sockets
  per-IP sessions  one person cycling reconnects
  daily budget     the backstop for everything not anticipated above

The daily budget is the one that matters most: whatever else leaks, total spend
for the day stops at a number the owner chose. It is intentionally the crudest
mechanism here, because it is the one that must not fail.

State is in-process. A single container is the deployment target, and a restart
resetting the counters is acceptable for a demo — reaching for Redis would add a
dependency and a failure mode to protect a number that is already approximate.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Every limit is overridable from the environment so the owner can tune spend
# without a redeploy.
MAX_SESSION_SECONDS = _env_int("DEMO_MAX_SESSION_SECONDS", 300)      # 5 min per visitor
IDLE_TIMEOUT_SECONDS = _env_int("DEMO_IDLE_TIMEOUT_SECONDS", 60)     # silence before closing
MAX_CONCURRENT = _env_int("DEMO_MAX_CONCURRENT", 3)                  # simultaneous conversations
MAX_SESSIONS_PER_IP_HOUR = _env_int("DEMO_MAX_SESSIONS_PER_IP_HOUR", 6)
DAILY_AUDIO_MINUTES = _env_int("DEMO_DAILY_AUDIO_MINUTES", 120)      # hard spend ceiling


@dataclass
class _Day:
    """Audio minutes spent since the last rollover."""
    stamp: str = ""
    seconds: float = 0.0


@dataclass
class Limiter:
    active: int = 0
    day: _Day = field(default_factory=_Day)
    per_ip: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    # ── budget ───────────────────────────────────────────────────────────────
    def _roll(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.day.stamp != today:
            self.day = _Day(stamp=today, seconds=0.0)

    def budget_left_seconds(self) -> float:
        self._roll()
        return max(0.0, DAILY_AUDIO_MINUTES * 60 - self.day.seconds)

    def spend(self, seconds: float) -> None:
        self._roll()
        self.day.seconds += max(0.0, seconds)

    # ── admission ────────────────────────────────────────────────────────────
    def may_start(self, ip: str) -> tuple[bool, str]:
        """Whether a new session is allowed, and if not, what to tell the visitor.

        The messages are written to be read by a stranger who has no idea how
        this is hosted — "try again in a few minutes" rather than a limit name.
        """
        self._roll()

        if self.budget_left_seconds() <= 0:
            return False, ("This demo has used up today's conversation budget. "
                           "Please come back tomorrow.")

        if self.active >= MAX_CONCURRENT:
            return False, (f"All {MAX_CONCURRENT} demo lines are busy right now. "
                           "Please try again in a few minutes.")

        window = self.per_ip[ip]
        cutoff = time.time() - 3600
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= MAX_SESSIONS_PER_IP_HOUR:
            return False, ("You have started several sessions in the last hour. "
                           "Please give it a little while.")

        return True, ""

    def started(self, ip: str) -> None:
        self.active += 1
        self.per_ip[ip].append(time.time())

    def ended(self, seconds: float) -> None:
        self.active = max(0, self.active - 1)
        self.spend(seconds)

    # ── introspection, for /healthz ──────────────────────────────────────────
    def snapshot(self) -> dict:
        self._roll()
        return {
            "active": self.active,
            "max_concurrent": MAX_CONCURRENT,
            "budget_minutes_total": DAILY_AUDIO_MINUTES,
            "budget_minutes_left": round(self.budget_left_seconds() / 60, 1),
            "session_cap_seconds": MAX_SESSION_SECONDS,
        }


limiter = Limiter()
