"""
The tool subset a stranger is allowed to drive.

core/primitives.py is built for one person on their own machine: run_python and
shell execute with that person's full authority, which is correct there and
indefensible here. This module is the public-facing replacement — same idea of a
few general tools, but every one of them chosen so that the worst a hostile
visitor can do is waste CPU.

What is deliberately absent, and why:

  shell            no. There is no version of this that is safe to expose.
  computer         meaningless — there is no screen or mouse in a container.
  files            no. The disk is shared between strangers. (Uploaded
                   documents are the exception, and are isolated per
                   session — see server/session_docs.py.)
  mcp / playwright a real browser per visitor is a resource and abuse problem.
  run_python       replaced by `calculate` below, which is not the same thing.

`calculate` is a RESTRICTED EVALUATOR, not a sandbox. It walks the AST and
refuses anything outside a small allowlist before executing. That stops the
obvious routes — imports, attribute tricks, exec, file and network access — but
the real boundary is the one underneath it: the container has no credentials
worth taking beyond the API key, and nothing it can reach. Treat the allowlist
as the first of two walls, never the only one.
"""
from __future__ import annotations

import ast
import io
import json
import re
import urllib.parse
import urllib.request
from contextlib import redirect_stdout

HTTP_TIMEOUT = 15
MAX_OUTPUT = 4000
UA = "Mozilla/5.0 (compatible; JarvisDemo/1.0)"


def _truncate(s: str, limit: int = MAX_OUTPUT) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n… [{len(s) - limit} more characters]"


# ═══════════════════════════════════════════════════════════════════════════
# calculate — arithmetic and small data work, nothing else
# ═══════════════════════════════════════════════════════════════════════════
_ALLOWED_IMPORTS = {
    "math", "statistics", "json", "re", "random", "datetime",
    "itertools", "collections", "decimal", "fractions", "string", "textwrap",
}

# Node types that can appear. Anything not listed is refused, so the default for
# a Python feature nobody thought about is "no" rather than "yes".
_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Expression, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.FormattedValue, ast.JoinedStr,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd, ast.MatMult,
    ast.UAdd, ast.USub, ast.Not, ast.Invert, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
    ast.In, ast.NotIn,
    ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Slice, ast.Subscript, ast.Starred,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.Call, ast.keyword, ast.Attribute,
    ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass,
    ast.Import, ast.ImportFrom, ast.alias,
    ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Lambda,
)

_BANNED_NAMES = {
    "eval", "exec", "compile", "open", "input", "__import__", "globals", "locals",
    "vars", "dir", "getattr", "setattr", "delattr", "breakpoint", "memoryview",
    "exit", "quit", "help",
}

_SAFE_BUILTINS = {
    k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
    for k in (
        "abs", "all", "any", "bin", "bool", "bytes", "chr", "complex", "dict",
        "divmod", "enumerate", "filter", "float", "format", "frozenset", "hex",
        "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max",
        "min", "next", "oct", "ord", "pow", "print", "range", "repr", "reversed",
        "round", "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
        "True", "False", "None", "ValueError", "TypeError", "KeyError",
        "IndexError", "ZeroDivisionError", "Exception",
    )
    if (k in __builtins__ if isinstance(__builtins__, dict) else hasattr(__builtins__, k))
}


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    """The import machinery needs __import__ even after the AST audit has passed.

    Removing it outright also disables `import math`, so it is replaced rather
    than deleted — and it re-checks the allowlist, because the audit only sees
    literal import statements in the submitted source.
    """
    root = name.split(".")[0]
    if root not in _ALLOWED_IMPORTS:
        raise ImportError(f"'{name}' is not available here.")
    return __import__(name, globals, locals, fromlist, level)


def _audit(code: str) -> str:
    """Return an error message, or '' if the code is acceptable."""
    try:
        tree = ast.parse(code, "<calc>", "exec")
    except SyntaxError as e:
        return f"Syntax error: {e.msg} on line {e.lineno}"

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return f"{type(node).__name__} is not permitted here."

        # Dunder access is the usual road out of a restricted namespace:
        # ().__class__.__bases__[0].__subclasses__() and friends.
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return "Attribute names beginning with __ are not permitted."
        if isinstance(node, ast.Name):
            if node.id in _BANNED_NAMES:
                return f"'{node.id}' is not available."
            if node.id.startswith("__"):
                return "Names beginning with __ are not permitted."

        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in _ALLOWED_IMPORTS:
                    return (f"Cannot import '{a.name}'. Available: "
                            f"{', '.join(sorted(_ALLOWED_IMPORTS))}.")
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in _ALLOWED_IMPORTS:
                return (f"Cannot import from '{node.module}'. Available: "
                        f"{', '.join(sorted(_ALLOWED_IMPORTS))}.")
    return ""


