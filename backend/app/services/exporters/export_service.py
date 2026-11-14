from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.services.document_model import DocumentModel
from app.services.exporters.office_and_pdf import (
    export_docx,
    export_image_preview,
    export_reflowed_pdf,
    export_searchable_pdf,
)
from app.services.exporters.text_and_structured import (
    export_csv,
    export_html,
    export_json,
    export_markdown,
    export_txt,
    wrap_in_zip,
)


def generate_export_bytes(
    doc: DocumentModel,
    export_format: str,
    quality_preset: str = "high",
    options: Dict[str, Any] | None = None,
) -> Tuple[bytes, str, List[str], int, float]:
    """
    Generate export bytes from DocumentModel.
    Returns (data_bytes, filename_ext, warnings, uncompressed_baseline_bytes, savings_percent).
    """
    opts = options or {}
    fmt = export_format.lower().strip()
    warnings: List[str] = []

    if fmt == "txt":
        data, w = export_txt(doc, opts)
        ext = "txt"
    elif fmt == "md":
        data, w = export_markdown(doc, opts)
        ext = "md"
    elif fmt == "html":
        data, w = export_html(doc, opts)
        ext = "html"
    elif fmt == "json":
        data, w = export_json(doc, opts)
        ext = "json"
    elif fmt == "csv":
        data, w = export_csv(doc, opts)
        ext = "csv"
    elif fmt == "xlsx":
        data, w = export_xlsx(doc, opts) if "export_xlsx" in globals() else _export_xlsx_proxy(doc, opts)
        ext = "xlsx"
    elif fmt == "docx":
        data, w = export_docx(doc, opts)
        ext = "docx"
    elif fmt == "pdf":
        data, w = export_reflowed_pdf(doc, opts)
        ext = "pdf"
    elif fmt == "searchable_pdf":
        if quality_preset == "smallest":
            cand_lossy, w_lossy = export_searchable_pdf(doc, "smallest", opts)
            cand_lossless, w_lossless = export_searchable_pdf(doc, "high", opts)
            if len(cand_lossless) < len(cand_lossy):
                data, w = cand_lossless, w_lossless + [
                    "Smallest-file selection chose lossless PNG encoding because it produced a smaller valid PDF than lossy JPEG for this content."
                ]
            else:
                data, w = cand_lossy, w_lossy
        else:
            data, w = export_searchable_pdf(doc, quality_preset, opts)
        ext = "pdf"
    elif fmt in {"png", "jpg", "jpeg", "webp"}:
        data, w = export_image_preview(doc, fmt, quality_preset, opts)
        ext = "jpg" if fmt == "jpeg" else fmt
    else:
        raise ValueError(f"Unsupported export format '{export_format}'.")

    warnings.extend(w)

    # Compute uncompressed/high-quality baseline for honest savings comparison
    baseline_bytes = len(data)
    if quality_preset in {"balanced", "smallest", "custom"} and fmt in {"searchable_pdf", "jpg", "jpeg", "webp"}:
        try:
            if fmt == "searchable_pdf":
                base_data, _ = export_searchable_pdf(doc, "high", {})
            else:
                base_data, _ = export_image_preview(doc, "png", "high", {})
            baseline_bytes = len(base_data)
        except Exception:
            baseline_bytes = len(data)

    # Optional ZIP wrapper for text/structured formats when requested or smallest preset
    if opts.get("zip_compress") and fmt in {"txt", "md", "json", "csv", "html"}:
        stem = Path(doc.source_filename).stem
        baseline_bytes = len(data)
        data = wrap_in_zip(f"{stem}.{ext}", data)
        ext = "zip"
        warnings.append("Packaged output inside a ZIP archive without altering text encoding.")

    actual_bytes = len(data)
    savings_pct = 0.0
    if baseline_bytes > 0:
        savings_pct = round(((baseline_bytes - actual_bytes) / baseline_bytes) * 100.0, 2)
        if savings_pct < 0:
            warnings.append(
                f"Note: compressed output ({actual_bytes:,} bytes) is larger than the baseline ({baseline_bytes:,} bytes). Consider downloading the uncompressed version."
            )

    return data, ext, warnings, baseline_bytes, savings_pct


def _export_xlsx_proxy(doc: DocumentModel, opts: Dict[str, Any]) -> Tuple[bytes, List[str]]:
    from app.services.exporters.text_and_structured import export_xlsx
    return export_xlsx(doc, opts)
