"""
The general primitive toolset — the whole of what JARVIS can *do*.

This module replaces the old one-file-per-task pattern in actions/. There is no
weather primitive, no flight primitive, no YouTube primitive, because there is
no such thing as a weather *capability* — there is only "open a URL", "fetch
some JSON", "read the screen". Those are primitives. A task is a composition of
them, decided at runtime by the model, not at authoring time by a developer.

The keystone is run_python. Anything nobody anticipated is reachable through it,
which is what makes the assistant open-ended: a new capability costs a sentence
in the conversation, not a new file on disk.

Each primitive exposes the same TOOL shape core/action_loader.py already
validates (name / description / parameters / handler), so registration is
unchanged — but PRIMITIVES below is an explicit list rather than a directory
scan, because this set is meant to stay small and deliberate. Growing it is a
design decision; growing actions/ was an accident.

No sandbox. Per the owner's explicit choice, run_python and shell execute with
the full authority of the user running JARVIS.
"""
from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import textwrap
import traceback
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import quote_plus

# ── Persistent execution namespace ───────────────────────────────────────────
# run_python calls share one namespace for the life of the process, so the model
# can build up state across turns the way a person does in a REPL: import pandas
# once, load a dataframe, then keep querying it over several exchanges without
# paying to rebuild it each time.
_NS: dict = {"__name__": "__jarvis__", "__builtins__": __builtins__}

_PRELUDE = textwrap.dedent("""
    import os, sys, json, re, time, math, shutil, subprocess, webbrowser
    import urllib.request, urllib.parse
    from pathlib import Path
    from datetime import datetime, timedelta
""")


def _bootstrap() -> None:
    """Preload the imports nearly every generated snippet needs, so the model
    does not spend a round trip rediscovering that it wants `os`."""
    try:
        exec(_PRELUDE, _NS)
    except Exception:
        pass


_bootstrap()


