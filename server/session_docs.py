"""
Per-visitor document RAG for the public demo.

core/document_store.py already implements the retrieval properly — parse, chunk
at ~1,100 characters, BM25 lexical index, optional embedding pass, and a
character budget on what comes back. None of that is reimplemented here. What
this module adds is the two things that engine deliberately does not have,
because the desktop build never needed them:

ISOLATION
    `core.document_store.store` is a module-level singleton. That is right for
    one person on their own machine and wrong the moment strangers share a
    process: visitor A uploads a CV and visitor B can ask about it. The class
    itself is instance-scoped — ingest() touches nothing global — so each
    session gets its own DocumentStore and drops it on disconnect.

A CEILING ON MEMORY
    Everything lives in RAM: chunk text, the lexical index, and ~1.1 MB of
    float32 vectors per 340 KB document when embeddings are on. The demo runs
    on a 512 MB machine, so uploads from anonymous visitors need a hard bound
    rather than a hopeful one. The caps below are per session, and sessions are
    evicted the moment their socket closes.

Embeddings are off by default here. They cost an API call per batch of chunks
against the owner's key, and BM25 answers "what does the document say about X"
well enough for a demo. Set DEMO_DOC_EMBEDDINGS=true to turn them on.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

MAX_UPLOAD_BYTES = int(os.environ.get("DEMO_MAX_UPLOAD_MB", "5")) * 1024 * 1024
MAX_DOCS_PER_SESSION = int(os.environ.get("DEMO_MAX_DOCS", "3"))
MAX_CHARS_PER_SESSION = int(os.environ.get("DEMO_MAX_DOC_CHARS", "400000"))

# Parsers are chosen by extension, so an extension the parser cannot read is
# rejected at the door rather than after the bytes have been written to disk.
ALLOWED_SUFFIXES = {
    ".pdf", ".docx", ".txt", ".md", ".csv", ".json", ".xlsx", ".xls",
    ".log", ".rtf", ".html", ".htm",
}


class SessionDocs:
    """One visitor's documents. Created with their socket, destroyed with it."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.created = time.time()
        self._lock = threading.Lock()
        self._chars = 0
        self._names: list[str] = []

        # Each session gets a private temp directory. The parser reads from a
        # path, and sharing one directory between visitors would let a crafted
        # filename collide with somebody else's upload.
        self._dir = Path(tempfile.mkdtemp(prefix=f"jarvisdemo-{session_id[:8]}-"))

        from core.document_store import DocumentStore
        self.store = DocumentStore()
        self.store.set_logger(lambda m: print(f"[docs:{session_id[:8]}] {m}"))

    # ── ingest ───────────────────────────────────────────────────────────────
    def accept(self, filename: str, data: bytes) -> tuple[bool, str]:
        """Validate and ingest an upload. Returns (ok, message for the visitor)."""
        name = Path(filename or "upload").name
        suffix = Path(name).suffix.lower()

        if suffix not in ALLOWED_SUFFIXES:
            return False, (f"I can read PDF, Word, Excel, CSV, JSON and text files. "
                           f"'{suffix or 'that'}' is not one of them.")
        if len(data) > MAX_UPLOAD_BYTES:
            return False, (f"That file is {len(data) / 1024 / 1024:.1f} MB. "
                           f"The demo accepts up to {MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
        if not data:
            return False, "That file is empty."

        with self._lock:
            if len(self._names) >= MAX_DOCS_PER_SESSION:
                return False, (f"This demo holds {MAX_DOCS_PER_SESSION} documents per "
                               f"session. Refresh to start over.")
            if self._chars >= MAX_CHARS_PER_SESSION:
                return False, "This session has reached its document limit."

        # Written to disk because the parser works on paths, not buffers. It
        # lives only as long as the session.
        dest = self._dir / name
        try:
            dest.write_bytes(data)
        except Exception as e:
            return False, f"Could not save that file: {e}"

        try:
            ok, detail = self.store.ingest(dest)
        except Exception as e:
            dest.unlink(missing_ok=True)
            return False, f"Could not read that file: {e}"

        if not ok:
            dest.unlink(missing_ok=True)
            return False, detail

        doc = self.store.active()
        if doc is not None:
            with self._lock:
                self._chars += doc.char_count
                self._names.append(doc.name)
            # The budget is checked again after parsing, because a 2 MB PDF can
            # hold far more text than a 2 MB spreadsheet and only the parser
            # knows which this was.
            if self._chars > MAX_CHARS_PER_SESSION:
                self.store.remove(doc.doc_id)
                dest.unlink(missing_ok=True)
                with self._lock:
                    self._chars -= doc.char_count
                    self._names.pop()
                return False, ("That document is larger than this demo can hold. "
                               "Try a shorter one.")

        return True, detail

    # ── query ────────────────────────────────────────────────────────────────
    def search(self, question: str, name: str = "") -> str:
        """Retrieve the passages that answer a question."""
        if not self.store.has_documents():
            return ("No document has been uploaded in this session. Ask the person "
                    "to attach one first.")
        doc = self.store.resolve(name or "")
        if doc is None:
            return f"No document called '{name}' here. Loaded: {', '.join(self._names)}"
        if not (question or "").strip():
            return self.store.describe(doc.doc_id)
        return self.store.build_context(question, doc)

    def manifest(self) -> str:
        """What to tell the model about what is loaded — names only, never
        contents, so the prompt stays small and the model still knows to look."""
        if not self.store.has_documents():
            return ""
        return self.store.manifest_for_prompt()

    def names(self) -> list[str]:
        with self._lock:
            return list(self._names)

    # ── teardown ─────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Drop everything. Called when the socket closes — this is what keeps
        one visitor's upload from outliving their visit, in memory or on disk."""
        try:
            self.store.clear()
        except Exception:
            pass
        shutil.rmtree(self._dir, ignore_errors=True)


class SessionRegistry:
    """Live sessions, so an HTTP upload can find the store belonging to a
    WebSocket that is already open."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionDocs] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str) -> SessionDocs:
        s = SessionDocs(session_id)
        with self._lock:
            self._sessions[session_id] = s
        return s

    def get(self, session_id: str) -> SessionDocs | None:
        with self._lock:
            return self._sessions.get(session_id)

    def drop(self, session_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(session_id, None)
        if s:
            s.close()

    def reap(self, max_age: float = 1800) -> None:
        """Safety net for sessions whose socket died without a clean close —
        otherwise a dropped connection leaks a temp directory for the life of
        the process."""
        cutoff = time.time() - max_age
        with self._lock:
            stale = [k for k, v in self._sessions.items() if v.created < cutoff]
            dead = [self._sessions.pop(k) for k in stale]
        for s in dead:
            s.close()


registry = SessionRegistry()
