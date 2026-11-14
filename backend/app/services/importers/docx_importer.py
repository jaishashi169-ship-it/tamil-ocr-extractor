import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import List

from docx import Document as DocxDocument

from app.config import get_settings
from app.security import validate_docx_archive_safety
from app.services.document_model import (
    DocumentModel,
    PageModel,
    SourceReference,
    TableModel,
    TextBlock,
)
from app.services.importers import CorruptFileError, ProgressCallback
from app.services.tamil_utils import detect_script_language, normalize_unicode

settings = get_settings()


def convert_legacy_doc_to_docx(doc_path: Path, out_dir: Path) -> Path:
    """Convert legacy .doc to .docx using isolated LibreOffice headless subprocess."""
    if not settings.ENABLE_ISOLATED_LIBREOFFICE:
        raise ValueError("Legacy .doc conversion worker is disabled on this server.")
    cmd = [
        settings.LIBREOFFICE_CMD,
        "--headless",
        "--norestore",
        "--invisible",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        "docx",
        "--outdir",
        str(out_dir),
        str(doc_path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise ValueError("LibreOffice (soffice) is not installed for legacy .doc conversion.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Legacy .doc conversion timed out.") from exc

    if res.returncode != 0:
        raise CorruptFileError(f"Legacy .doc conversion failed: {res.stderr.strip() or 'unknown error'}")

    converted = out_dir / f"{doc_path.stem}.docx"
    if not converted.exists():
        raise CorruptFileError("Legacy .doc conversion produced no output.")
    return converted


def import_docx(
    file_path: Path,
    job_id: str,
    filename: str,
    is_legacy_doc: bool = False,
    progress_cb: ProgressCallback = None,
) -> DocumentModel:
    if progress_cb:
        progress_cb(1, 1, "Extracting Word document")

    if is_legacy_doc:
        with tempfile.TemporaryDirectory() as tmpdir:
            converted_path = convert_legacy_doc_to_docx(file_path, Path(tmpdir))
            return _parse_docx_file(converted_path, job_id, filename, original_format="doc")
    return _parse_docx_file(file_path, job_id, filename, original_format="docx")


def _parse_docx_file(file_path: Path, job_id: str, filename: str, original_format: str) -> DocumentModel:
    try:
        validate_docx_archive_safety(file_path)
        doc = DocxDocument(str(file_path))
    except Exception as exc:
        raise CorruptFileError(f"Unable to open Word document '{filename}': {exc}") from exc

    blocks: List[TextBlock] = []
    for para in doc.paragraphs:
        txt = normalize_unicode(para.text.strip())
        if not txt:
            continue
        style_name = (para.style.name if para.style else "") or ""
        block_type = "paragraph"
        heading_level = None
        if style_name.lower().startswith("heading"):
            block_type = "heading"
            try:
                heading_level = int(style_name.split()[-1])
            except Exception:
                heading_level = 2
        elif "list" in style_name.lower():
            block_type = "list_item"

        font_name = None
        for run in para.runs:
            if run.font and run.font.name:
                font_name = run.font.name
                break

        bid = str(uuid.uuid4())
        blocks.append(
            TextBlock(
                id=bid,
                block_type=block_type,  # type: ignore[arg-type]
                heading_level=heading_level,
                text=txt,
                language=detect_script_language(txt),
                extraction_method="direct",
                confidence=100.0,
                font_name=font_name,
                source_ref=SourceReference(filename=filename, page_number=1, block_id=bid),
            )
        )

    tables: List[TableModel] = []
    for tbl in doc.tables:
        rows = []
        for row in tbl.rows:
            rows.append([normalize_unicode(cell.text.strip()) for cell in row.cells])
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

    combined = "\n".join(b.text for b in blocks)
    page = PageModel(
        page_number=1,
        source_type=original_format,
        extraction_method="direct",
        language_detected=detect_script_language(combined),
        confidence_avg=100.0,
        blocks=blocks,
        tables=tables,
    )

    return DocumentModel(
        job_id=job_id,
        source_filename=filename,
        format_type=original_format,
        pages=[page],
        metadata={"total_pages": 1, "paragraph_count": len(blocks), "table_count": len(tables)},
    )
