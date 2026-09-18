"""
core/document_store.py — session state and retrieval for uploaded documents.

WHAT PROBLEM THIS SOLVES
    A live voice session has one system prompt, fixed at connect time, and a
    context window that a sliding window is already compressing. Dropping a
    40-page PDF into either of those is the obvious move and the wrong one:

      • Into the system prompt — LiveConnectConfig is built once per connect
        (main.py:_build_config), so the document only takes effect after a
        session rebuild, and then rides in EVERY subsequent reconnect. One
        dropped packet re-sends the whole PDF.
      • Into the conversation — it survives exactly until the sliding window
        compresses it away, at which point the assistant confidently answers
        from a summary of a summary.

    So the document does not go into the prompt at all. What goes in is a
    manifest — a few hundred characters naming the file and what is in it — and
    the passages themselves are fetched per question by files(action='query').

    This is the same shape the memory system already uses: a small core in the
    prompt (format_memory_for_prompt) plus an on-demand lookup (recall_memory).
    Copying an architecture the codebase already proves out is worth more than a
    marginally better one nobody else in the file tree recognises.

THE TWO PATHS
    Small document (under FULL_DOC_CHARS): retrieval is skipped and the whole
    text is returned. Chunking a two-page letter to find the relevant third of
    it is strictly worse than sending the letter — it costs a ranking pass and
    risks cutting the one sentence that mattered.

    Large document: chunk, rank, and return the best passages inside a character
    budget. This is the path that makes a 200-page manual answerable at all.

RANKING: LEXICAL ALWAYS, VECTORS WHEN THEY EARN IT
    BM25 runs on every query. It is pure Python over a few hundred short
    strings, needs no network, no model and no extra dependency, and completes
    in well under a millisecond — the same reasoning memory_manager._score
    documents for recall.

    Embeddings are strictly an addition on top, computed in a background thread
    at ingest and only for documents big enough for lexical matching to struggle
    with vocabulary mismatch ("revenue" in the question, "turnover" in the PDF).
    They are fused with BM25, never trusted alone. Three consequences that
    matter more than the ranking quality:

      • The first question after an upload never waits for an embedding pass.
        If the vectors are not ready, the query is answered lexically.
      • An API failure, an expired key or an offline machine degrades to BM25
        silently rather than breaking document Q&A.
      • The feature costs nothing for anyone who never uploads a large file.

THREAD SAFETY
    Ingestion runs on an executor thread (main.py hands it off), retrieval runs
    on another, and the Qt thread reads the manifest for the HUD. Every mutation
    of the document table is behind one lock. The lock is never held across a
    network call — the embedding worker computes into a local array and takes
    the lock only to publish it.
"""
from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core import document_parser
from core.document_parser import ParsedDocument

# ── Budgets ──────────────────────────────────────────────────────────────────
# Under this many characters a document is never chunked — the whole thing is
# returned on every query. ~12k chars is roughly 3k tokens: comfortably affordable
# per turn, and it covers the large majority of real uploads (letters, invoices,
# CVs, single-topic reports).
FULL_DOC_CHARS = 12_000

# Ceiling on what one document_query call may return for a large document.
# ~24k chars ≈ 6k tokens. Large enough to carry a dozen passages with their
# citations, small enough that several document questions in a row do not fill
# the window the sliding compressor then has to chew through.
CONTEXT_CHAR_BUDGET = 24_000

# Chunk geometry. 1,100 characters is about a screenful of prose — big enough to
# hold a complete argument, small enough that a hit is specific. The overlap
# exists so a sentence spanning a boundary is retrievable from either side.
CHUNK_CHARS   = 1_100
CHUNK_OVERLAP = 150

# Documents at or above this size get embeddings computed in the background.
# Below it, BM25 alone is fine and an embedding pass would be latency and spend
# with nothing to show for it.
EMBED_MIN_CHARS = 30_000

# Retrieval shape.
TOP_K              = 8      # chunks scored in before neighbour expansion

