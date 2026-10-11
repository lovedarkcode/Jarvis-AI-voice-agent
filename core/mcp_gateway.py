"""
MCP gateway — authenticated reach into services JARVIS has no credentials for.

Why this exists, given run_python can already do anything locally: it cannot log
into your Gmail. The value of MCP here is not capability, it is credentials and
session state — an OAuth token someone else refreshes, a browser that is already
logged in. Nothing in this module makes JARVIS more capable on its own machine.

WHY ONE TOOL AND NOT MANY
A Gmail server exposes ~10 tools and a GitHub server ~30. Registering each as its
own function declaration would put the tool surface back above 40, which is the
exact sprawl core/primitives.py exists to prevent — only sourced from a server
instead of a file. So every server is reached through a single `mcp` primitive,
and what each one offers is advertised to the model through a prompt manifest
(see catalog_for_prompt) rather than through the tool list. That is the same
trick core/document_store.py uses for uploaded documents: tell the model what
exists, not what it contains.

TRANSPORT
Raw synchronous JSON-RPC over the server's stdin/stdout, rather than the asyncio
MCP SDK. Primitives are called synchronously inside run_in_executor, and a
Playwright session must survive across calls — a per-call event loop would mean a
per-call browser, losing the logged-in session that is the whole point. One
long-lived subprocess per server, one lock per server, is a far better fit than
bridging asyncio across thread boundaries.

TRUST
A server's tool descriptions are third-party text that lands in the model's
context, on a machine where run_python is unsandboxed. Prefer official
@modelcontextprotocol/* servers and your own.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"   # broadest server support
START_TIMEOUT = 60                # npx may download a package on first run
CALL_TIMEOUT = 120                # a browser step can legitimately take a while
GMAIL_READ_TOOLS = frozenset({'search_emails', 'read_email'})

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str) -> str:
    """Resolve ${VAR} against the environment so tokens live in .env, not in a
    config file that could be committed."""
    from core.env_config import get
    return _ENV_RE.sub(lambda m: get(m.group(1), ""), value)


class MCPServer:
    """One MCP server subprocess and the JSON-RPC conversation with it."""

    def __init__(self, name: str, spec: dict, logger=print):
        self.name = name
        self.command = spec.get("command", "")
        self.args = [_expand(str(a)) for a in spec.get("args", [])]
        self.env = {k: _expand(str(v)) for k, v in (spec.get("env") or {}).items()}
        self.enabled = spec.get("enabled", True)
        self.description = spec.get("description", "")
        self.is_gmail = name == 'gmail' or any('@gongrzhe/server-gmail-autoauth-mcp' in a for a in self.args)
        self.local_only = spec.get('local_only', False) or self.is_gmail
        self.allowed_tools = GMAIL_READ_TOOLS if self.is_gmail else None
        self.call_timeout = 15 if self.is_gmail else CALL_TIMEOUT

        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.error: str = ""
        self.ready = False
        self._id = 0
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._lines = queue.Queue()
        self._stop_requested = False
        self._uses_launcher = False
        self._log = logger

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> bool:
        if not self._start_lock.acquire(blocking=False):
            return False
        try:
            return self._start()
        finally:
            self._start_lock.release()

    def _start(self) -> bool:
        if self._stop_requested:
            return False
        if self.ready:
            return True
        if self.local_only and os.getenv('VERCEL'):
            self.error = 'available only in local runs'
            return False
        if not self.enabled:
            self.error = "disabled in config"
            return False

        if self.is_gmail:
            credentials = Path(self.env.get('GMAIL_CREDENTIALS_PATH') or os.getenv('GMAIL_CREDENTIALS_PATH') or
                               Path.home() / '.gmail-mcp' / 'credentials.json')
            if not credentials.is_file():
                self.error = 'Gmail is not authenticated. Run the Gmail auth command and restart Jarvis.'
                return False
        if not self.command:
            self.error = "no command in config"
            return False

        env = os.environ.copy()
        env.update(self.env)
        # npx and friends are .cmd shims on Windows, which CreateProcess will not
        # run without a shell resolution step; shutil.which finds the real target.
        import shutil as _sh
        exe = _sh.which(self.command) or self.command
        self._uses_launcher = Path(exe).suffix.lower() in ('.cmd', '.bat')

        try:
            self.proc = subprocess.Popen(
                [exe, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.error = f"could not start: {e}"
            self._log(f"{self.name}: {self.error}")
            return False

        if self._stop_requested:
            self.stop()
            return False

        # Drain stderr continuously. A server that logs heavily will otherwise
        # fill the pipe buffer and hang forever on its next write — a deadlock
        # that looks exactly like a slow server.
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        self._lines = queue.Queue()
        threading.Thread(target=self._read_stdout, args=(self.proc, self._lines), daemon=True).start()

        try:
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "jarvis", "version": "1.0"},
            }, timeout=START_TIMEOUT)
            self._notify("notifications/initialized")
            result = self._request("tools/list", {}, timeout=START_TIMEOUT)
            self.tools = result.get("tools", []) or []
            if self.allowed_tools is not None:
                self.tools = [t for t in self.tools if t.get('name') in self.allowed_tools]
            if self._stop_requested:
                self.stop()
                return False
            self.error = ''
            self.ready = True
            self._log(f"{self.name}: {len(self.tools)} tools")
            return True
        except Exception as e:
            self.error = str(e)
            self._log(f"{self.name}: handshake failed — {e}")
            self.stop()
            return False

    def _drain_stderr(self, proc) -> None:
        try:
            for line in proc.stderr:
                if line.strip() and not self.is_gmail:
                    print(f"[MCP:{self.name}] {line.rstrip()}")
        except Exception:
            pass
        finally:
            proc.stderr.close()

    @staticmethod
    def _read_stdout(proc, lines):
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)
            proc.stdout.close()

    def stop(self) -> None:
        self._stop_requested = True
        proc, self.proc = self.proc, None
        self.ready = False
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            # npx is a launcher; terminate its child server as well on Windows.
            if os.name == 'nt' and self._uses_launcher and proc.poll() is None:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=5)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    # ── JSON-RPC ─────────────────────────────────────────────────────────────
    def _send(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("server is not running")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict, timeout: int = CALL_TIMEOUT) -> dict:
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError('server is busy; please try again')
        try:
            self._id += 1
            req_id = self._id
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

            while time.monotonic() < deadline:
                if self.proc is None or self.proc.stdout is None:
                    raise RuntimeError("server went away")
                try:
                    line = self._lines.get(timeout=max(0.001, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if not line:
                    raise RuntimeError("server closed its output stream")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue   # servers sometimes print banners on stdout
                # Skip notifications and responses to earlier, abandoned requests.
                if msg.get("id") != req_id:
                    continue
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(err.get("message") or json.dumps(err))
                return msg.get("result") or {}
            raise TimeoutError(f"no reply within {timeout}s")
        finally:
            self._lock.release()

    # ── calling ──────────────────────────────────────────────────────────────
    def call(self, tool: str, arguments: dict) -> str:
        if self.allowed_tools is not None and tool not in self.allowed_tools:
            return 'Gmail is read-only. That tool is not permitted.'
        if not self.ready:
            return f"MCP server '{self.name}' unavailable: {self.error or ('still starting' if self.enabled else 'disabled in config')}"
        if self.is_gmail and tool == 'search_emails':
            arguments = dict(arguments)
            try:
                arguments['maxResults'] = min(5, max(1, int(arguments.get('maxResults', 5))))
            except (ValueError, TypeError):
                return 'Invalid search limit.'
        try:
            result = self._request("tools/call", {"name": tool, "arguments": arguments}, timeout=self.call_timeout)
        except Exception as e:
            if self.is_gmail:
                return 'ERROR: Gmail did not respond. Check authentication or your connection and retry.'
            return f"MCP {self.name}.{tool} failed: {e}"
        if self.is_gmail:
            if result.get('isError'):
                return 'ERROR: Gmail could not complete the request. Check authentication and Gmail API access.'
            if tool == 'search_emails' and not any(b.get('text', '').strip() for b in result.get('content', [])):
                return 'No matching emails.'
        return _render_result(result)


def _render_result(result: dict) -> str:
    """Flatten an MCP tool result into text the live model can hear.

    Content blocks may be text, images or embedded resources; only text is
    meaningful in a voice session, so non-text blocks are named rather than
    inlined — a base64 image would blow the context for no benefit.
    """
    blocks = result.get("content") or []
    parts: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "image":
            parts.append("[image returned]")
        elif kind == "resource":
            res = block.get("resource") or {}
            parts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
    body = "\n".join(p for p in parts if p).strip()
    if result.get("isError"):
        return f"ERROR: {body or 'the tool reported a failure'}"
    if not body and result.get("structuredContent") is not None:
        body = json.dumps(result["structuredContent"])[:3000]
    return body or "Done."


class MCPGateway:
    def __init__(self, config_path: Path, logger=print):
        self.config_path = config_path
        self.servers: dict[str, MCPServer] = {}
        self._log = logger
        self._started = threading.Event()

    def load(self) -> None:
        if not self.config_path.exists():
            self._log(f"no config at {self.config_path} — MCP disabled")
            self._started.set()
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            self._log(f"config unreadable: {e}")
            self._started.set()
            return
        for name, spec in (raw.get("mcpServers") or {}).items():
            self.servers[name] = MCPServer(name, spec, logger=self._log)

    def start_all(self, background: bool = True) -> None:
        """Bring servers up. In the background by default: npx may download a
        package on first run, and JARVIS must never fail to boot because a
        third-party server is slow or broken."""
        self.load()

        def _run() -> None:
            workers = []
            def start_server(server):
                try:
                    server.start()
                except Exception as e:
                    server.error = str(e)
                    self._log(f"{server.name}: {e}")
            for server in self.servers.values():
                if server.enabled:
                    worker = threading.Thread(target=start_server, args=(server,), daemon=True)
                    worker.start()
                    workers.append(worker)
            for worker in workers:
                worker.join()
            self._started.set()

        if background:
            threading.Thread(target=_run, daemon=True).start()
        else:
            _run()

    def wait_ready(self, timeout: float = 30) -> None:
        self._started.wait(timeout)

    # ── what the model is told ───────────────────────────────────────────────
    def catalog_for_prompt(self) -> str:
        """The manifest injected at connect time.

        Names every reachable tool but not its full schema — enough for the model
        to know a capability exists and to call it, without paying the context
        cost of N function declarations. The model gets the argument detail from
        mcp(action='describe') when it actually needs it.
        """
        live = list(self.servers.values())
        if not live:
            return ""
        lines = ["[MCP SERVERS — reach these with the `mcp` tool]"]
        for server in live:
            label = f" — {server.description}" if server.description else ""
            lines.append(f"- {server.name}{label}")
            if server.is_gmail:
                lines.append('  Read-only: only search_emails and read_email are permitted; no marking or other changes.')
            if not server.ready:
                lines.append(f"  unavailable: {server.error or ('still starting' if server.enabled else 'disabled in config')}")
                continue
            names = ", ".join(t.get("name", "?") for t in server.tools)
            lines.append(f"  tools: {names}")
        lines.append(
            "Call mcp(server=..., tool=..., arguments={...}). "
            "If unsure of a tool's arguments, call mcp(action='describe', "
            "server=..., tool=...) first."
        )
        return "\n".join(lines) + "\n"

    def describe(self, server_name: str = "", tool_name: str = "") -> str:
        if not server_name:
            return self.catalog_for_prompt() or "No MCP servers are running."
        server = self.servers.get(server_name)
        if server is None:
            return f"No such MCP server: {server_name}. Known: {', '.join(self.servers) or 'none'}"
        if not server.ready:
            return f"MCP server '{server_name}' is not running: {server.error or 'still starting'}"
        tools = server.tools
        if tool_name:
            tools = [t for t in tools if t.get("name") == tool_name]
            if not tools:
                return f"'{server_name}' has no tool '{tool_name}'."
        out = []
        for t in tools:
            out.append(f"{t.get('name')}: {t.get('description', '').strip()}")
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            if props:
                required = set(schema.get("required") or [])
                for key, meta in props.items():
                    flag = "required" if key in required else "optional"
                    desc = (meta.get("description") or "").strip()
                    out.append(f"  - {key} ({meta.get('type', 'any')}, {flag}) {desc}")
        return "\n".join(out)

    def call(self, server_name: str, tool: str, arguments: dict) -> str:
        server = self.servers.get(server_name)
        if server is None:
            return f"No such MCP server: {server_name}. Known: {', '.join(self.servers) or 'none'}"
        return server.call(tool, arguments)

    def status(self) -> str:
        if not self.servers:
            return "No MCP servers configured."
        rows = []
        for s in self.servers.values():
            state = "ready" if s.ready else ("disabled" if not s.enabled else f"down ({s.error or 'starting'})")
            rows.append(f"- {s.name}: {state}, {len(s.tools)} tools")
        return "\n".join(rows)

    def stop_all(self) -> None:
        for s in self.servers.values():
            s.stop()


# ── module-level instance, mirroring core.document_store's `store` ───────────
gateway = MCPGateway(Path(__file__).resolve().parent.parent / "mcp_servers.json")


def mcp(parameters: dict, player=None, **_) -> str:
    action = (parameters.get("action") or "").lower().strip()
    server = (parameters.get("server") or "").strip()
    tool = (parameters.get("tool") or "").strip()

    if action == "list" or (not server and not tool and action != "describe"):
        return gateway.describe() or gateway.status()
    if action == "describe":
        return gateway.describe(server, tool)
    if action == "status":
        return gateway.status()

    if not server or not tool:
        return "Both server and tool are required. Use action='list' to see what is available."

    arguments = parameters.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            return "arguments must be a JSON object."

    _log(f"{server}.{tool} {json.dumps(arguments)[:120]}", player)
    return gateway.call(server, tool, arguments)


def _log(message: str, player=None) -> None:
    print(f"[MCP] {message}")
    if player:
        try:
            player.write_log(f"SYS: mcp — {message}")
        except Exception:
            pass


TOOL = {
    "name": "mcp",
    "description": (
        "Reach an external service through its MCP server — things needing credentials or a "
        "logged-in session that you cannot do with run_python: email, calendars, cloud drives, "
        "repositories, and real browser automation with a persistent signed-in browser. "
        "Call with server, tool and arguments. Use action='list' to see which servers and tools "
        "are available, or action='describe' with a server and tool to see that tool's arguments "
        "before calling it. Prefer the local primitives for anything on this machine."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":    {"type": "STRING", "description": "Omit to call a tool. 'list' to see everything available, 'describe' for one tool's arguments, 'status' for server health."},
            "server":    {"type": "STRING", "description": "Which MCP server, e.g. 'playwright'."},
            "tool":      {"type": "STRING", "description": "The tool on that server to call."},
            "arguments": {"type": "STRING", "description": "Arguments for the tool, as a JSON object."},
        },
        "required": [],
    },
    "handler": mcp,
}
