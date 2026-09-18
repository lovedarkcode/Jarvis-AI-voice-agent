"""
core/document_parser.py — text extraction for the document-intelligence pipeline.

WHAT THIS FILE IS RESPONSIBLE FOR
    Turning a file on disk into text, and nothing else. No chunking, no ranking,
    no prompts, no model calls. That separation matters because extraction is the
    only part of the pipeline whose correctness depends on a third-party library
    getting a binary format right — keeping it alone in one file means a bad PDF
    can be diagnosed without reading anything about retrieval.

WHY SEGMENTS RATHER THAN ONE STRING
    Every extractor here returns a list of Segments, where a segment is the
    natural unit of the source: a PDF page, a section under a docx heading, a
    slice of an Excel sheet. Two things fall out of that for free:

      • Citations. The assistant can say "page 14" or "sheet 'Q3', rows 40-80"
        instead of "somewhere in the document", which is the difference between
        an answer a person can check and one they have to trust.
      • Chunk boundaries that mean something. Splitting a 90-page PDF purely by
        character count puts the end of page 7 and the start of page 8 in one
        chunk with no marker between them; the model then attributes a figure to
        the wrong section. Segments give the chunker real seams to cut on.

THE PDF CASCADE
    There is no single PDF library that is both fast and good at layout, so this
    tries them in order and stops at the first that returns real text:

      pypdfium2   — C-backed, by far the fastest, good plain-text extraction
      pypdf       — pure Python, no native build step, reliable fallback
      pdfplumber  — slowest, but the best at tables and multi-column layout
      PyPDF2      — legacy; only here because actions/file_processor.py already
                    depends on it, so some installs will have it and nothing else

    A PDF with no text layer (a scan, a photographed contract) extracts to
    roughly nothing from every one of them. That is not an error and must not be
    reported as one — it is a real and common document that needs OCR. The
    parser detects it explicitly and says so, because "extraction failed" sends
    someone debugging their install for a problem that is in the file.

NOTHING HERE RAISES
    Every public function returns a ParsedDocument carrying `ok`, `warnings` and
    `error`. A malformed upload must degrade to a spoken sentence, never a
    traceback in the receive loop — main.py's tool dispatch already catches, but
    an extractor that throws past its own try block loses the diagnostic that
    tells the user what was actually wrong with their file.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Extensions this module can turn into text. Anything else belongs to
# actions/file_processor.py, which handles images, audio, video and archives.
#
# PDF / DOCX / XLSX are the feature. TXT, MD, CSV and JSON are here because they
# traverse the identical code path at zero extra cost, and a user who drops a
# .csv onto the HUD expects it to be readable for the same reason they expect a
# .xlsx to be.
SUPPORTED_EXTS = {
    ".pdf",
    ".docx",
    ".xlsx", ".xlsm",
    ".txt", ".md", ".rst", ".log",
    ".csv", ".tsv",
    ".json",
}

# Below this many extracted characters a PDF is treated as having no text layer
# rather than as having failed. Chosen above zero because a scanned page often
# yields a few stray glyphs from a header stamp or a digital signature.
_SCANNED_PDF_THRESHOLD = 120

# Rows per Excel segment. Large enough that a segment carries enough rows for a
# question about a trend, small enough that one segment is not the whole sheet.
_XLSX_ROWS_PER_SEGMENT = 200

# Cell values longer than this are truncated. A spreadsheet with a pasted essay
# in one cell should not be allowed to dominate the sheet it lives in.
_XLSX_MAX_CELL_CHARS = 300


@dataclass
class Segment:
    """One citable unit of a document."""
    label: str          # "page 4", "sheet 'Sales' rows 2-201", "Introduction"
    text:  str
    index: int = 0      # position in reading order


@dataclass
class ParsedDocument:
    name:       str
    path:       str
    kind:       str                       # "pdf" | "docx" | "xlsx" | "text" | "csv" | "json"
    segments:   list[Segment] = field(default_factory=list)
    extractor:  str = ""                  # which library actually produced the text
    warnings:   list[str] = field(default_factory=list)
    error:      str = ""

    @property
    def ok(self) -> bool:
        return not self.error and any(s.text.strip() for s in self.segments)

    @property
    def text(self) -> str:
        """The whole document in reading order. Used for the small-document path
        where retrieval is skipped entirely."""
        return "\n\n".join(s.text for s in self.segments if s.text.strip())

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)

    @property
    def segment_noun(self) -> str:
        """What a segment is called for this kind of file, for the manifest."""
        return {"pdf": "pages", "xlsx": "sheet sections",
                "docx": "sections"}.get(self.kind, "sections")


# ── helpers ──────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    """Normalise whitespace without destroying structure.

    Extractors emit a lot of noise: form feeds, non-breaking spaces, runs of
    blank lines where a figure used to be, and soft hyphens left over from
    justified text. Collapsing it here means the chunker's character budget is
    spent on content rather than on whitespace, which is worth a few percent of
    the context window on a long PDF.
    """
    if not text:
        return ""
    text = text.replace("\x0c", "\n").replace("\xa0", " ").replace("­", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_probably_scanned(segments: list[Segment]) -> bool:
    return sum(len(s.text.strip()) for s in segments) < _SCANNED_PDF_THRESHOLD


# ── PDF ──────────────────────────────────────────────────────────────────────

def _pdf_pypdfium2(path: Path) -> list[Segment]:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    try:
        out = []
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                raw = page.get_textpage().get_text_range()
            finally:
                page.close()
            out.append(Segment(label=f"page {i + 1}", text=_clean(raw), index=i))
        return out
    finally:
        pdf.close()


def _pdf_pypdf(path: Path) -> list[Segment]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return [
        Segment(label=f"page {i + 1}", text=_clean(p.extract_text() or ""), index=i)
        for i, p in enumerate(reader.pages)
    ]


def _pdf_pdfplumber(path: Path) -> list[Segment]:
    """Slowest of the four, but the only one that recovers tables as tables.

    Tables are rendered as pipe-delimited rows rather than left as the loose
    stream of numbers the plain-text extractors produce, because a row of eight
    bare figures with no column context is worse than useless to the model — it
    invites confident answers built on the wrong column.
    """
    import pdfplumber
    out = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            body = _clean(page.extract_text() or "")
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for t_i, table in enumerate(tables):
                rows = [
                    " | ".join((str(c).strip() if c is not None else "") for c in row)
                    for row in table if any(c is not None and str(c).strip() for c in row)
                ]
                if rows:
                    body += f"\n\n[table {t_i + 1}]\n" + "\n".join(rows)
            out.append(Segment(label=f"page {i + 1}", text=body.strip(), index=i))
    return out


def _pdf_pypdf2(path: Path) -> list[Segment]:
    import PyPDF2
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        return [
            Segment(label=f"page {i + 1}", text=_clean(p.extract_text() or ""), index=i)
            for i, p in enumerate(reader.pages)
        ]


_PDF_EXTRACTORS = (
    ("pypdfium2",  _pdf_pypdfium2),
    ("pypdf",      _pdf_pypdf),
    ("pdfplumber", _pdf_pdfplumber),
    ("PyPDF2",     _pdf_pypdf2),
)


def _parse_pdf(path: Path, doc: ParsedDocument) -> ParsedDocument:
    tried: list[str] = []
    for name, fn in _PDF_EXTRACTORS:
        try:
            segments = fn(path)
        except ImportError:
            continue                      # library absent — next in the cascade
        except Exception as e:
            tried.append(f"{name} ({type(e).__name__})")
            continue

        if _is_probably_scanned(segments):
            # Keep going: a library that handles this file's encoding better may
            # still find the text layer. Only after all four agree is it a scan.
            tried.append(f"{name} (no text layer)")
            continue

        doc.segments  = segments
        doc.extractor = name
        if tried:
            doc.warnings.append("Fell back past: " + ", ".join(tried))
        return doc

    if not tried:
        doc.error = ("No PDF library is installed. Run: "
                     "pip install pypdfium2 pypdf pdfplumber")
        return doc

    doc.error = (
        f"'{path.name}' has no extractable text layer — it is almost certainly a "
        f"scan or a photo saved as a PDF. Reading it needs OCR, which this "
        f"pipeline does not do. (Tried: {', '.join(tried)}.)"
    )
    return doc


# ── DOCX ─────────────────────────────────────────────────────────────────────

def _parse_docx(path: Path, doc: ParsedDocument) -> ParsedDocument:
    """Sectioned by heading, with tables preserved.

    The existing file_processor joins only `doc.paragraphs`, which silently drops
    every table in the file — and in the documents people actually upload
    (invoices, reports, specs) the table is usually where the answer is. Walking
    the body in document order instead keeps paragraphs and tables interleaved as
    they appear on the page.
    """
    try:
        import docx
        from docx.document import Document as _Doc
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        doc.error = "python-docx is not installed. Run: pip install python-docx"
        return doc

    try:
        d = docx.Document(str(path))
    except Exception as e:
        doc.error = f"Could not open '{path.name}' as a Word document: {e}"
        return doc

    def _iter_block_items(parent):
        """Yield Paragraphs and Tables in true document order.

        python-docx exposes .paragraphs and .tables as two separate flat lists
        with no way to interleave them, so the underlying XML body has to be
        walked directly. This is the documented approach and is stable across
        versions.
        """
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        body = parent.element.body if isinstance(parent, _Doc) else parent._element
        for child in body.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, parent)
            elif isinstance(child, CT_Tbl):
                yield Table(child, parent)

    sections: list[tuple[str, list[str]]] = []
    state = {"title": "", "body": []}

    def _flush():
        if state["body"] and any(b.strip() for b in state["body"]):
            sections.append((state["title"], list(state["body"])))

    try:
        for block in _iter_block_items(d):
            if isinstance(block, Paragraph):
                text  = (block.text or "").strip()
                style = (block.style.name if block.style is not None else "") or ""
                if not text:
                    continue
                if style.startswith("Heading") or style == "Title":
                    _flush()
                    level = 1
                    m = re.search(r"(\d+)", style)
                    if m:
                        level = max(1, min(6, int(m.group(1))))
                    state["title"] = text
                    state["body"]  = [f"{'#' * level} {text}"]
                else:
                    state["body"].append(text)
            else:  # Table
                rows = []
                for row in block.rows:
                    cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    state["body"].append("\n[table]\n" + "\n".join(rows))
        _flush()
    except Exception as e:
        doc.warnings.append(f"Stopped early while reading the document: {e}")
        _flush()

    if not sections:
        doc.error = f"'{path.name}' contains no readable text."
        return doc

    doc.segments = [
        Segment(label=(title or f"section {i + 1}"), text=_clean("\n".join(body)), index=i)
        for i, (title, body) in enumerate(sections)
    ]
    doc.extractor = "python-docx"
    return doc


# ── XLSX ─────────────────────────────────────────────────────────────────────

def _parse_xlsx(path: Path, doc: ParsedDocument) -> ParsedDocument:
    """One segment per slice of rows, with the header row repeated in each.

    Repeating the header is the single most important detail in spreadsheet
    retrieval. A chunk that begins at row 400 is, without it, an anonymous grid
    of numbers: the model has no way to know the third column is revenue, so it
    guesses — and a plausible wrong number is the worst possible output for a
    finance question. Repeating one line per chunk costs almost nothing and makes
    every chunk independently interpretable.

    Cells are read with values_only so formulas resolve to their cached results;
    the formula text itself is not what anybody is asking about.
    """
    try:
        import openpyxl
    except ImportError:
        doc.error = "openpyxl is not installed. Run: pip install openpyxl"
        return doc

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        doc.error = f"Could not open '{path.name}' as a workbook: {e}"
        return doc

    def _cell(v) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        return s[:_XLSX_MAX_CELL_CHARS] + "…" if len(s) > _XLSX_MAX_CELL_CHARS else s

    segments: list[Segment] = []
    idx = 0
    try:
        for ws in wb.worksheets:
            header_line  = ""
            buf: list[str] = []
            first_row_no = 1
            row_no       = 0
            had_rows     = False

            for row_no, row in enumerate(ws.iter_rows(values_only=True), start=1):
                cells = [_cell(c) for c in row]
                if not any(cells):
                    continue
                line = " | ".join(cells).rstrip(" |")
                if not header_line:
                    # The first non-empty row is treated as the header. Not always
                    # true, but right far more often than any heuristic that tries
                    # to be cleverer, and a wrong guess only costs one repeated
                    # line per chunk.
                    header_line = line
                    continue
                if not buf:
                    first_row_no = row_no
                buf.append(line)
                had_rows = True

                if len(buf) >= _XLSX_ROWS_PER_SEGMENT:
                    segments.append(Segment(
                        label=f"sheet '{ws.title}' rows {first_row_no}-{row_no}",
                        text=header_line + "\n" + "\n".join(buf),
                        index=idx,
                    ))
                    idx += 1
                    buf = []

            if buf:
                segments.append(Segment(
                    label=f"sheet '{ws.title}' rows {first_row_no}-{row_no}",
                    text=header_line + "\n" + "\n".join(buf),
                    index=idx,
                ))
                idx += 1
            elif header_line and not had_rows:
                # A sheet with a header and nothing else still deserves a mention;
                # "the Notes sheet is empty" is a real answer to a real question.
                segments.append(Segment(
                    label=f"sheet '{ws.title}' (header only)",
                    text=header_line,
                    index=idx,
                ))
                idx += 1
    except Exception as e:
        doc.warnings.append(f"Stopped early while reading the workbook: {e}")
    finally:
        try:
            wb.close()
        except Exception:
            pass

    if not segments:
        doc.error = f"'{path.name}' contains no readable cells."
        return doc

    doc.segments  = segments
    doc.extractor = "openpyxl"
    return doc


# ── plain text, CSV, JSON ────────────────────────────────────────────────────

def _read_text_file(path: Path) -> str:
    """UTF-8 first, then the platform default, then bytes with replacement.

    Files written by Excel on a Turkish or Japanese Windows are routinely cp1254
    or cp932, and failing to read one because of its encoding is the kind of bug
    that makes an assistant look broken on somebody else's machine and fine on
    yours.
    """
    for enc in ("utf-8", "utf-8-sig", None):
        try:
            return path.read_text(encoding=enc) if enc else path.read_text()
        except (UnicodeDecodeError, LookupError):
            continue
        except Exception:
            break
    return path.read_bytes().decode("utf-8", errors="replace")


def _parse_text(path: Path, doc: ParsedDocument) -> ParsedDocument:
    raw = _clean(_read_text_file(path))
    if not raw:
        doc.error = f"'{path.name}' is empty."
        return doc

    # Markdown splits on its own headings; everything else is one segment and is
    # handed to the chunker to divide by size.
    if path.suffix.lower() == ".md":
        parts = re.split(r"\n(?=#{1,6}\s)", raw)
        if len(parts) > 1:
            doc.segments = [
                Segment(label=(p.strip().splitlines()[0].lstrip("# ").strip()[:60]
                               or f"section {i + 1}"),
                        text=p.strip(), index=i)
                for i, p in enumerate(parts) if p.strip()
            ]
            doc.extractor = "markdown"
            return doc

    doc.segments  = [Segment(label="document", text=raw, index=0)]
    doc.extractor = "plain text"
    return doc


def _parse_csv(path: Path, doc: ParsedDocument) -> ParsedDocument:
    raw = _read_text_file(path)
    if not raw.strip():
        doc.error = f"'{path.name}' is empty."
        return doc
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
    except Exception:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel

    rows = list(csv.reader(io.StringIO(raw), dialect))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        doc.error = f"'{path.name}' contains no rows."
        return doc

    header = " | ".join((c or "").strip() for c in rows[0])
    body   = rows[1:]
    segments: list[Segment] = []
    for i in range(0, len(body), _XLSX_ROWS_PER_SEGMENT):
        block = body[i:i + _XLSX_ROWS_PER_SEGMENT]
        lines = [" | ".join((c or "").strip() for c in r) for r in block]
        segments.append(Segment(
            label=f"rows {i + 2}-{i + 1 + len(block)}",
            text=header + "\n" + "\n".join(lines),
            index=len(segments),
        ))
    doc.segments  = segments or [Segment(label="header", text=header, index=0)]
    doc.extractor = "csv"
    return doc


def _parse_json(path: Path, doc: ParsedDocument) -> ParsedDocument:
    raw = _read_text_file(path)
    try:
        data = json.loads(raw)
    except Exception as e:
        doc.warnings.append(f"Not valid JSON ({e}) — read as plain text.")
        return _parse_text(path, doc)

    # A top-level list is segmented per batch of items so retrieval can land on
    # the relevant records; anything else is pretty-printed as one segment.
    if isinstance(data, list) and len(data) > 20:
        segments = []
        for i in range(0, len(data), 50):
            block = data[i:i + 50]
            segments.append(Segment(
                label=f"items {i}-{i + len(block) - 1}",
                text=json.dumps(block, indent=1, ensure_ascii=False)[:20000],
                index=len(segments),
            ))
        doc.segments = segments
    else:
        doc.segments = [Segment(
            label="document",
            text=json.dumps(data, indent=1, ensure_ascii=False),
            index=0,
        )]
    doc.extractor = "json"
    return doc


# ── public entry point ───────────────────────────────────────────────────────

_KIND_BY_EXT = {
    ".pdf":  ("pdf",   _parse_pdf),
    ".docx": ("docx",  _parse_docx),
    ".xlsx": ("xlsx",  _parse_xlsx),
    ".xlsm": ("xlsx",  _parse_xlsx),
    ".csv":  ("csv",   _parse_csv),
    ".tsv":  ("csv",   _parse_csv),
    ".json": ("json",  _parse_json),
}


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def parse(path: str | Path) -> ParsedDocument:
    """Extract text from `path`. Never raises.

    Returns a ParsedDocument whose `.ok` is False and whose `.error` carries a
    sentence fit to be spoken to the user when extraction did not work.
    """
    p = Path(path)
    doc = ParsedDocument(name=p.name, path=str(p), kind="text")

    try:
        if not p.exists():
            doc.error = f"File not found: {p}"
            return doc
        if not p.is_file():
            doc.error = f"Not a file: {p}"
            return doc

        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            doc.error = (f"'{p.name}' is a {ext or 'extensionless'} file, which is not a "
                         f"readable document. Supported: PDF, Word, Excel, CSV, JSON, text.")
            return doc

        kind, fn = _KIND_BY_EXT.get(ext, ("text", _parse_text))
        doc.kind = kind
        doc = fn(p, doc)

        # Drop segments that survived extraction but hold nothing, then renumber
        # so `index` stays a contiguous reading order the chunker can rely on.
        doc.segments = [s for s in doc.segments if s.text.strip()]
        for i, s in enumerate(doc.segments):
            s.index = i

        if not doc.error and not doc.segments:
            doc.error = f"No text could be extracted from '{p.name}'."
        return doc

    except Exception as e:                                       # pragma: no cover
        doc.error = f"Could not read '{p.name}': {type(e).__name__}: {e}"
        return doc
