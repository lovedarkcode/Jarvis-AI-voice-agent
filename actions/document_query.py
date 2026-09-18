"""
actions/document_query.py — answer questions from an uploaded document.

This is the retrieval half of the document pipeline. Extraction and indexing
live in core/document_parser.py and core/document_store.py; this file is the
surface the model calls.

WHY IT IS A SEPARATE TOOL FROM file_processor
    file_processor already reads PDFs and Word files, so a second document tool
    looks redundant until you look at what each one returns. file_processor
    performs an OPERATION and returns its outcome — it converts a PDF to Word,
    extracts text to a file, or makes its own one-shot gemini-flash call and
    hands back that model's prose. The live session never sees the document; it
    sees a sentence about the document, written by a different model with no
    knowledge of the conversation.

    That is fine for "convert this to Word" and wrong for "what does clause 4
    say?". A question needs the source text in front of the model that is
    talking to the user, so it can be asked a follow-up, argued with, and
    answered in the user's own language and register. This tool returns
    PASSAGES, not conclusions.

    The split, stated once so the routing rule in core/prompt.txt has something
    to point at:
        file_processor  — do something TO the file
        document_query  — answer something FROM the file

RETURN CONTRACT
    A block of document text wrapped in instructions, handed straight back to
    the live session as the tool result. Never a refusal, never a traceback: if
    nothing can be read, it returns a sentence the assistant can say out loud.
"""
from __future__ import annotations

from pathlib import Path

from core import document_parser
from core.document_store import store


def _ingest_on_demand(path_str: str, player=None) -> tuple[bool, str]:
    """Parse a document the store has not seen yet.

    The normal path is that the HUD ingests on drop, so by the time a question
    arrives the document is indexed. This exists for the cases where that did
    not happen: a path spoken aloud, a file put in place by another tool, or an
    upload from the phone dashboard. Falling back to reading it here costs the
    one-off parse time and keeps the user out of a dead end where the file
    plainly exists and the assistant insists it has nothing loaded.
    """
    p = Path(path_str).expanduser()
    if not p.exists():
        return False, f"I cannot find a file at {path_str}."
    if not document_parser.is_supported(p):
        return False, (f"'{p.name}' is not a readable document. I can read PDF, "
                       f"Word, Excel, CSV, JSON and plain text files; for other "
                       f"file types use the file processor instead.")
    if player:
        try:
            player.write_log(f"DOC: reading {p.name}…")
        except Exception:
            pass
    return store.ingest(p)


def document_query(parameters: dict, player=None) -> str:
    action = (parameters.get("action") or "query").strip().lower()
    scope  = (parameters.get("scope") or "targeted").strip().lower()
    name   = (parameters.get("document") or "").strip()
    query  = (parameters.get("question") or "").strip()

    # ── list ────────────────────────────────────────────────────────────────
    if action == "list":
        docs = store.all_documents()
        if not docs:
            return ("No documents are loaded. The user can drop a PDF, Word or "
                    "Excel file onto the interface, or send one from the phone "
                    "dashboard.")
        active = store.active()
        lines  = []
        for d in docs:
            mark = " (active)" if active and d.doc_id == active.doc_id else ""
            lines.append(f"{d.name}{mark} — {d.kind.upper()}, {d.segments} "
                         f"sections, {d.char_count:,} characters")
        return "Documents loaded:\n" + "\n".join(lines)

    # ── resolve which document ──────────────────────────────────────────────
    doc = store.resolve(name)

    if doc is None and name:
        # A name that is not loaded might still be a path the user just named.
        ok, msg = _ingest_on_demand(name, player)
        if not ok:
            loaded = [d.name for d in store.all_documents()]
            if loaded:
                return (f"{msg} Documents I do have loaded: {', '.join(loaded)}. "
                        f"Ask the user which one they mean.")
            return msg
        doc = store.active()

    if doc is None:
        return ("No document is loaded, so there is nothing to read. Ask the user "
                "to drop the file onto the interface (or send it from the phone "
                "dashboard) and then repeat their question.")

    # ── build the context payload ───────────────────────────────────────────
    if player:
        try:
            label = "overview" if scope == "overview" else (query[:40] or "document")
            player.write_log(f"DOC: {doc.name} → {label}")
        except Exception:
            pass

    if scope == "overview" or not query:
        context = store.build_overview(doc)
    else:
        context = store.build_context(query, doc)

    # A semantic index that is still building is worth one line: it explains why
    # a repeat of the same question a minute later can find something this one
    # missed, which otherwise looks like the assistant being inconsistent.
    if doc.embed_state == "pending":
        context += ("\n\n[Note: the semantic index for this document is still "
                    "building, so this search was keyword-only. Do not mention "
                    "this unless the user asks why something was not found.]")

    print(f"[DocumentQuery] {doc.name} | scope={scope} | {len(context):,} chars")
    return context


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "document_query",
    "description": (
        "Reads an uploaded document (PDF, Word, Excel, CSV, JSON or text) and "
        "returns the passages that answer a question about it. "
        "Call this for ANY question about the content of a file the user has "
        "uploaded or referred to: what it says, what it means, a figure, a date, "
        "a name, a clause, a total, a comparison between parts of it, or a "
        "summary of it. "
        "You have NOT read any uploaded document until you call this — the "
        "system prompt only lists which documents exist, never their contents. "
        "Never answer a question about a document from memory or by guessing, "
        "and never say you cannot read files. "
        "Use file_processor INSTEAD when the user wants an operation performed on "
        "the file rather than an answer from it (convert it, resize it, extract it "
        "to a new file, run it). "
        "This tool returns raw document text for you to answer from; read it and "
        "reply in the user's own language."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {
                "type": "STRING",
                "description": (
                    "What the user wants to know, in their words, translated to "
                    "English. This is used to search the document, so keep the "
                    "specific nouns and numbers they used — they are what match "
                    "the text. Leave empty only when scope is 'overview'."
                ),
            },
            "scope": {
                "type": "STRING",
                "description": (
                    "'targeted' (default) — a specific question; returns the "
                    "matching passages. "
                    "'overview' — the user wants a summary, the gist, or asked "
                    "what the document is about; returns a sample spread across "
                    "the whole document instead of searching it."
                ),
            },
            "document": {
                "type": "STRING",
                "description": (
                    "Which document, by filename, when more than one is loaded "
                    "and the user named a specific one. Leave empty to use the "
                    "most recently uploaded document, which is almost always "
                    "what 'this file' means."
                ),
            },
            "action": {
                "type": "STRING",
                "description": (
                    "'query' (default) — read the document. "
                    "'list' — name the documents currently loaded, for when the "
                    "user asks what files you have."
                ),
            },
        },
        "required": [],
    },
    "handler": document_query,
}
