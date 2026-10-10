"""Request-scoped document extraction and retrieval for the BYOK web app.

Extracted segments stay in the visitor's browser memory. Each chat request
builds its own lexical index; no server session, shared store or Gemini key is
needed, including on serverless deployments.
"""
import re
import json
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from core.document_parser import ParsedDocument, Segment, parse
from core.document_store import DocumentStore, loaded_document

MAX_UPLOAD_BYTES = 3_500_000
MAX_TEXT_CHARS = 400_000
SUPPORTED = {'.pdf', '.docx', '.xlsx', '.xlsm', '.txt', '.md', '.rst',
             '.log', '.csv', '.tsv', '.json'}


class DocumentSegment(BaseModel):
    label: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class Attachment(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=16)
    segments: list[DocumentSegment] = Field(min_length=1, max_length=1500)

    @model_validator(mode='after')
    def bounded_text(self):
        if sum(len(s.text) for s in self.segments) > MAX_TEXT_CHARS:
            raise ValueError('Document text exceeds the limit.')
        return self


def extract_document(filename: str, data: bytes):
    name = filename.replace('\\', '/').rsplit('/', 1)[-1][:255]
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(400, 'Attach PDF, Word (.docx), Excel (.xlsx/.xlsm), or a text file. Convert older .doc/.xls files first.')
    if not data:
        raise HTTPException(400, 'The document is empty.')
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, 'Attach a document under 3.5 MB.')
    # Office files are ZIP archives. Bound expansion before handing them to
    # their parsers, so a small compressed upload cannot consume unbounded RAM.
    if suffix in {'.docx', '.xlsx', '.xlsm'}:
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 3000 or sum(e.file_size for e in entries) > 16_000_000:
                    raise HTTPException(413, 'This Office document is too large when expanded. Attach a smaller excerpt.')
        except (zipfile.BadZipFile, OSError):
            raise HTTPException(400, 'This Office file is unreadable. Save it again as .docx or .xlsx.') from None
    with tempfile.TemporaryDirectory(prefix='jarvis-document-') as directory:
        path = Path(directory) / ('upload' + suffix)
        path.write_bytes(data)
        parsed = parse(path)
    if not parsed.ok:
        if suffix == '.pdf':
            message = 'Could not read this PDF. Use an unlocked PDF with selectable text; scanned PDFs need OCR first.'
        else:
            message = 'Could not read this document. Check that it contains text or cells and save it again.'
        raise HTTPException(400, message)
    if parsed.char_count > MAX_TEXT_CHARS or len(parsed.segments) > 1500:
        raise HTTPException(413, 'This document exceeds 400,000 extracted characters or 1,500 sections. Attach a smaller excerpt.')
    attachment = Attachment(name=name, kind=parsed.kind, segments=[
        DocumentSegment(label=s.label[:500], text=s.text) for s in parsed.segments
    ])
    return {'attachment': attachment.model_dump(), 'characters': parsed.char_count,
            'warnings': ['Some content could not be extracted. Check the original document.'] if parsed.warnings else []}


def document_context(attachment: Attachment, query: str, question: str = ''):
    parsed = ParsedDocument(name=attachment.name, path='', kind=attachment.kind,
                            segments=[Segment(s.label, s.text, i) for i, s in enumerate(attachment.segments)])
    doc = loaded_document('request-document', parsed)
    store = DocumentStore()
    status = (f'[ATTACHMENT AVAILABLE: {json.dumps(doc.name, ensure_ascii=False)}; '
              f'{doc.kind.upper()}; {doc.segments} extracted sections; '
              f'{doc.char_count:,} extracted characters. The upload succeeded and '
              'document text is available in this request.]\n')
    if re.search(r'\b(summar\w*|overview|outline|whole document|entire document)\b', question or query, re.I):
        return status + store.build_overview(doc, budget=20000)
    if not doc.is_small and not store.retrieve(query, doc, budget=20000):
        # Availability/read requests rarely contain words from the PDF itself.
        # A lexical miss means no focused match, never a missing attachment.
        # Supply actual text so the assistant can identify/read the document,
        # while preserving the distinction between a sample and a full answer.
        return (status + '[No focused passage matched the question. The document is still '
                'attached. Representative excerpts follow; use them to answer general '
                'reading or availability questions. For a specific detail absent from '
                'these excerpts, say it was not found and ask for a section or topic. '
                'Do not claim the upload failed or ask the user to upload again.]\n'
                + store.build_overview(doc, budget=20000, summarize=False))
    return status + store.build_context(query, doc, budget=20000)
