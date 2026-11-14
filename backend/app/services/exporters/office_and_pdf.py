import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from docx import Document as DocxDocument
from docx.shared import Pt
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib import colors

from app.services.document_model import DocumentModel
from app.services.tamil_utils import normalize_unicode
from app.storage import storage

TAMIL_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "C:/Windows/Fonts/Nirmala.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _find_unicode_font() -> Optional[str]:
    for candidate in TAMIL_FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def export_docx(doc: DocumentModel, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    word = DocxDocument()
    normal_style = word.styles["Normal"]
    normal_style.font.name = "Noto Sans Tamil"
    normal_style.font.size = Pt(11)

    include_page_breaks = options.get("page_breaks", True)
    warnings: List[str] = []

    for idx, page in enumerate(doc.pages):
        if idx > 0 and include_page_breaks:
            word.add_page_break()
        for blk in page.blocks:
            txt = normalize_unicode(blk.text.strip())
            if not txt:
                continue
            if blk.block_type == "heading":
                lvl = min(4, max(1, blk.heading_level or 2))
                h = word.add_heading(txt, level=lvl)
                for r in h.runs:
                    r.font.name = "Noto Sans Tamil"
            elif blk.block_type == "list_item":
                p = word.add_paragraph(txt, style="List Bullet")
                for r in p.runs:
                    r.font.name = "Noto Sans Tamil"
            else:
                p = word.add_paragraph(txt)
                for r in p.runs:
                    r.font.name = "Noto Sans Tamil"

        for tbl in page.tables:
            if not tbl.rows:
                continue
            cols = max(len(r) for r in tbl.rows)
            w_tbl = word.add_table(rows=len(tbl.rows), cols=cols)
            w_tbl.style = "Table Grid"
            for r_i, row in enumerate(tbl.rows):
                for c_i in range(cols):
                    val = row[c_i] if c_i < len(row) else ""
                    cell_p = w_tbl.cell(r_i, c_i).paragraphs[0]
                    run = cell_p.add_run(normalize_unicode(val))
                    run.font.name = "Noto Sans Tamil"

    out = io.BytesIO()
    word.save(out)
    return out.getvalue(), warnings


def export_reflowed_pdf(doc: DocumentModel, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    warnings: List[str] = []
    font_path = _find_unicode_font()
    font_name = "Helvetica"
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("StudioUnicode", font_path))
            font_name = "StudioUnicode"
        except Exception:
            warnings.append("Could not register Tamil Unicode TTF font; falling back to default PDF font.")
    else:
        warnings.append("Noto Sans Tamil TTF font not found on host; Tamil glyphs require Noto Sans Tamil installed.")

    buf = io.BytesIO()
    pdf_doc = SimpleDocTemplate(buf, pagesize=A4, title=doc.source_filename)
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle("StudioBody", parent=styles["Normal"], fontName=font_name, fontSize=11, leading=16)
    head_style = ParagraphStyle("StudioHead", parent=styles["Heading2"], fontName=font_name, fontSize=14, leading=18)

    story: List[Any] = []
    for idx, page in enumerate(doc.pages):
        if idx > 0:
            story.append(PageBreak())
        for blk in page.blocks:
            txt = normalize_unicode(blk.text.strip())
            if not txt:
                continue
            safe_txt = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
            st = head_style if blk.block_type == "heading" else body_style
            story.append(Paragraph(safe_txt, st))
            story.append(Spacer(1, 6))
        for tbl in page.tables:
            if not tbl.rows:
                continue
            rt = Table(tbl.rows)
            rt.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("FONTNAME", (0, 0), (-1, -1), font_name),
                        ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ]
                )
            )
            story.append(rt)
            story.append(Spacer(1, 10))

    if not story:
        story.append(Paragraph("No text content extracted.", body_style))

    pdf_doc.build(story)
    return buf.getvalue(), warnings