# Two different thresholds, because they answer two different questions.
#
# MIN_RELEVANCE is measured on the ABSOLUTE BM25 score, as a fraction of the
# best score this query could possibly achieve. It decides whether the document
# covers the subject at all. It cannot be measured on normalised scores: dividing
# by the maximum makes the best chunk score 1.0 by construction, so a question
# the document has no answer to looks exactly like one it answers perfectly. That
# is the difference between "the manual does not cover catering" and twenty
# pages of unrelated text presented as if they were relevant.
#
# KEEP_RATIO is measured on the normalised scores and decides how many of the
# survivors are worth sending once relevance is established.
#
# 0.28 is not a guess. Measured over sixteen queries against the test corpus,
# questions the documents genuinely answer scored 0.40 to 1.05, and questions
# about subjects entirely absent from them scored 0.00 to 0.17. The threshold
# sits in the middle of that gap, leaving roughly equal margin on both sides.
# Re-measure it before moving it: too high and the assistant starts refusing
# questions its documents do answer, which is the more damaging of the two
# failures — a wrong "I can't find that" is indistinguishable to the user from
# the feature being broken.
MIN_RELEVANCE      = 0.28   # of the query's total information content
KEEP_RATIO         = 0.25   # of the best chunk's score
BM25_WEIGHT        = 0.55   # lexical half of the fused score
VECTOR_WEIGHT      = 0.45   # semantic half, only when vectors are ready
QUERY_EMBED_TIMEOUT = 2.5   # seconds; past this the query is answered lexically

# Gemini embedding model. 768 dimensions rather than the default 3072: on
# retrieval over a few hundred chunks the quality difference is not measurable,
# and the smaller vectors cut both the response size and the dot-product cost.
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS  = 768
EMBED_BATCH = 32

# BM25 constants, standard Okapi values.
_BM25_K1 = 1.5
_BM25_B  = 0.75

# Deliberately no stopword list. This assistant is used in many languages and an
# English stopword list would strip nothing from a Turkish document while
# silently damaging an English one. Inverse document frequency already
# discounts terms that appear everywhere, in whatever language they appear in.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _config_path() -> Path:
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent)
    return base / "config" / "api_keys.json"


def _config() -> dict:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def embeddings_enabled() -> bool:
    """Semantic re-ranking is on by default.

    Turn it off with DOCUMENT_EMBEDDINGS=false in .env — for an offline machine,
    a metered connection, or someone who does not want document text leaving the
    device at all. .env is checked first because that is where a new user is now
    told to configure this app; the api_keys.json key is still honoured so an
    existing install that set it there does not silently change behaviour.
    """
    from core.env_config import get
    raw = get("DOCUMENT_EMBEDDINGS")
    if raw:
        return raw.strip().lower() not in ("0", "false", "no", "off")
    return bool(_config().get("document_embeddings", True))


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    doc_id:  str
    index:   int
    text:    str
    source:  str                 # citation label inherited from the segment
    tokens:  list[str] = field(default_factory=list)
    length:  int = 0             # token count, cached for BM25


@dataclass
class LoadedDocument:
    doc_id:      str
    name:        str
    path:        str
    kind:        str
    extractor:   str
    char_count:  int
    segments:    int
    full_text:   str
    parts:       list[tuple[str, str]]         # (citation label, text) in reading order
    chunks:      list[Chunk]
    outline:     list[str]                    # segment labels, for the manifest
    preview:     str                          # opening lines, for the manifest
    warnings:    list[str] = field(default_factory=list)
    ingested_at: float = field(default_factory=time.time)

    # Lexical index, built once at ingest.
    _df:        dict[str, int] = field(default_factory=dict)
    _avg_len:   float = 0.0

    # Vector index, published by the background worker when (and if) it finishes.
    _vectors:   object | None = None           # numpy.ndarray | None
    embed_state: str = "off"                   # off | pending | ready | failed
    embed_note:  str = ""

    @property
    def is_small(self) -> bool:
        return self.char_count <= FULL_DOC_CHARS


# ── Chunking ─────────────────────────────────────────────────────────────────