def _truncate(s: str, limit: int = 4000) -> str:
    """Tool results ride back through the live audio session, where a wall of
    text costs latency. Keep the head and tail — the middle of a traceback or a
    directory listing is the least informative part."""
    if len(s) <= limit:
        return s
    head, tail = s[: limit // 2], s[-limit // 4:]
    return f"{head}\n\n... [{len(s) - limit} chars elided] ...\n\n{tail}"


# ═══════════════════════════════════════════════════════════════════════════
# 1. run_python — arbitrary capability
# ═══════════════════════════════════════════════════════════════════════════
def run_python(parameters: dict, player=None, **_) -> str:
    code = (parameters.get("code") or "").strip()
    if not code:
        return "No code supplied."

    # Fenced blocks survive from models that narrate their code; strip them
    # rather than failing on a SyntaxError the model cannot see.
    if code.startswith("```"):
        lines = code.splitlines()
        lines = lines[1:] if len(lines) > 1 else lines
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        code = "\n".join(lines).strip()

    _log(f"exec {len(code)} chars", player)
    out, err = io.StringIO(), io.StringIO()
    value = None

    try:
        with redirect_stdout(out), redirect_stderr(err):
            # Run it the way a REPL would: execute everything up to the final
            # statement, and if that last statement is an expression, evaluate it
            # and return its value. Without this, the extremely common shape
            # "import x\nx.something()" silently returns nothing, and the model
            # has to be told to print — a rule it forgets constantly.
            tree = ast.parse(code, "<jarvis>", "exec")
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                head = ast.Module(body=tree.body[:-1], type_ignores=[])
                tail = ast.Expression(body=tree.body[-1].value)
                exec(compile(head, "<jarvis>", "exec"), _NS)
                value = eval(compile(tail, "<jarvis>", "eval"), _NS)
            else:
                exec(compile(tree, "<jarvis>", "exec"), _NS)
                value = _NS.get("result")
    except Exception:
        return _truncate(f"ERROR:\n{traceback.format_exc()}\n\nstdout:\n{out.getvalue()}")

    parts = []
    if out.getvalue().strip():
        parts.append(out.getvalue().strip())
    if err.getvalue().strip():
        parts.append(f"[stderr] {err.getvalue().strip()}")
    if value is not None:
        parts.append(value if isinstance(value, str) else repr(value))

    return _truncate("\n".join(parts)) if parts else "Executed. No output."


# ═══════════════════════════════════════════════════════════════════════════
# 2. shell — the OS as it actually is
# ═══════════════════════════════════════════════════════════════════════════
def shell(parameters: dict, player=None, **_) -> str:
    command = (parameters.get("command") or "").strip()
    if not command:
        return "No command supplied."

    timeout = int(parameters.get("timeout") or 60)
    _log(f"shell: {command[:70]}", player)

    # PowerShell on Windows: almost every useful system query there
    # (Get-CimInstance, Get-Process, the audio cmdlets) has no cmd.exe
    # equivalent worth writing.
    if os.name == "nt":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["/bin/sh", "-c", command]

    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s."
    except Exception as e:
        return f"Command failed to start: {e}"

    body = (p.stdout or "").strip()
    if (p.stderr or "").strip():
        body += f"\n[stderr] {p.stderr.strip()}"
    if p.returncode != 0:
        body += f"\n[exit {p.returncode}]"
    return _truncate(body) or f"Done (exit {p.returncode})."


# ═══════════════════════════════════════════════════════════════════════════
# 3. files — read/write without a round trip through code generation
# ═══════════════════════════════════════════════════════════════════════════
def files(parameters: dict, player=None, current_file=None, **_) -> str:
    action = (parameters.get("action") or "read").lower().strip()
    raw_path = parameters.get("path") or ""
    # An uploaded file is the implied subject when the user says "this document"
    # and names no path — the old dispatcher special-cased that for one tool; it
    # belongs here, where every file action benefits from it.
    if not raw_path and current_file:
        raw_path = current_file
    path = os.path.expandvars(os.path.expanduser(raw_path))
    _log(f"files.{action}: {path}", player)

    try:
        p = Path(path)
        if action == "query":
            return _query_document(parameters, path)

        if action == "read":
            return _truncate(p.read_text(encoding="utf-8", errors="replace"), 6000)

        if action == "write":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(parameters.get("content") or "", encoding="utf-8")
            return f"Wrote {p}."

        if action == "append":
            with p.open("a", encoding="utf-8") as fh:
                fh.write(parameters.get("content") or "")
            return f"Appended to {p}."

        if action == "list":
            target = p if p.is_dir() else p.parent
            entries = sorted(
                f"{'D' if c.is_dir() else 'F'}  {c.name}" for c in target.iterdir()
            )
            return _truncate(f"{target}:\n" + "\n".join(entries))

        if action == "find":
            pattern = parameters.get("pattern") or "*"
            root = p if p.is_dir() else Path.home()
            hits = [str(h) for h in root.rglob(pattern)][:200]
            return _truncate("\n".join(hits)) or "No matches."

        if action == "delete":
            if p.is_dir():
                import shutil as _sh
                _sh.rmtree(p)
            else:
                p.unlink()
            return f"Deleted {p}."

        if action in ("move", "copy"):
            import shutil as _sh
            dest = os.path.expandvars(os.path.expanduser(parameters.get("dest") or ""))
            if not dest:
                return "No destination supplied."
            (_sh.move if action == "move" else _sh.copy2)(str(p), dest)
            return f"{action.title()}d to {dest}."

        return f"Unknown files action: {action}"
    except Exception as e:
        return f"files.{action} failed: {e}"


def _query_document(parameters: dict, path: str) -> str:
    """Answer a question from an ingested document.

    This is retrieval, not a plain read: core/document_store.py has already
    chunked and indexed uploads, and a 300-page PDF cannot simply be handed back
    as text. It lives inside `files` rather than in a document tool of its own
    because "get me the relevant part of this file" is a file operation — the
    old split into a separate document_query tool was the file-per-task habit,
    not a real boundary.
    """
    try:
        from core.document_store import store as _store
    except Exception as e:
        return f"Document store unavailable: {e}"

    question = (parameters.get("question") or parameters.get("content") or "").strip()
    doc = _store.resolve(path or "")
    if doc is None:
        return ("No such document is loaded. Uploaded documents are listed in the "
                "[DOCUMENTS LOADED] block; to read an arbitrary file from disk use "
                "action='read'.")
    if not question:
        return _store.describe(doc.doc_id)
    return _store.build_context(question, doc)


# ═══════════════════════════════════════════════════════════════════════════
# 4. browser — the web, without a tool per website
# ═══════════════════════════════════════════════════════════════════════════
def browser(parameters: dict, player=None, session_memory=None, **_) -> str:
    action = (parameters.get("action") or "open").lower().strip()
    target = (parameters.get("target") or "").strip()
    _log(f"browser.{action}: {target[:60]}", player)

    try:
        if action == "open":
            if not target:
                return "Nothing to open."
            url = target if target.startswith(("http://", "https://")) else f"https://{target}"
            webbrowser.open(url)
            return f"Opened {url}."

        if action == "search":
            # Everything the old weather_report / flight_finder files existed to
            # do is this line with a different query string.
            engine = (parameters.get("engine") or "google").lower()
            roots = {
                "google":  "https://www.google.com/search?q=",
                "youtube": "https://www.youtube.com/results?search_query=",
                "flights": "https://www.google.com/travel/flights?q=",
                "maps":    "https://www.google.com/maps/search/",
                "images":  "https://www.google.com/search?tbm=isch&q=",
            }
            url = roots.get(engine, roots["google"]) + quote_plus(target)
            webbrowser.open(url)
            if session_memory:
                try:
                    session_memory.set_last_search(query=target, response=url)
                except Exception:
                    pass
            return f"Searching {engine} for {target}."

        if action == "fetch":
            # Read a page's text rather than showing it — this is how the model
            # answers a question instead of just displaying something.
            import re as _re
            import urllib.request
            url = target if target.startswith("http") else f"https://{target}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8", errors="replace")
            raw = _re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw,
                          flags=_re.S | _re.I)
            text = _re.sub(r"<[^>]+>", " ", raw)
            return _truncate(_re.sub(r"\s+", " ", text).strip(), 6000)

        return f"Unknown browser action: {action}"
    except Exception as e:
        return f"browser.{action} failed: {e}"


