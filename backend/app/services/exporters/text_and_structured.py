import csv
import html
import io
import json
import zipfile
from typing import Any, Dict, List, Tuple

import openpyxl

from app.security import sanitize_html
from app.services.document_model import DocumentModel
from app.services.tamil_utils import normalize_unicode


def export_txt(doc: DocumentModel, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    sep = options.get("page_separator", "\n\n---\n\n")
    include_labels = options.get("include_page_labels", True)
    parts: List[str] = []
    for page in doc.pages:
        body = page.to_plain_text()
        if include_labels:
            parts.append(f"[Page {page.page_number}]\n{body}")
        else:
            parts.append(body)
    content = normalize_unicode(sep.join(parts))
    return content.encode("utf-8"), []


def _escape_md(text: str) -> str:
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("*", "\\*").replace("_", "\\_")


def export_markdown(doc: DocumentModel, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    lines: List[str] = [f"# {_escape_md(doc.source_filename)}", ""]
    for page in doc.pages:
        if len(doc.pages) > 1:
            lines.append(f"## Page {page.page_number}")
            lines.append("")
        for blk in page.blocks:
            txt = normalize_unicode(blk.text.strip())
            if not txt:
                continue
            if blk.block_type == "heading":
                lvl = min(6, max(1, blk.heading_level or 2))
                lines.append(f"{'#' * lvl} {txt}")
            elif blk.block_type == "list_item":
                lines.append(f"- {txt}")
            else:
                lines.append(txt)
            lines.append("")
        for tbl in page.tables:
            if not tbl.rows or len(tbl.rows) < 2:
                continue
            non_empty_header = [c for c in tbl.rows[0] if c.strip()]
            if len(non_empty_header) < 2:
                continue
            header = tbl.rows[0]
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in header) + " |")
            lines.append("| " + " | ".join("---" for _ in header) + " |")
            for row in tbl.rows[1:]:
                padded = row + [""] * max(0, len(header) - len(row))
                lines.append("| " + " | ".join(c.replace("|", "\\|") for c in padded[: len(header)]) + " |")
            lines.append("")
    return "\n".join(lines).encode("utf-8"), []


def export_html(doc: DocumentModel, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    body_parts: List[str] = []
    for page in doc.pages:
        body_parts.append(f'<section class="page" data-page="{page.page_number}">')
        if len(doc.pages) > 1:
            body_parts.append(f"<h2>Page {page.page_number}</h2>")
        for blk in page.blocks:
            esc = html.escape(normalize_unicode(blk.text.strip())).replace("\n", "<br>")
            if not esc:
                continue
            if blk.block_type == "heading":
                lvl = min(6, max(1, blk.heading_level or 2))
                body_parts.append(f"<h{lvl}>{esc}</h{lvl}>")
            elif blk.block_type == "list_item":
                body_parts.append(f"<ul><li>{esc}</li></ul>")
            else:
                body_parts.append(f"<p>{esc}</p>")
        for tbl in page.tables:
            body_parts.append("<table>")
            for r_idx, row in enumerate(tbl.rows):
                tag = "th" if r_idx == 0 and tbl.has_header else "td"
                cells = "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in row)
                body_parts.append(f"<tr>{cells}</tr>")
            body_parts.append("</table>")
        body_parts.append("</section>")

    sanitized_body = sanitize_html("\n".join(body_parts))
    full_html = f"""<!DOCTYPE html>
<html lang="ta">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(doc.source_filename)} - Tamil English Document Studio</title>
<style>
  body {{ font-family: 'Noto Sans', 'Noto Sans Tamil', system-ui, sans-serif; line-height: 1.7; max-width: 860px; margin: 2rem auto; padding: 0 1.25rem; color: #0f172a; background: #f8fafc; }}
  .page {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 0.5rem 0.75rem; text-align: left; }}
  th {{ background: #f1f5f9; }}
</style>
</head>
<body>
<h1>{html.escape(doc.source_filename)}</h1>
{sanitized_body}
</body>
</html>"""
    return full_html.encode("utf-8"), []


def export_json(doc: DocumentModel, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    payload = doc.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), []


def export_csv(doc: DocumentModel, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    buf = io.StringIO()
    writer = csv.writer(buf)
    warnings: List[str] = []
    table_count = 0
    for page in doc.pages:
        for tbl in page.tables:
            table_count += 1
            if tbl.uncertain_boundaries:
                warnings.append(f"Table on page {page.page_number} has uncertain row/column boundaries.")
            for row in tbl.rows:
                writer.writerow(row)
            writer.writerow([])
    if table_count == 0:
        warnings.append("No structured tables were detected; exporting text lines as single-column CSV.")
        writer.writerow(["page", "text"])
        for page in doc.pages:
            for blk in page.blocks:
                writer.writerow([page.page_number, blk.text])
    return buf.getvalue().encode("utf-8-sig"), warnings


def export_xlsx(doc: DocumentModel, options: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Extracted Tables"
    warnings: List[str] = []
    table_count = 0
    for page in doc.pages:
        for tbl in page.tables:
            table_count += 1
            if tbl.uncertain_boundaries:
                warnings.append(f"Table on page {page.page_number} has uncertain row/column boundaries.")
            for row in tbl.rows:
                ws.append(row)
            ws.append([])
    if table_count == 0:
        warnings.append("No structured tables were detected; exporting extracted paragraphs by page.")
        ws.append(["Page", "Method", "Language", "Confidence", "Text"])
        for page in doc.pages:
            for blk in page.blocks:
                ws.append([page.page_number, blk.extraction_method, blk.language, blk.confidence, blk.text])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), warnings


def wrap_in_zip(filename: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(filename, data)
    return buf.getvalue()
