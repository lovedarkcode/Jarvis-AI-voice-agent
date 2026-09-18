"""
core/env_config.py — the one place the Gemini API key comes from.

THE RULE
    The key lives in .env. Nowhere else. It is never written to
    config/api_keys.json, never typed into the interface, and never persisted by
    the app at all — the app only ever reads it.

WHY IT MOVED OUT OF api_keys.json
    api_keys.json is the settings file: voice, assistant name, audio devices,
    plugin toggles, per-plugin config. The app rewrites it constantly, from the
    Qt thread, whenever any of those change. A secret living in a file that is
    rewritten on every settings change is a secret with many chances to be
    copied, backed up or committed by accident — and the rewrite path
    (_on_setup_done) did in fact clobber the whole file.

    .env is the opposite: written once by a person, read by the app, and matched
    by .gitignore. One file, one purpose.

WHERE THE VALUE IS LOOKED UP, IN ORDER
    1. The real process environment. A key exported in the shell, injected by a
       container, or set by a CI runner wins over the file — that is what people
       expect from a .env convention, and it is the only way to run this without
       writing a secret to disk at all.
    2. .env next to main.py.

    Missing in both is not an error here. It is reported by the caller, which
    knows whether it can carry on without one.

NAME MATCHING IS DELIBERATELY FORGIVING
    GEMINI_API_KEY is the documented name, but a person who typed
    Gemini_API_KEY, GOOGLE_API_KEY or GEMINI_KEY has not made a mistake worth an
    evening of debugging — especially since the failure would surface as
    "API key not valid", which sends them looking at the key instead of at its
    name. Lookup is case-insensitive across a small set of aliases.

RELOADING
    The file's mtime is checked on read, so correcting a bad key in .env takes
    effect on the next reconnect without restarting the app. main.py's connect
    loop asks for the key on every attempt, which makes that recovery automatic:
    fix the file, and the session comes back on its own.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
ENV_PATH = BASE_DIR / ".env"

# First match wins. GEMINI_API_KEY is the documented spelling; the rest are
# what people actually type.
_KEY_ALIASES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "GEMINI_KEY",
    "API_KEY",
)

_lock = threading.Lock()
_cache: dict[str, str] = {}
_cache_mtime: float = -1.0


def _parse_env(text: str) -> dict[str, str]:
    """Minimal .env parser — no dependency, because adding one to read five
    lines of KEY=VALUE would be the heaviest thing in this file.

    Handles what real .env files contain: CRLF endings (this project's own .env
    is CRLF), a UTF-8 BOM, `export ` prefixes, `#` comments, blank lines, and
    values wrapped in single or double quotes. Anything it cannot parse is
    skipped rather than raised on — a stray line must not stop the app booting.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.lstrip("﻿").strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name  = name.strip()
        value = value.strip()
        # Strip a trailing inline comment only when the value is not quoted —
        # a '#' inside a quoted secret is part of the secret.
        if value[:1] in ("'", '"'):
            quote = value[0]
            end   = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        elif "#" in value:
            value = value.split("#", 1)[0].strip()
        if name:
            out[name.upper()] = value.strip()
    return out


def _load() -> dict[str, str]:
    """Return the parsed .env, re-reading it when the file has changed."""
    global _cache, _cache_mtime
    try:
        mtime = ENV_PATH.stat().st_mtime
    except Exception:
        with _lock:
            _cache, _cache_mtime = {}, -1.0
        return {}

    with _lock:
        if mtime != _cache_mtime:
            try:
                _cache = _parse_env(ENV_PATH.read_text(encoding="utf-8", errors="replace"))
                _cache_mtime = mtime
            except Exception as e:
                print(f"[Env] Could not read {ENV_PATH.name}: {e}")
                _cache, _cache_mtime = {}, mtime
        return dict(_cache)


def get(name: str, default: str = "") -> str:
    """One value, process environment first, then .env."""
    upper = name.upper()
    val = os.environ.get(name) or os.environ.get(upper)
    if val and val.strip():
        return val.strip()
    return (_load().get(upper) or default).strip()


def get_api_key() -> str:
    """The Gemini API key, or '' when it has not been set.

    Returns empty rather than raising: every caller here is inside a tool or a
    connect loop that already knows how to report the problem in a way the user
    can act on, and a traceback out of an action thread says far less than
    "add GEMINI_API_KEY to .env".
    """
    for alias in _KEY_ALIASES:
        val = get(alias)
        if val:
            return val
    return ""


def has_api_key() -> bool:
    """Whether a key is present and long enough to be worth trying.

    The length floor catches the two common non-keys — an empty value left after
    `GEMINI_API_KEY=` and a placeholder like `your-key-here` — before they turn
    into an authentication round trip and a misleading error.
    """
    return len(get_api_key()) > 15


def get_os_system() -> str:
    """Which OS the per-platform actions should target.

    Read from OS_SYSTEM in .env when set, otherwise detected. It used to be
    asked for on the setup screen, which made a question out of something the
    machine already knows and could get wrong if someone misclicked.
    """
    val = get("OS_SYSTEM").lower()
    if val in ("windows", "mac", "darwin", "linux"):
        return "mac" if val == "darwin" else val
    import platform
    return {"darwin": "mac", "windows": "windows"}.get(
        platform.system().lower(), "linux")


def env_file_path() -> Path:
    return ENV_PATH


def env_file_exists() -> bool:
    return ENV_PATH.is_file()


def missing_key_message() -> str:
    """The sentence shown when no key is configured. One place, so the
    interface, the console and the logs all say the same thing."""
    if not env_file_exists():
        return (f"No .env file found at {ENV_PATH}. Create one containing:\n"
                f"    GEMINI_API_KEY=your_key_here\n"
                f"then restart. A free key comes from https://aistudio.google.com/apikey")
    return (f"GEMINI_API_KEY is missing or empty in {ENV_PATH.name}. Add:\n"
            f"    GEMINI_API_KEY=your_key_here\n"
            f"then restart. A free key comes from https://aistudio.google.com/apikey")