def _split_segment(text: str, limit: int, overlap: int) -> list[str]:
    """Split one segment on the strongest boundary that fits.

    Preference order is paragraph, then sentence, then hard cut. Cutting
    mid-sentence is the worst outcome available: the chunk that gets retrieved
    then opens on half a clause, and the model completes the thought itself.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    out:   list[str] = []
    start = 0
    n     = len(text)

    while start < n:
        end = min(start + limit, n)
        if end < n:
            window = text[start:end]
            cut = window.rfind("\n\n")
            if cut < limit * 0.4:
                m = None
                for m in re.finditer(r"[.!?…](?=\s)", window):
                    pass                      # keep the last sentence end
                cut = m.end() if m and m.end() > limit * 0.4 else -1
            if cut < limit * 0.4:
                cut = window.rfind(" ")
            if cut > limit * 0.4:
                end = start + cut
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def _build_chunks(doc_id: str, parsed: ParsedDocument) -> list[Chunk]:
    """Segment boundaries are always chunk boundaries.

    A chunk never spans two pages or two sheets, so its citation is unambiguous.
    The alternative — packing small segments together to fill the budget — buys
    a slightly denser context and gives up the ability to say which page a figure
    came from, which is the wrong trade for a document the user is going to act
    on.
    """
    chunks: list[Chunk] = []
    for seg in parsed.segments:
        for piece in _split_segment(seg.text, CHUNK_CHARS, CHUNK_OVERLAP):
            toks = _tokenize(piece)
            if not toks:
                continue
            chunks.append(Chunk(
                doc_id=doc_id,
                index=len(chunks),
                text=piece,
                source=seg.label,
                tokens=toks,
                length=len(toks),
            ))
    return chunks


def _build_lexical_index(doc: LoadedDocument) -> None:
    df: dict[str, int] = {}
    total = 0
    for ch in doc.chunks:
        total += ch.length
        for term in set(ch.tokens):
            df[term] = df.get(term, 0) + 1
    doc._df      = df
    doc._avg_len = (total / len(doc.chunks)) if doc.chunks else 0.0


def _bm25_scores(doc: LoadedDocument, query_terms: list[str]) -> tuple[list[float], float]:
    """Okapi BM25 over the document's own chunks.

    Returns (scores, ideal), where `ideal` is roughly the score a chunk would get
    if it contained every query term once at average length. Dividing the best
    actual score by it gives a scale-free measure of how much of the question the
    document actually addresses — which is what MIN_RELEVANCE is checked against.

    Terms absent from the document entirely are counted into `ideal` at the idf
    they WOULD have if they appeared once, and this is the detail the whole
    relevance gate turns on. Skipping them (the obvious implementation, since
    they contribute nothing to any chunk's score) makes `ideal` the sum over only
    the terms that happen to be present — so a question whose every meaningful
    word is missing from the document scores a perfect match on the one common
    word that survived.
    """
    n = len(doc.chunks)
    if not n or not query_terms:
        return [0.0] * n, 0.0

    # idf a term would carry if it appeared in exactly one chunk: the ceiling.
    idf_unseen = math.log(1 + (n - 1 + 0.5) / 1.5)

    idf: dict[str, float] = {}
    ideal = 0.0
    for term in set(query_terms):
        df = doc._df.get(term, 0)
        if df:
            # +1 inside the log keeps the value positive for a term present in
            # every chunk, which plain Robertson IDF drives negative.
            idf[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))
            ideal += idf[term]
        else:
            ideal += idf_unseen

    if not idf:
        return [0.0] * n, ideal

    avg = doc._avg_len or 1.0
    out = []
    for ch in doc.chunks:
        tf: dict[str, int] = {}
        for t in ch.tokens:
            if t in idf:
                tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term, f in tf.items():
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * ch.length / avg)
            score += idf[term] * (f * (_BM25_K1 + 1)) / (denom or 1.0)
        out.append(score)
    return out, ideal


# ── Embeddings ───────────────────────────────────────────────────────────────

def _embed_texts(texts: list[str], task_type: str, timeout: float | None = None):
    """Embed `texts` with the Gemini embedding model. Returns a normalised
    float32 matrix, or None on any failure.

    Normalising here means cosine similarity is a plain dot product at query
    time, which keeps retrieval to one matrix multiply.
    """
    if not texts:
        return None
    try:
        import numpy as np
        from google import genai
        from google.genai import types

        from core.env_config import get_api_key
        key = get_api_key()
        if not key:
            return None

        client  = genai.Client(api_key=key)
        vectors = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]

            # A 429 from the embedding endpoint is a per-minute rate limit, not a
            # refusal — the free tier allows 100 requests a minute and the server
            # tells you how long to wait. Treating it as fatal costs the document
            # its semantic index for the rest of the session over a pause of a
            # few seconds. Bounded to two retries so a genuinely exhausted quota
            # still degrades to lexical search promptly instead of hanging a
            # background thread on an endpoint that is not going to recover.
            for attempt in range(3):
                try:
                    resp = client.models.embed_content(
                        model=EMBED_MODEL,
                        contents=batch,
                        config=types.EmbedContentConfig(
                            task_type=task_type,
                            output_dimensionality=EMBED_DIMS,
                        ),
                    )
                    break
                except Exception as e:
                    transient = ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
                                 or "503" in str(e) or "UNAVAILABLE" in str(e))
                    if not transient or attempt == 2 or timeout is not None:
                        # `timeout is not None` marks the query-side call, which
                        # is already racing a deadline — retrying there would
                        # spend the budget the caller set aside for an answer.
                        raise
                    delay = _retry_delay(str(e), default=2.0 * (attempt + 1))
                    print(f"[Documents] Embedding rate-limited, retrying in {delay:.0f}s")
                    time.sleep(delay)

            for e in resp.embeddings:
                vectors.append(e.values)

        mat = np.asarray(vectors, dtype="float32")
        if mat.ndim != 2 or not mat.size:
            return None
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms
    except Exception as e:
        print(f"[Documents] Embedding failed ({type(e).__name__}): {e}")
        return None


# ── The store ────────────────────────────────────────────────────────────────

class DocumentStore:
    """Session-scoped. Documents live for as long as the process does, survive
    session reconnects (they belong to the conversation, not the socket), and
    are dropped on shutdown."""

    def __init__(self) -> None:
        self._docs: dict[str, LoadedDocument] = {}
        self._order: list[str] = []          # ingest order, newest last
        self._active: str = ""
        self._lock = threading.RLock()
        self._log: Callable[[str], None] | None = None

    # -- wiring -------------------------------------------------------------

    def set_logger(self, fn: Callable[[str], None]) -> None:
        """Route progress to the HUD activity log, the way memory_manager's
        trim notifier does. A background embedding pass that reports only to
        stdout is invisible to the person waiting on it."""
        self._log = fn

    def _say(self, msg: str) -> None:
        print(f"[Documents] {msg}")
        if self._log:
            try:
                self._log(f"SYS: {msg}")
            except Exception:
                pass

    # -- ingest -------------------------------------------------------------

    def ingest(self, path: str | Path) -> tuple[bool, str]:
        """Parse, chunk and index a document. Blocking — call it off the Qt
        thread and off the event loop. Returns (ok, sentence_for_the_user).

        Re-ingesting a path that is already loaded just re-activates it: a user
        who drops the same file twice means "let us talk about this one", not
        "parse it again".
        """
        p = Path(path)
        doc_id = str(p.resolve()).lower()

        with self._lock:
            existing = self._docs.get(doc_id)
            if existing and existing.ingested_at >= _mtime(p):
                self._active = doc_id
                return True, (f"'{existing.name}' is already loaded and is now the "
                              f"active document.")

        t0     = time.monotonic()
        parsed = document_parser.parse(p)
        if not parsed.ok:
            return False, parsed.error or f"Could not read '{p.name}'."

        chunks = _build_chunks(doc_id, parsed)
        if not chunks:
            return False, f"No readable text in '{p.name}'."

        doc = LoadedDocument(
            doc_id=doc_id,
            name=parsed.name,
            path=str(p),
            kind=parsed.kind,
            extractor=parsed.extractor,
            char_count=parsed.char_count,
            segments=len(parsed.segments),
            full_text=parsed.text,
            parts=[(s.label, s.text) for s in parsed.segments],
            chunks=chunks,
            outline=[s.label for s in parsed.segments[:40]],
            preview=_preview(parsed.text),
            warnings=list(parsed.warnings),
        )
        _build_lexical_index(doc)

        with self._lock:
            self._docs[doc_id] = doc
            if doc_id not in self._order:
                self._order.append(doc_id)
            self._active = doc_id

        took = time.monotonic() - t0
        self._say(
            f"Read {doc.name} — {doc.segments} {parsed.segment_noun}, "
            f"{doc.char_count:,} chars, {len(doc.chunks)} chunks "
            f"via {doc.extractor} in {took:.1f}s"
        )
        for w in doc.warnings:
            self._say(f"{doc.name}: {w}")

        self._maybe_embed(doc)

        return True, self.describe(doc_id)

    def _maybe_embed(self, doc: LoadedDocument) -> None:
        """Kick off the vector pass in the background, if it is worth doing.

        Deliberately fire-and-forget on a daemon thread: the user's first
        question must not wait on it, and a process exiting mid-embed should not
        be held open by it.
        """
        if doc.char_count < EMBED_MIN_CHARS:
            doc.embed_state = "off"
            doc.embed_note  = "document small enough for lexical search alone"
            return
        if not embeddings_enabled():
            doc.embed_state = "off"
            doc.embed_note  = "disabled in config"
            return

        doc.embed_state = "pending"

        def _worker() -> None:
            t0  = time.monotonic()
            mat = _embed_texts([c.text for c in doc.chunks], "RETRIEVAL_DOCUMENT")
            with self._lock:
                if mat is None or len(mat) != len(doc.chunks):
                    doc.embed_state = "failed"
                    doc.embed_note  = "embedding pass failed — using lexical search"
                    self._say(f"{doc.name}: semantic index unavailable, "
                              f"lexical search still active.")
                    return
                doc._vectors    = mat
                doc.embed_state = "ready"
                doc.embed_note  = ""
            self._say(f"{doc.name}: semantic index ready "
                      f"({len(doc.chunks)} chunks, {time.monotonic() - t0:.1f}s)")

        threading.Thread(target=_worker, daemon=True,
                         name=f"embed-{doc.name[:20]}").start()

    # -- lookup -------------------------------------------------------------

    def has_documents(self) -> bool:
        with self._lock:
            return bool(self._docs)

    def active(self) -> LoadedDocument | None:
        with self._lock:
            return self._docs.get(self._active)

    def all_documents(self) -> list[LoadedDocument]:
        with self._lock:
            return [self._docs[d] for d in self._order if d in self._docs]

    def resolve(self, name_or_path: str = "") -> LoadedDocument | None:
        """Find a document by (partial, case-insensitive) name or by path.
        Falls back to the active document when nothing is given — which is the
        common case, because the user says "this file", not its filename."""
        with self._lock:
            if not name_or_path:
                return self._docs.get(self._active)

            needle = name_or_path.strip().lower()
            exact  = self._docs.get(str(Path(name_or_path).resolve()).lower())
            if exact:
                return exact
            for doc_id in reversed(self._order):
                doc = self._docs.get(doc_id)
                if doc and doc.name.lower() == needle:
                    return doc
            for doc_id in reversed(self._order):
                doc = self._docs.get(doc_id)
                if doc and needle in doc.name.lower():
                    return doc
            return None

    def set_active(self, doc_id: str) -> None:
        with self._lock:
            if doc_id in self._docs:
                self._active = doc_id

    def clear(self) -> None:
        """Drop everything. Called at shutdown so chunk text and vectors do not
        outlive the session that loaded them."""
        with self._lock:
            self._docs.clear()
            self._order.clear()
            self._active = ""

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id not in self._docs:
                return False
            del self._docs[doc_id]
            self._order = [d for d in self._order if d != doc_id]
            if self._active == doc_id:
                self._active = self._order[-1] if self._order else ""
            return True

    # -- retrieval ----------------------------------------------------------

    def retrieve(self, query: str, doc: LoadedDocument,
                 budget: int = CONTEXT_CHAR_BUDGET) -> list[Chunk]:
        """Rank `doc`'s chunks against `query` and return the best of them in
        reading order, inside `budget` characters.

        Reading order matters and is easy to lose: returning chunks sorted by
        score hands the model page 40 before page 3, and it will narrate them in
        that order. Selection is by relevance; presentation is by position.
        """
        terms = _tokenize(query)
        if not terms:
            return doc.chunks[: max(1, budget // CHUNK_CHARS)]

        lexical, ideal = _bm25_scores(doc, terms)

        # Does this document address the question at all? Decided on the raw
        # scores, before any normalisation flattens the answer away.
        coverage = (max(lexical) / ideal) if ideal > 0 else 0.0
        sem = self._vector_scores(query, doc) if doc._vectors is not None else None
        if coverage < MIN_RELEVANCE:
            # One reprieve: a ready semantic index can still recognise a question
            # phrased entirely in words the document never uses ("turnover" for
            # "revenue"), which is the exact case lexical matching cannot see.
            if sem is None or max(sem) < 0.62:
                return []

        fused   = _normalise(lexical)
        weights = BM25_WEIGHT
        if sem is not None:
            fused = [
                BM25_WEIGHT * lex + VECTOR_WEIGHT * s
                for lex, s in zip(fused, _normalise_range(sem))
            ]
            weights = BM25_WEIGHT + VECTOR_WEIGHT

        # Back to 0..1 so KEEP_RATIO means the same thing whether or not the
        # semantic half contributed.
        fused = [f / weights for f in fused]
        best  = max(fused) if fused else 0.0
        floor = best * KEEP_RATIO

        ranked = sorted(range(len(fused)), key=lambda i: fused[i], reverse=True)
        picked: set[int] = set()
        for i in ranked[:TOP_K]:
            if fused[i] < floor:
                break
            picked.add(i)

        if not picked:
            return []

        # Pull in the immediate neighbours of the strongest hits. A definition is
        # routinely in the chunk before the one that uses the term, and the
        # overlap alone does not always carry it.
        for i in list(picked):
            if fused[i] >= best * 0.75:
                for nb in (i - 1, i + 1):
                    if 0 <= nb < len(doc.chunks):
                        picked.add(nb)

        out:  list[Chunk] = []
        used = 0
        for i in sorted(picked):
            ch = doc.chunks[i]
            if used + len(ch.text) > budget:
                continue
            out.append(ch)
            used += len(ch.text)
        return out

    def _vector_scores(self, query: str, doc: LoadedDocument):
        """Cosine similarity of the query against the chunk matrix, or None.

        The embedding call is a network round trip, so it is capped: past
        QUERY_EMBED_TIMEOUT the answer goes out on lexical scores alone. A
        semantically better answer that arrives two seconds late is, in a voice
        assistant, a worse answer.
        """
        result: dict[str, object] = {}

        def _work() -> None:
            # Passing the deadline through is what suppresses the rate-limit
            # retry inside _embed_texts: on the query side there is no time to
            # wait out a 429, and the lexical scores are already in hand.
            result["v"] = _embed_texts([query], "RETRIEVAL_QUERY",
                                       timeout=QUERY_EMBED_TIMEOUT)

        t = threading.Thread(target=_work, daemon=True, name="embed-query")
        t.start()
        t.join(QUERY_EMBED_TIMEOUT)
        qv = result.get("v")
        if qv is None:
            if t.is_alive():
                print("[Documents] Query embedding timed out — lexical only.")
            return None
        try:
            import numpy as np
            return (np.asarray(doc._vectors) @ np.asarray(qv)[0]).tolist()
        except Exception:
            return None

    # -- prompt surfaces ----------------------------------------------------

    def manifest_for_prompt(self) -> str:
        """The block that goes into the system prompt at connect time.

        This is a table of contents, never the contents. It exists so the model
        knows a document is loaded and knows it has not read it — the same job
        the [ALSO REMEMBERED] key index does for memory. Without it the model
        answers document questions from its own knowledge and never calls the
        tool, which is the single most likely way this feature fails in practice.
        """
        docs = self.all_documents()
        if not docs:
            return ""

        lines = [
            "[DOCUMENTS LOADED — you have NOT read these. Their text is not in "
            "this prompt; call files(action='query') to read any of them]"
        ]
        active = self.active()
        for doc in docs[-5:]:
            mark = " (ACTIVE)" if active and doc.doc_id == active.doc_id else ""
            lines.append(
                f"- {doc.name}{mark} — {doc.kind.upper()}, {doc.segments} "
                f"{_noun_for(doc.kind)}, ~{doc.char_count:,} characters."
            )
            if doc.preview:
                lines.append(f"  Opens with: {doc.preview}")
        lines.append(
            "To answer ANY question about these — including a summary, a figure, "
            "a date, a name or a comparison — call files with action='query' "
            "and your question first. Never answer from memory or guess what a "
            "document says."
        )
        return "\n".join(lines) + "\n"

    def describe(self, doc_id: str = "") -> str:
        """One sentence about a freshly ingested document, for the assistant to
        paraphrase to the user."""
        doc = self._docs.get(doc_id) if doc_id else self.active()
        if not doc:
            return "No document is loaded."
        bits = [f"{doc.name} is loaded: {doc.kind.upper()}, {doc.segments} "
                f"{_noun_for(doc.kind)}, about {doc.char_count:,} characters"]
        if doc.is_small:
            bits.append("small enough to read in full")
        else:
            bits.append(f"indexed into {len(doc.chunks)} searchable passages")
        if doc.outline:
            head = ", ".join(doc.outline[:4])
            if doc.kind in ("docx", "text"):
                bits.append(f"sections include {head}")
        return "; ".join(bits) + "."

    def build_context(self, query: str, doc: LoadedDocument,
                      budget: int = CONTEXT_CHAR_BUDGET) -> str:
        """Assemble the payload files(action='query') hands back to the model.

        Three things every returned block carries, and why:

          • The source label on each passage, so the model can cite a page.
          • An explicit instruction to answer only from what is here. A voice
            assistant that pads a document answer with plausible general
            knowledge is worse than one that says the document does not cover it.
          • An explicit statement when retrieval found nothing, rather than an
            empty block — silence invites the model to fill it.
        """
        header = f"[DOCUMENT: {doc.name} — {doc.kind.upper()}, {doc.segments} {_noun_for(doc.kind)}]"

        if doc.is_small:
            # The whole document, but still carved into labelled parts. Joining
            # the text into one block would be simpler and would silently cost
            # the thing a short document is BEST placed to give: an exact
            # citation. A six-page PDF that fits in context should still let the
            # assistant say "on page four", and it can only do that if the page
            # boundaries survive. Same rendering as the retrieval path below, so
            # the model sees one format either way.
            return "\n".join([
                header,
                "[This is the COMPLETE document text.]",
                "",
                _render_parts(doc.parts, budget),
                "",
                _ANSWER_RULE,
            ])

        picked = self.retrieve(query, doc, budget)
        if not picked:
            return "\n".join([
                header,
                f"[No passage in this document matches: {query}]",
                "",
                "Tell the user plainly that this document does not appear to "
                "cover that, and offer what it does cover. Do NOT answer from "
                "general knowledge and do NOT invent a passage.",
            ])

        parts = [
            header,
            f"[{len(picked)} most relevant passages for: {query}. "
            f"The rest of the document is NOT shown.]",
            "",
        ]
        for ch in picked:
            parts.append(f"--- [{ch.source}] ---")
            parts.append(ch.text)
            parts.append("")
        parts.append(_ANSWER_RULE)
        return "\n".join(parts)

    def build_overview(self, doc: LoadedDocument,
                       budget: int = CONTEXT_CHAR_BUDGET) -> str:
        """Assemble a whole-document view for "summarise this" style requests.

        Retrieval is the wrong tool for a summary and fails at it in a way that
        looks like success: ranking chunks against the word "summary" returns
        whatever part of the document happens to discuss summaries. Worse, the
        trigger word differs per language, so no keyword check could route around
        it — which is why the model sets scope='overview' explicitly instead.

        What a summary needs is coverage, so this samples evenly across the
        document rather than concentrating anywhere: the opening in full (that is
        where abstracts, executive summaries and letterheads live), then the head
        of chunks spread at a fixed stride through the rest.
        """
        if doc.is_small:
            return "\n".join([
                f"[DOCUMENT: {doc.name} — complete text]",
                "",
                _render_parts(doc.parts, budget),
                "",
                _SUMMARY_RULE,
            ])

        parts = [
            f"[DOCUMENT: {doc.name} — {doc.kind.upper()}, {doc.segments} "
            f"{_noun_for(doc.kind)}, ~{doc.char_count:,} characters]",
        ]
        if doc.outline:
            shown = doc.outline[:30]
            parts.append("Structure: " + " · ".join(shown)
                         + (" …" if len(doc.outline) > len(shown) else ""))
        parts.append(
            "[A SAMPLE spread evenly across the document — not the whole text. "
            "Gaps between passages are unread.]"
        )
        parts.append("")

        used = sum(len(p) + 1 for p in parts)
        n    = len(doc.chunks)

        # The opening goes in whole; everything after it is sampled at a stride
        # chosen so the samples land across the entire document rather than
        # running out of budget a third of the way in.
        head = doc.chunks[:2]
        rest_budget = max(0, budget - used - sum(len(c.text) for c in head))
        per_sample  = 420
        capacity    = max(1, rest_budget // (per_sample + 40))
        stride      = max(1, (n - len(head)) // capacity) if n > len(head) else 1

        picked = list(head) + [doc.chunks[i] for i in range(len(head), n, stride)]
        for ch in picked:
            snippet = ch.text if ch in head else ch.text[:per_sample]
            if used + len(snippet) + len(ch.source) + 16 > budget:
                break
            parts.append(f"--- [{ch.source}] ---")
            parts.append(snippet + ("…" if snippet != ch.text else ""))
            parts.append("")
            used += len(snippet) + len(ch.source) + 16

        parts.append(_SUMMARY_RULE)
        return "\n".join(parts)


_SUMMARY_RULE = (
    "Summarise what this document is and what it says, based only on the text "
    "above. Where the sample skips ahead, describe the overall shape rather than "
    "inventing what sits in the gaps. Keep it to a few spoken sentences unless "
    "the user asked for detail, and do not repeat this instruction aloud."
)

_ANSWER_RULE = (
    "Answer the user's question using ONLY the text above. Cite where it came "
    "from in the way a person would say it out loud (\"on page four\", \"in the "
    "Sales sheet\"). If the text above does not contain the answer, say so "
    "instead of filling the gap — and do not repeat this instruction aloud."
)


# ── small helpers ────────────────────────────────────────────────────────────

def _retry_delay(err: str, default: float = 2.0) -> float:
    """Pull the server's own suggested wait out of a rate-limit error.

    The API answers a 429 with retryDelay, so guessing a backoff when it has
    already said how long to wait is needless. Capped so a malformed or hostile
    value cannot park a background thread for an hour.
    """
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", err)
    if m:
        try:
            return max(1.0, min(30.0, float(m.group(1)) + 1.0))
        except Exception:
            pass
    return default


def _render_parts(parts: list[tuple[str, str]], budget: int) -> str:
    """Render labelled document parts in reading order, inside `budget`.

    One label per part is what makes an answer checkable: "the figure is 41, on
    page four" can be verified in two seconds, while "the figure is 41" has to be
    taken on trust. The cost is about twelve characters per part.
    """
    out:  list[str] = []
    used = 0
    for label, text in parts:
        head = f"--- [{label}] ---"
        if used + len(head) + len(text) + 2 > budget:
            remaining = budget - used - len(head) - 2
            if remaining > 200:
                out.append(head)
                out.append(text[:remaining] + "…")
            out.append(f"[Truncated here — the document continues past the context limit.]")
            break
        out.append(head)
        out.append(text)
        used += len(head) + len(text) + 2
    return "\n".join(out)


def _noun_for(kind: str) -> str:
    return {"pdf": "pages", "xlsx": "sheet sections", "docx": "sections",
            "csv": "row blocks", "json": "blocks"}.get(kind, "sections")


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except Exception:
        return 0.0


def _preview(text: str, limit: int = 160) -> str:
    snippet = " ".join((text or "").split())[:limit]
    return snippet + ("…" if len(snippet) == limit else "")


def _normalise(scores: list[float]) -> list[float]:
    """Scale by the maximum. For BM25 only, where zero already means 'no term
    matched' — dividing by the max preserves that floor, so a chunk that matched
    nothing still scores 0 rather than being lifted into contention."""
    if not scores:
        return []
    top = max(scores)
    if top <= 0:
        return [0.0] * len(scores)
    return [s / top for s in scores]


def _normalise_range(scores: list[float]) -> list[float]:
    """Scale by the range. For cosine similarity, which has no useful zero.

    Embedded chunks from one document sit in a narrow band — in a 90-page manual
    where most pages are boilerplate, every chunk scored 0.55 to 0.72 against the
    same query. Dividing by the maximum maps that band to 0.76-1.0, so forty
    interchangeable filler pages all clear the keep threshold and the answer
    arrives buried in them. Subtracting the floor first spends the full 0-1 range
    on the differences that actually distinguish the chunks, which is the only
    part of a cosine score that carries information.
    """
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    span = hi - lo
    if span <= 1e-9:
        return [0.0] * len(scores)
    return [(s - lo) / span for s in scores]


# One store per process, imported directly by the action and by main.py. The
# same shape as core/undo.py's module-level stack: shared mutable state that
# several threads reach without being handed a reference through every call.
store = DocumentStore()
