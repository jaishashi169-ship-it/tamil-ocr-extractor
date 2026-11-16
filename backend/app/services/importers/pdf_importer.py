import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
from PIL import Image

from app.config import get_settings
from app.services.document_model import (
    BoundingBox,
    DocumentModel,
    PageModel,
    SourceReference,
    TableModel,
    TextBlock,
)
from app.services.importers import CorruptFileError, PasswordRequiredError, ProgressCallback
from app.services.ocr_engine import OCROptions, run_ocr_on_image
from app.services.tamil_utils import detect_script_language, normalize_unicode
from app.storage import storage

settings = get_settings()


def import_pdf(
    file_path: Path,
    job_id: str,
    filename: str,
    ocr_opts: OCROptions,
    password: Optional[str] = None,
    progress_cb: ProgressCallback = None,
    page_range: Optional[List[int]] = None,
) -> DocumentModel:
    try:
        doc = fitz.open(str(file_path))
    except Exception as exc:
        raise CorruptFileError(f"Unable to open PDF '{filename}': {exc}") from exc

    try:
        if doc.needs_pass:
            if not password or not doc.authenticate(password):
                raise PasswordRequiredError("This PDF is password-protected. Please provide the document password.")

        total_pages = len(doc)
        if total_pages == 0:
            raise CorruptFileError("PDF has 0 pages.")
        if total_pages > settings.MAX_PDF_PAGES:
            raise ValueError(f"PDF has {total_pages} pages, exceeding the limit of {settings.MAX_PDF_PAGES} pages.")

        pages: List[PageModel] = []
        doc_warnings: List[str] = []

        for idx in range(total_pages):
            page_num = idx + 1
            if page_range and page_num not in page_range:
                continue

            if progress_cb:
                progress_cb(page_num, total_pages, f"Processing PDF page {page_num} of {total_pages}")

            page = doc.load_page(idx)
            rect = page.rect

            # Render preview & thumbnail for Review UI
            preview_pix = page.get_pixmap(dpi=150)
            preview_key = f"jobs/{job_id}/pages/page_{page_num:04d}_preview.png"
            storage.save_bytes(preview_key, preview_pix.tobytes("png"))

            thumb_pix = page.get_pixmap(dpi=48)
            thumb_key = f"jobs/{job_id}/pages/page_{page_num:04d}_thumb.png"
            storage.save_bytes(thumb_key, thumb_pix.tobytes("png"))

            # Evaluate embedded text on this page
            raw_dict = page.get_text("dict")
            embedded_blocks: List[TextBlock] = []
            embedded_char_count = 0

            for b in raw_dict.get("blocks", []):
                if b.get("type") != 0:
                    continue
                lines_text: List[str] = []
                font_name: Optional[str] = None
                font_size: Optional[float] = None
                for ln in b.get("lines", []):
                    spans_text = []
                    for sp in ln.get("spans", []):
                        txt = sp.get("text", "")
                        if txt:
                            spans_text.append(txt)
                            if font_name is None:
                                font_name = sp.get("font")
                                font_size = sp.get("size")
                    if spans_text:
                        lines_text.append("".join(spans_text))
                block_text = normalize_unicode("\n".join(lines_text).strip())
                if block_text:
                    embedded_char_count += len(block_text.replace(" ", ""))
                    bx0, by0, bx1, by1 = b.get("bbox", (0, 0, 0, 0))
                    bbox = BoundingBox(x=bx0, y=by0, width=max(1.0, bx1 - bx0), height=max(1.0, by1 - by0))
                    bid = str(uuid.uuid4())
                    is_heading = bool(font_size and font_size >= 15.0 and len(block_text) < 120)
                    embedded_blocks.append(
                        TextBlock(
                            id=bid,
                            block_type="heading" if is_heading else "paragraph",
                            heading_level=2 if is_heading else None,
                            text=block_text,
                            language=detect_script_language(block_text),
                            extraction_method="direct",
                            confidence=100.0,
                            bbox=bbox,
                            font_name=font_name,
                            font_size=font_size,
                            source_ref=SourceReference(
                                filename=filename,
                                page_number=page_num,
                                block_id=bid,
                                bbox=bbox,
                            ),
                        )
                    )

            # Detect tables via PyMuPDF find_tables if available
            page_tables: List[TableModel] = []
            try:
                tab_finder = page.find_tables()
                for t in tab_finder.tables:
                    rows = [[normalize_unicode(str(c or "")) for c in row] for row in t.extract()]
                    if rows:
                        tid = str(uuid.uuid4())
                        page_tables.append(
                            TableModel(
                                id=tid,
                                rows=rows,
                                has_header=True,
                                source_ref=SourceReference(filename=filename, page_number=page_num, block_id=tid),
                            )
                        )
            except Exception:
                pass

            page_warnings: List[str] = []

            # Check if embedded text appears to be legacy non-Unicode font gibberish (e.g. Bamini, TAB, PageMaker)
            combined_embedded = " ".join(b.text for b in embedded_blocks)
            is_legacy_gibberish = False
            if embedded_char_count >= 25:
                import re

                bamini_markers = ["jkpo;", "vd;id", "ghh;it", "epiy", "nkhop", "vz;", "jfty;", "murhiz", "tpsf;fk;"]
                lower_emb = combined_embedded.lower()
                has_bamini_words = any(m in lower_emb for m in bamini_markers)
                has_dense_semicolons = len(re.findall(r"[a-zA-Z];[a-zA-Z]", combined_embedded)) >= 3
                if has_bamini_words or has_dense_semicolons:
                    is_legacy_gibberish = True
                    page_warnings.append(
                        "Detected legacy non-Unicode font encoding in PDF; applying visual OCR for authentic Unicode reproduction."
                    )

            # Determine whether to run visual OCR on this page
            should_run_ocr = (
                embedded_char_count < 25
                or ocr_opts.force_ocr
                or ocr_opts.document_type == "handwritten"
                or is_legacy_gibberish
            )

            if not should_run_ocr:
                extraction_method = "direct"
                source_type = "pdf_text"
                final_blocks = embedded_blocks
                conf_avg = 100.0
            else:
                ocr_pix = page.get_pixmap(dpi=250)
                ocr_img_key = f"jobs/{job_id}/tmp/page_{page_num:04d}_ocr.png"
                storage.save_bytes(ocr_img_key, ocr_pix.tobytes("png"))
                ocr_img_path = storage.get_local_file(ocr_img_key)
                try:
                    ocr_blocks, conf_avg, ocr_warns = run_ocr_on_image(ocr_img_path, filename, page_num, ocr_opts)
                    page_warnings.extend(ocr_warns)
                finally:
                    storage.delete_key(ocr_img_key)

                if embedded_blocks and not ocr_blocks and not is_legacy_gibberish:
                    final_blocks = embedded_blocks
                    extraction_method = "direct"
                    source_type = "pdf_text"
                else:
                    final_blocks = ocr_blocks
                    first_method = final_blocks[0].extraction_method if final_blocks else "ocr"
                    extraction_method = "gemini_vision" if first_method == "gemini_vision" else "ocr"
                    source_type = "pdf_scanned" if embedded_char_count < 25 else "pdf_mixed"

            combined_text = "\n".join(b.text for b in final_blocks)
            lang_detected = detect_script_language(combined_text)

            pages.append(
                PageModel(
                    page_number=page_num,
                    width=float(rect.width),
                    height=float(rect.height),
                    source_type=source_type,
                    extraction_method=extraction_method,
                    language_detected=lang_detected,
                    confidence_avg=conf_avg,
                    blocks=final_blocks,
                    tables=page_tables,
                    warnings=page_warnings,
                    metadata={"preview_key": preview_key, "thumbnail_key": thumb_key},
                )
            )

        return DocumentModel(
            job_id=job_id,
            source_filename=filename,
            format_type="pdf",
            pages=pages,
            warnings=doc_warnings,
            metadata={"total_pages": total_pages, "is_encrypted": doc.is_encrypted},
        )
    finally:
        doc.close()