def export_searchable_pdf(doc: DocumentModel, quality_preset: str, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    """
    Build a Searchable PDF by placing original page preview images with an aligned invisible text layer.
    Supports lossless optimization and optional image downsampling for compression presets.
    """
    warnings: List[str] = [
        "Searchable PDF mode: embeds OCR/extracted text behind page images. Digital signatures from original files are not preserved."
    ]
    out_pdf = fitz.open()
    font_path = _find_unicode_font()

    jpeg_quality = 92
    scale_factor = 1.0
    if quality_preset == "balanced":
        jpeg_quality = 75
        scale_factor = 0.85
        warnings.append("Balanced compression uses lossy JPEG image encoding (quality 75) which slightly reduces image detail.")
    elif quality_preset == "smallest":
        jpeg_quality = 55
        scale_factor = 0.65
        warnings.append("Smallest file preset applies stronger lossy compression (quality 55, downscaled) which visibly reduces image detail.")
    elif quality_preset == "custom":
        jpeg_quality = int(options.get("image_quality", 80))
        scale_factor = float(options.get("scale_factor", 1.0))

    for page in doc.pages:
        preview_key = (page.metadata or {}).get("preview_key")
        w = float(page.width or 595.0)
        h = float(page.height or 842.0)
        pdf_page = out_pdf.new_page(width=w, height=h)

        if preview_key:
            try:
                img_path = storage.get_local_file(preview_key)
                img = Image.open(img_path).convert("RGB")
                if scale_factor < 1.0:
                    nw = max(100, int(img.width * scale_factor))
                    nh = max(100, int(img.height * scale_factor))
                    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
                img_buf = io.BytesIO()
                if quality_preset == "high":
                    img.save(img_buf, format="PNG", optimize=True)
                else:
                    img.save(img_buf, format="JPEG", quality=jpeg_quality, optimize=True)
                pdf_page.insert_image(fitz.Rect(0, 0, w, h), stream=img_buf.getvalue())
            except Exception:
                warnings.append(f"Preview image missing for page {page.page_number}; rendering text only.")

        # Insert searchable text layer (render_mode=3 is invisible searchable text when preview image exists)
        render_mode = 3 if preview_key else 0
        for blk in page.blocks:
            txt = normalize_unicode(blk.text.strip())
            if not txt:
                continue
            bx = blk.bbox.x if blk.bbox else 36.0
            by = (blk.bbox.y + 12.0) if blk.bbox else 50.0
            try:
                if font_path:
                    pdf_page.insert_text(
                        (bx, min(h - 10, max(12, by))),
                        txt,
                        fontsize=10,
                        fontname="StudioUnicode",
                        fontfile=font_path,
                        render_mode=render_mode,
                    )
                else:
                    pdf_page.insert_text(
                        (bx, min(h - 10, max(12, by))),
                        txt,
                        fontsize=10,
                        render_mode=render_mode,
                    )
            except Exception:
                pass

    pdf_bytes = out_pdf.tobytes(deflate=True, garbage=4)
    out_pdf.close()
    return pdf_bytes, warnings


def export_image_preview(doc: DocumentModel, target_format: str, quality_preset: str, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    warnings: List[str] = []
    if not doc.pages:
        raise ValueError("No pages available for image export.")
    first_page = doc.pages[0]
    preview_key = (first_page.metadata or {}).get("preview_key")
    if not preview_key:
        raise ValueError("No visual preview image is available for this document type.")

    img_path = storage.get_local_file(preview_key)
    img = Image.open(img_path).convert("RGB")

    quality = int(options.get("image_quality", 90 if quality_preset == "high" else (75 if quality_preset == "balanced" else 55)))
    if options.get("resize_percent"):
        pct = max(10, min(100, int(options["resize_percent"]))) / 100.0
        img = img.resize((max(1, int(img.width * pct)), max(1, int(img.height * pct))), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    fmt = target_format.lower()
    if fmt == "png":
        img.save(buf, format="PNG", optimize=True)
        warnings.append("PNG export uses lossless optimization. Resize explicitly if smaller dimensions are needed.")
    elif fmt in {"jpg", "jpeg"}:
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        warnings.append(f"Lossy JPEG compression (quality {quality}) trades some visual detail for smaller file size.")
    elif fmt == "webp":
        img.save(buf, format="WEBP", quality=quality, method=6)
        warnings.append(f"Lossy WEBP compression (quality {quality}) trades some visual detail for smaller file size.")
    else:
        raise ValueError(f"Unsupported image export format '{target_format}'.")

    return buf.getvalue(), warnings