def calculate(code: str) -> str:
    code = (code or "").strip()
    if not code:
        return "No code given."
    if len(code) > 4000:
        return "That is longer than this demo will evaluate."

    problem = _audit(code)
    if problem:
        return f"Refused: {problem}"

    env: dict = {"__builtins__": dict(_SAFE_BUILTINS, __import__=_guarded_import)}
    out = io.StringIO()
    value = None
    try:
        with redirect_stdout(out):
            tree = ast.parse(code, "<calc>", "exec")
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                head = ast.Module(body=tree.body[:-1], type_ignores=[])
                tail = ast.Expression(body=tree.body[-1].value)
                exec(compile(head, "<calc>", "exec"), env)
                value = eval(compile(tail, "<calc>", "eval"), env)
            else:
                exec(compile(tree, "<calc>", "exec"), env)
                value = env.get("result")
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"

    parts = []
    if out.getvalue().strip():
        parts.append(out.getvalue().strip())
    if value is not None:
        parts.append(value if isinstance(value, str) else repr(value))
    return _truncate("\n".join(parts)) if parts else "Done. No output."


# ═══════════════════════════════════════════════════════════════════════════
# web_search / read_page
# ═══════════════════════════════════════════════════════════════════════════
def web_search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "No search query given."
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return "Search is unavailable on this server."
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=6))
    except Exception as e:
        return f"Search failed: {e}"
    if not hits:
        return "No results."
    lines = []
    for h in hits:
        lines.append(f"- {h.get('title', '').strip()}\n  {h.get('body', '').strip()}\n  {h.get('href', '')}")
    return _truncate("\n".join(lines))


# Only public web pages. Without this a visitor could ask the server to fetch
# cloud metadata endpoints or anything else reachable from inside the network —
# the classic SSRF pivot, and the one request this tool must never make.
_BLOCKED_HOSTS = re.compile(
    r"^(localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|"
    r"169\.254\.|\[::1\]|metadata|.*\.internal)", re.I)


def read_page(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "No URL given."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urllib.parse.urlparse(url).hostname or ""
    if _BLOCKED_HOSTS.match(host):
        return "That address is not reachable from this demo."
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype and "json" not in ctype:
                return f"That link is {ctype or 'not text'}, which this demo cannot read."
            raw = r.read(2_000_000).decode("utf-8", errors="replace")
    except Exception as e:
        return f"Could not load that page: {e}"
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", raw)
    return _truncate(re.sub(r"\s+", " ", text).strip(), 6000)


# ═══════════════════════════════════════════════════════════════════════════
DECLARATIONS = [
    {
        "name": "calculate",
        "description": (
            "Evaluate Python for arithmetic, dates, statistics, text and small data work, "
            "and return the output. Restricted to a safe standard-library subset — no "
            "files, no network, no system access. Use it whenever a question needs an "
            "exact number rather than an estimate."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "code": {"type": "STRING", "description": "Python to evaluate. Print, or end on an expression, to return a value."},
            },
            "required": ["code"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web and return titles, snippets and links. Use for current events, "
            "facts, prices — anything you would otherwise be guessing at."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"query": {"type": "STRING", "description": "What to search for."}},
            "required": ["query"],
        },
    },
    {
        "name": "read_page",
        "description": (
            "Fetch a public web page and return its text, so you can answer from what it "
            "actually says. Follow a web_search result with this when the snippet is not enough."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {"url": {"type": "STRING", "description": "The page URL."}},
            "required": ["url"],
        },
    },
]

# Retrieval over a document the visitor uploaded. Declared separately because
# it is the one tool that needs session state — the others are pure functions,
# this one has to reach the store belonging to THIS visitor and no other.
SEARCH_DOCUMENT = {
    "name": "search_document",
    "description": (
        "Search a document the person has uploaded in this session and return the "
        "passages that answer a question. Use this for EVERY question about an "
        "uploaded file — a figure, a date, a name, a clause, a total, or a summary. "
        "You have not read the document until you call this: the list of loaded "
        "files tells you what exists, never what is in it. Answer only from the "
        "passages returned, say where they came from, and if they do not cover the "
        "question, say the document does not address it rather than filling the gap."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING", "description": "What to find out from the document."},
            "document": {"type": "STRING", "description": "Which file, if several are loaded. Optional."},
        },
        "required": ["question"],
    },
}

HANDLERS = {
    "calculate": lambda a: calculate(a.get("code", "")),
    "web_search": lambda a: web_search(a.get("query", "")),
    "read_page": lambda a: read_page(a.get("url", "")),
}


def declarations_for(docs=None) -> list[dict]:
    """The tool list for one session.

    search_document is ALWAYS included, even with nothing uploaded yet. The Live
    API fixes its tool list when the session connects, and documents arrive
    afterwards — declaring it conditionally would mean the one case that matters,
    someone attaching a file mid-conversation, is the case where the tool does
    not exist. The handler explains itself when the store is empty, which costs
    one wasted call at worst.
    """
    return list(DECLARATIONS) + [SEARCH_DOCUMENT]


def run(name: str, args: dict, docs=None) -> str:
    args = args or {}
    if name == "search_document":
        if docs is None:
            return "No document has been uploaded in this session."
        return docs.search(args.get("question", ""), args.get("document", ""))

    fn = HANDLERS.get(name)
    if fn is None:
        return f"'{name}' is not available in this demo."
    try:
        return fn(args)
    except Exception as e:
        return f"{name} failed: {e}"
