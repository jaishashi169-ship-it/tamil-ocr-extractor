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
    block_type: Literal["heading", "paragraph", "list_item", "table_cell", "caption", "header_footer"] = "paragraph"
    heading_level: Optional[int] = None
    text: str
    raw_ocr_text: Optional[str] = None
    language: str = "unknown"  # tam | eng | tam+eng | unknown
    extraction_method: Literal["direct", "ocr", "psd_text_layer", "user_edited"] = "direct"
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
    extraction_method: Literal["direct", "ocr", "hybrid"] = "direct"
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
