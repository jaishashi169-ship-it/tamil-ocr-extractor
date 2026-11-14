import csv
import io
import json
import uuid
from pathlib import Path
from typing import List

from bs4 import BeautifulSoup
from charset_normalizer import from_bytes

from app.security import sanitize_html
from app.services.document_model import (
    DocumentModel,
    PageModel,
    SourceReference,
    TableModel,
    TextBlock,
)
from app.services.importers import CorruptFileError, ProgressCallback
from app.services.tamil_utils import detect_script_language, normalize_unicode


def _decode_text_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        best = from_bytes(raw).best()
        if best is None:
            raise CorruptFileError("Unable to decode text file encoding.")
        return str(best)


def import_structured_or_text(
    file_path: Path,
    job_id: str,
    filename: str,
    format_type: str,
    progress_cb: ProgressCallback = None,
) -> DocumentModel:
    if progress_cb:
        progress_cb(1, 1, f"Reading {format_type.upper()} document")

    raw_bytes = file_path.read_bytes()
    text_content = normalize_unicode(_decode_text_bytes(raw_bytes))

    blocks: List[TextBlock] = []
    tables: List[TableModel] = []
    warnings: List[str] = []

    if format_type == "csv":
        reader = csv.reader(io.StringIO(text_content))
        rows = [[normalize_unicode(c) for c in row] for row in reader]
        if rows:
            tid = str(uuid.uuid4())
            tables.append(
                TableModel(
                    id=tid,
                    rows=rows,
                    has_header=True,
                    source_ref=SourceReference(filename=filename, page_number=1, block_id=tid),
                )
            )
    elif format_type == "json":
        try:
            parsed = json.loads(text_content)
            pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
        except json.JSONDecodeError as exc:
            raise CorruptFileError(f"Invalid JSON file '{filename}': {exc}") from exc
        bid = str(uuid.uuid4())
        blocks.append(
            TextBlock(
                id=bid,
                block_type="paragraph",
                text=pretty,
                language=detect_script_language(pretty),
                extraction_method="direct",
                confidence=100.0,
                source_ref=SourceReference(filename=filename, page_number=1, block_id=bid),
            )
        )
    elif format_type == "html":
        cleaned_html = sanitize_html(text_content)
        soup = BeautifulSoup(cleaned_html, "html.parser")
        for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote"]):
            txt = normalize_unicode(el.get_text(" ", strip=True))
            if not txt:
                continue
            tag = el.name.lower()
            is_h = tag.startswith("h")
            bid = str(uuid.uuid4())
            blocks.append(
                TextBlock(
                    id=bid,
                    block_type="heading" if is_h else ("list_item" if tag == "li" else "paragraph"),
                    heading_level=int(tag[1]) if is_h else None,
                    text=txt,
                    language=detect_script_language(txt),
                    extraction_method="direct",
                    confidence=100.0,
                    source_ref=SourceReference(filename=filename, page_number=1, block_id=bid),
                )
            )
        for tbl in soup.find_all("table"):
            rows = []
            for tr in tbl.find_all("tr"):
                cells = [normalize_unicode(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
                if cells:
                    rows.append(cells)
            if rows:
                tid = str(uuid.uuid4())
                tables.append(
                    TableModel(
                        id=tid,
                        rows=rows,
                        has_header=True,
                        source_ref=SourceReference(filename=filename, page_number=1, block_id=tid),
                    )
                )
        if not blocks and not tables:
            fallback = normalize_unicode(soup.get_text("\n", strip=True))
            if fallback:
                bid = str(uuid.uuid4())
                blocks.append(
                    TextBlock(
                        id=bid,
                        block_type="paragraph",
                        text=fallback,
                        language=detect_script_language(fallback),
                        extraction_method="direct",
                        confidence=100.0,
                        source_ref=SourceReference(filename=filename, page_number=1, block_id=bid),
                    )
                )
    elif format_type == "md":
        for line in text_content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            bid = str(uuid.uuid4())
            if stripped.startswith("#"):
                hashes = len(stripped) - len(stripped.lstrip("#"))
                heading_txt = stripped[hashes:].strip()
                blocks.append(
                    TextBlock(
                        id=bid,
                        block_type="heading",
                        heading_level=min(6, max(1, hashes)),
                        text=heading_txt,
                        language=detect_script_language(heading_txt),
                        extraction_method="direct",
                        confidence=100.0,
                        source_ref=SourceReference(filename=filename, page_number=1, block_id=bid),
                    )
                )
            elif stripped.startswith(("- ", "* ")):
                blocks.append(
                    TextBlock(
                        id=bid,
                        block_type="list_item",
                        text=stripped[2:].strip(),
                        language=detect_script_language(stripped),
                        extraction_method="direct",
                        confidence=100.0,
                        source_ref=SourceReference(filename=filename, page_number=1, block_id=bid),
                    )
                )
            else:
                blocks.append(
                    TextBlock(
                        id=bid,
                        block_type="paragraph",
                        text=stripped,
                        language=detect_script_language(stripped),
                        extraction_method="direct",
                        confidence=100.0,
                        source_ref=SourceReference(filename=filename, page_number=1, block_id=bid),
                    )
                )
    else:
        # TXT
        for para in text_content.split("\n\n"):
            p = para.strip()
            if not p:
                continue
            bid = str(uuid.uuid4())
            blocks.append(
                TextBlock(
                    id=bid,
                    block_type="paragraph",
                    text=p,
                    language=detect_script_language(p),
                    extraction_method="direct",
                    confidence=100.0,
                    source_ref=SourceReference(filename=filename, page_number=1, block_id=bid),
                )
            )

    combined = "\n".join(b.text for b in blocks)
    page = PageModel(
        page_number=1,
        source_type=format_type,
        extraction_method="direct",
        language_detected=detect_script_language(combined),
        confidence_avg=100.0,
        blocks=blocks,
        tables=tables,
        warnings=warnings,
    )

    return DocumentModel(
        job_id=job_id,
        source_filename=filename,
        format_type=format_type,
        pages=[page],
        warnings=warnings,
        metadata={"total_pages": 1},
    )