# ═══════════════════════════════════════════════════════════════════════════
# 5. computer — mouse, keyboard, volume, brightness, apps
# ═══════════════════════════════════════════════════════════════════════════
def computer(parameters: dict, player=None, **_) -> str:
    action = (parameters.get("action") or "").lower().strip()
    value = parameters.get("value")
    _log(f"computer.{action}: {value}", player)

    try:
        import pyautogui
        pyautogui.FAILSAFE = False
    except Exception:
        pyautogui = None

    try:
        if action in ("type", "write"):
            if pyautogui is None:
                return "pyautogui unavailable."
            pyautogui.typewrite(str(value or ""), interval=0.01)
            return "Typed."

        if action in ("press", "key"):
            if pyautogui is None:
                return "pyautogui unavailable."
            keys = [k.strip() for k in str(value or "").split("+") if k.strip()]
            if not keys:
                return "No key given."
            if len(keys) > 1:
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(keys[0])
            return f"Pressed {value}."

        if action == "click":
            if pyautogui is None:
                return "pyautogui unavailable."
            if isinstance(value, str) and "," in value:
                x, y = (int(float(n)) for n in value.split(",")[:2])
                pyautogui.click(x, y)
                return f"Clicked ({x}, {y})."
            pyautogui.click()
            return "Clicked."

        if action == "scroll":
            if pyautogui is None:
                return "pyautogui unavailable."
            pyautogui.scroll(int(value or -500))
            return "Scrolled."

        if action == "volume":
            return _volume(value)

        if action == "brightness":
            try:
                import screen_brightness_control as sbc
                sbc.set_brightness(int(value))
                return f"Brightness {value}%."
            except Exception as e:
                return f"Brightness failed: {e}"

        if action == "open_app":
            return _open_app(str(value or ""))

        return f"Unknown computer action: {action}"
    except Exception as e:
        return f"computer.{action} failed: {e}"


def _volume(value) -> str:
    """Volume by media keys. `value` is up / down / mute, optionally with a step
    count ('up 4'), because one keypress moves ~2% and users mean more."""
    try:
        import pyautogui
    except Exception:
        return "pyautogui unavailable."
    spec = str(value or "up").lower().split()
    direction = spec[0]
    steps = int(spec[1]) if len(spec) > 1 and spec[1].isdigit() else 5
    keymap = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}
    key = keymap.get(direction)
    if key is None:
        return f"Unknown volume direction: {direction}"
    for _ in range(1 if key == "volumemute" else steps):
        pyautogui.press(key)
    return f"Volume {direction}."


def _open_app(name: str) -> str:
    """Launch by name. `start` resolves anything on PATH, in the Start menu, or
    registered as a URL protocol — which covers far more than a hardcoded map of
    app paths ever did."""
    if not name:
        return "No app named."
    try:
        if os.name == "nt":
            subprocess.Popen(
                ["cmd", "/c", "start", "", name],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            subprocess.Popen([name])
        return f"Opened {name}."
    except Exception as e:
        return f"Could not open {name}: {e}"


# ═══════════════════════════════════════════════════════════════════════════
# 6. http — talk to any API
# ═══════════════════════════════════════════════════════════════════════════
def http(parameters: dict, player=None, **_) -> str:
    url = (parameters.get("url") or "").strip()
    if not url:
        return "No URL supplied."
    method = (parameters.get("method") or "GET").upper()
    headers = parameters.get("headers") or {}
    body = parameters.get("body")
    _log(f"http {method} {url[:60]}", player)

    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except Exception:
            headers = {}
    headers.setdefault("User-Agent", "Mozilla/5.0")

    try:
        import urllib.request
        data = None
        if body is not None:
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body)
                headers.setdefault("Content-Type", "application/json")
            data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as r:
            return _truncate(r.read().decode("utf-8", errors="replace"), 6000)
    except Exception as e:
        return f"http {method} failed: {e}"


