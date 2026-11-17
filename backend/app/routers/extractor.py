"""FastAPI router for original language extraction, language discovery, and multiformat exports."""
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import subprocess
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.extraction_kit import (
    LABELS,
    export,
    extract,
    installed_languages,
)

router = APIRouter(tags=["Original Language Extractor"])

FORMATS = {
    "txt": "text/plain",
    "json": "application/json",
    "html": "text/html",
    "md": "text/markdown",
    "epub": "application/epub+zip",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@router.get("/languages")
def languages():
    models = sorted(installed_languages() - {"osd", "equ"})
    return [{"code": m, "label": LABELS.get(m, m)} for m in models]


@router.post("/extract")
def extract_file(
    file: UploadFile = File(...),
    languages: str = Form("tam+eng"),
    mode: str = Form("auto"),
    verify: bool = Form(False),
    encoding: str = Form("auto"),
):
    if mode not in {"auto", "native", "ocr"}:
        raise HTTPException(422, "Unsupported mode")
    if not re.fullmatch(r"[a-zA-Z0-9_/]+(?:\+[a-zA-Z0-9_/]+)*", languages):
        raise HTTPException(422, "Invalid OCR model list")
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / ("source" + Path(file.filename or "").suffix.lower())
        with path.open("wb") as writer:
            while chunk := file.file.read(1024 * 1024):
                if writer.tell() + len(chunk) > 64 * 1024 * 1024:
                    raise HTTPException(413, "Upload exceeds 64 MiB")
                writer.write(chunk)
        try:
            sections = extract(path, languages, mode, encoding, verify)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"ocr_languages": languages, "sections": sections}


class Section(BaseModel):
    section: int = Field(ge=1)
    text: str
    model_config = {"extra": "allow"}


class ExportRequest(BaseModel):
    sections: List[Section]
    format: str


@router.post("/export")
def download(body: ExportRequest):
    if body.format not in FORMATS:
        raise HTTPException(422, "Unsupported export format")
    with TemporaryDirectory() as tmp:
        target = Path(tmp) / ("extracted." + body.format)
        try:
            export([s.model_dump() for s in body.sections], target)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return Response(
            content=target.read_bytes(),
            media_type=FORMATS[body.format],
            headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
        )
