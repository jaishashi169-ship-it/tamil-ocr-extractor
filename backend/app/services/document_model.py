from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class SourceReference(BaseModel):
    filename: str
    page_number: int
    layer_name: Optional[str] = None
    block_id: Optional[str] = None
    bbox: Optional[BoundingBox] = None


class TextBlock(BaseModel):
    id: str
    block_type: Literal["heading", "paragraph", "list_item", "table_cell", "caption", "header_footer", "verse"] = "paragraph"
    heading_level: Optional[int] = None
    text: str
    raw_ocr_text: Optional[str] = None
    language: str = "unknown"  # tam | eng | tam+eng | unknown
    extraction_method: Literal["direct", "ocr", "psd_text_layer", "user_edited", "gemini_vision"] = "direct"
    confidence: Optional[float] = None  # 0.0 to 100.0
    bbox: Optional[BoundingBox] = None
    font_name: Optional[str] = None
    font_size: Optional[float] = None
    source_ref: Optional[SourceReference] = None


class TableModel(BaseModel):
    id: str
    rows: List[List[str]]
    has_header: bool = True
    uncertain_boundaries: bool = False
    source_ref: Optional[SourceReference] = None


class ImageAsset(BaseModel):
    id: str
    filename: str
    storage_key: str
    width: int
    height: int
    mime_type: str = "image/png"
    caption: Optional[str] = None
    source_ref: Optional[SourceReference] = None


class PageModel(BaseModel):
    page_number: int
    width: Optional[float] = None
    height: Optional[float] = None
    source_type: str = "unknown"  # pdf_text | pdf_scanned | pdf_mixed | image | psd | docx | text
    extraction_method: Literal["direct", "ocr", "hybrid", "gemini_vision"] = "direct"
    language_detected: str = "unknown"
    confidence_avg: Optional[float] = None
    blocks: List[TextBlock] = Field(default_factory=list)
    tables: List[TableModel] = Field(default_factory=list)
    images: List[ImageAsset] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_plain_text(self) -> str:
        parts: List[str] = []
        for b in self.blocks:
            if b.text.strip():
                parts.append(b.text.strip())
        for t in self.tables:
            for row in t.rows:
                parts.append(" | ".join(cell.strip() for cell in row))
        return "\n\n".join(parts)


class DocumentModel(BaseModel):
    job_id: str
    source_filename: str
    format_type: str
    conversion_mode: Literal["clean_editable", "layout_preserved"] = "clean_editable"
    pages: List[PageModel] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


def build_reviewed_document_model(job: Any, db: Any) -> DocumentModel:
    """Reconstruct a DocumentModel from persisted PageResult rows, applying user edits."""
    from app.models import PageResult
    from app.services.tamil_utils import normalize_unicode

    pages = (
        db.query(PageResult)
        .filter(PageResult.job_id == job.id)
        .order_by(PageResult.page_number.asc())
        .all()
    )
    page_models: List[PageModel] = []
    for p in pages:
        pm = PageModel(**(p.model_json or {"page_number": p.page_number}))
        if p.edited_text is not None and p.edited_text != p.raw_text:
            edited_paras = [para.strip() for para in p.edited_text.split("\n\n") if para.strip()]
            new_blocks: List[TextBlock] = []
            for idx, para in enumerate(edited_paras):
                orig_blk = pm.blocks[idx] if idx < len(pm.blocks) else None
                new_blocks.append(
                    TextBlock(
                        id=orig_blk.id if orig_blk else f"edited-{p.page_number}-{idx}",
                        block_type=orig_blk.block_type if orig_blk else "paragraph",
                        heading_level=orig_blk.heading_level if orig_blk else None,
                        text=normalize_unicode(para),
                        raw_ocr_text=orig_blk.raw_ocr_text if orig_blk else None,
                        language=pm.language_detected,
                        extraction_method="user_edited",
                        confidence=orig_blk.confidence if orig_blk else pm.confidence_avg,
                        bbox=orig_blk.bbox if orig_blk else None,
                        source_ref=orig_blk.source_ref if orig_blk else None,
                    )
                )
            pm.blocks = new_blocks
        else:
            for blk in pm.blocks:
                if blk.text:
                    blk.text = normalize_unicode(blk.text)
        for tbl in pm.tables:
            tbl.rows = [
                [normalize_unicode(cell) if isinstance(cell, str) else normalize_unicode(getattr(cell, "text", str(cell))) for cell in row]
                for row in tbl.rows
            ]
        page_models.append(pm)

    return DocumentModel(
        job_id=job.id,
        source_filename=job.original_filename,
        format_type=job.format_type,
        conversion_mode=(job.settings_json or {}).get("conversion_mode", "clean_editable"),
        pages=page_models,
        metadata=job.metadata_json or {},
    )
