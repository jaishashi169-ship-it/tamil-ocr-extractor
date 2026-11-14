import os
import re
import zipfile
from pathlib import Path
from typing import Optional, Tuple
import bleach

ALLOWED_FORMATS = {
    "pdf": {"extensions": {".pdf"}, "mime": "application/pdf"},
    "docx": {"extensions": {".docx"}, "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "doc": {"extensions": {".doc"}, "mime": "application/msword"},
    "txt": {"extensions": {".txt"}, "mime": "text/plain"},
    "md": {"extensions": {".md", ".markdown"}, "mime": "text/markdown"},
    "png": {"extensions": {".png"}, "mime": "image/png"},
    "jpg": {"extensions": {".jpg", ".jpeg"}, "mime": "image/jpeg"},
    "webp": {"extensions": {".webp"}, "mime": "image/webp"},
    "tiff": {"extensions": {".tif", ".tiff"}, "mime": "image/tiff"},
    "bmp": {"extensions": {".bmp"}, "mime": "image/bmp"},
    "psd": {"extensions": {".psd"}, "mime": "image/vnd.adobe.photoshop"},
    "html": {"extensions": {".html", ".htm"}, "mime": "text/html"},
    "csv": {"extensions": {".csv"}, "mime": "text/csv"},
    "json": {"extensions": {".json"}, "mime": "application/json"},
}

SAFE_HTML_TAGS = [
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "strong", "em", "b", "i", "u",
    "table", "thead", "tbody", "tr", "th", "td",
    "blockquote", "pre", "code", "hr", "div", "span", "a",
]
SAFE_HTML_ATTRS = {
    "a": ["href", "title"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
}


def sanitize_filename(filename: str) -> str:
    """Prevent path traversal and strip unsafe characters while preserving Unicode."""
    base = os.path.basename(filename.replace("\\", "/"))
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip()
    return cleaned or "document"


def safe_join(root: str | Path, *parts: str) -> Path:
    """Join path components and verify the result remains inside root."""
    root_path = Path(root).resolve()
    target = root_path.joinpath(*parts).resolve()
    if root_path != target and root_path not in target.parents:
        raise ValueError("Path traversal detected")
    return target


def sanitize_html(raw_html: str) -> str:
    """Strip unsafe tags, scripts, and event handlers from HTML."""
    return bleach.clean(
        raw_html,
        tags=SAFE_HTML_TAGS,
        attributes=SAFE_HTML_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    )


def detect_format_by_signature(header_bytes: bytes, filename: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Validate both filename extension and magic-byte content signature.
    Returns (format_type, error_message).
    """
    ext = Path(filename).suffix.lower()
    if not header_bytes:
        return None, "Uploaded file is empty."

    if header_bytes.startswith(b"%PDF-"):
        if ext != ".pdf":
            return None, f"File content is PDF but extension is '{ext}'."
        return "pdf", None

    if header_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        if ext != ".png":
            return None, f"File content is PNG but extension is '{ext}'."
        return "png", None

    if header_bytes.startswith(b"\xff\xd8\xff"):
        if ext not in {".jpg", ".jpeg"}:
            return None, f"File content is JPEG but extension is '{ext}'."
        return "jpg", None

    if header_bytes.startswith(b"RIFF") and len(header_bytes) >= 12 and header_bytes[8:12] == b"WEBP":
        if ext != ".webp":
            return None, f"File content is WEBP but extension is '{ext}'."
        return "webp", None

    if header_bytes.startswith((b"II*\x00", b"MM\x00*")):
        if ext not in {".tif", ".tiff"}:
            return None, f"File content is TIFF but extension is '{ext}'."
        return "tiff", None

    if header_bytes.startswith(b"BM"):
        if ext != ".bmp":
            return None, f"File content is BMP but extension is '{ext}'."
        return "bmp", None

    if header_bytes.startswith(b"8BPS"):
        if ext != ".psd":
            return None, f"File content is PSD but extension is '{ext}'."
        return "psd", None

    if header_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if ext != ".doc":
            return None, f"File content is legacy OLE2/DOC but extension is '{ext}'."
        return "doc", None

    if header_bytes.startswith(b"PK\x03\x04"):
        if ext == ".docx":
            return "docx", None
        return None, f"ZIP-based archive with extension '{ext}' is not a supported DOCX document."

    # Text-based formats: verify no NUL bytes and valid UTF-8/text decoding
    if b"\x00" in header_bytes[:4096]:
        return None, f"Unsupported or corrupt binary file for extension '{ext}'."

    text_map = {
        ".txt": "txt",
        ".md": "md",
        ".markdown": "md",
        ".html": "html",
        ".htm": "html",
        ".csv": "csv",
        ".json": "json",
    }
    if ext in text_map:
        return text_map[ext], None

    return None, f"Unsupported file format '{ext}'."


def validate_docx_archive_safety(file_path: Path, max_uncompressed_bytes: int = 500_000_000, max_entries: int = 5000) -> None:
    """Protect against zip bombs in DOCX archives."""
    if not zipfile.is_zipfile(file_path):
        raise ValueError("Corrupt DOCX archive.")
    total_size = 0
    with zipfile.ZipFile(file_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise ValueError("DOCX archive exceeds maximum allowed internal entries.")
        for info in infos:
            if ".." in info.filename or info.filename.startswith("/"):
                raise ValueError("Unsafe path inside DOCX archive.")
            total_size += info.file_size
            if total_size > max_uncompressed_bytes:
                raise ValueError("DOCX uncompressed content exceeds safety limit (possible zip bomb).")