# ═══════════════════════════════════════════════════════════════════════════
def _log(message: str, player=None) -> None:
    print(f"[Primitive] {message}")
    if player:
        try:
            player.write_log(f"SYS: {message}")
        except Exception:
            pass


# ── Declarations ─────────────────────────────────────────────────────────────
PRIMITIVES = [
    {
        "name": "run_python",
        "description": (
            "Execute Python on this computer and return its output. This is your "
            "general capability: if no other tool fits the request, WRITE CODE HERE "
            "rather than saying you cannot do it. Full standard library, pip packages, "
            "OS access, network. State persists between calls, so variables and imports "
            "from an earlier call are still available. Use it for calculations, data "
            "work, file processing, APIs, automation, system queries — anything."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "code": {
                    "type": "STRING",
                    "description": "Python source to execute. Print, or assign to `result`, to return a value.",
                },
            },
            "required": ["code"],
        },
        "handler": run_python,
    },
    {
        "name": "shell",
        "description": (
            "Run a shell command (PowerShell on Windows) and return its output. "
            "Use for installed CLI tools, package managers, git, system administration, "
            "service control, and anything the OS exposes as a command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "The command line to run."},
                "timeout": {"type": "NUMBER", "description": "Seconds before giving up (default 60)."},
            },
            "required": ["command"],
        },
        "handler": shell,
    },
    {
        "name": "files",
        "description": (
            "Read, write, append, list, find, delete, move or copy files and folders, "
            "and query the contents of an UPLOADED document. "
            "Use action='query' with a question for ANY question about an uploaded PDF, Word, "
            "Excel, CSV or text file — a figure, a date, a name, a clause, a total or a summary. "
            "It returns the relevant passages; answer only from those and say so if they do not "
            "cover it. action='read' is for plain text files on disk. For anything more involved, "
            "use run_python."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":   {"type": "STRING", "description": "read | write | append | list | find | delete | move | copy | query"},
                "path":     {"type": "STRING", "description": "Target path, or an uploaded document's name for query. Defaults to the currently uploaded file. Environment variables and ~ are expanded."},
                "question": {"type": "STRING", "description": "For action='query': what you want to know from the document."},
                "content": {"type": "STRING", "description": "Text to write or append."},
                "dest":    {"type": "STRING", "description": "Destination path for move/copy."},
                "pattern": {"type": "STRING", "description": "Glob pattern for find (e.g. *.pdf)."},
            },
            "required": ["action"],
        },
        "handler": files,
    },
    {
        "name": "browser",
        "description": (
            "Open a site, run a search, or fetch a page's text. "
            "action='search' with engine='google'|'youtube'|'flights'|'maps'|'images' covers "
            "weather, flights, videos, directions and shopping — pick the engine and phrase "
            "the query. action='fetch' returns the page text so you can ANSWER from it "
            "instead of merely displaying it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "open | search | fetch"},
                "target": {"type": "STRING", "description": "URL to open/fetch, or the search query."},
                "engine": {"type": "STRING", "description": "google | youtube | flights | maps | images"},
            },
            "required": ["action", "target"],
        },
        "handler": browser,
    },
    {
        "name": "computer",
        "description": (
            "Control the machine directly: type text, press keys or hotkeys, click, scroll, "
            "set volume or brightness, and launch applications."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "type | press | click | scroll | volume | brightness | open_app"},
                "value": {
                    "type": "STRING",
                    "description": (
                        "Text to type, a key combo ('ctrl+shift+n'), 'x,y' to click, "
                        "'up 5'/'down'/'mute' for volume, 0-100 for brightness, or an app name."
                    ),
                },
            },
            "required": ["action"],
        },
        "handler": computer,
    },
    {
        "name": "http",
        "description": (
            "Make an HTTP request to any API and return the response body. "
            "Use when you need structured data rather than a rendered page."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url":     {"type": "STRING", "description": "Full request URL."},
                "method":  {"type": "STRING", "description": "GET | POST | PUT | DELETE (default GET)."},
                "headers": {"type": "STRING", "description": "Request headers as a JSON object."},
                "body":    {"type": "STRING", "description": "Request body; JSON is sent as application/json."},
            },
            "required": ["url"],
        },
        "handler": http,
    },
]
