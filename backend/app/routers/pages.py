from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Draft, Job, PageResult, User
from app.services.study.grammar import review_grammar_and_spelling
from app.services.tamil_utils import (
    SUPPORTED_LEGACY_ENCODINGS,
    convert_legacy_tamil,
    detect_repeated_headers_footers,
    normalize_unicode,
    remove_headers_footers_from_text,
)
from app.storage import storage

router = APIRouter(prefix="/jobs", tags=["Pages & Review"])


class SavePageTextRequest(BaseModel):
    edited_text: str
    is_autosave: bool = False
    expected_version: Optional[int] = None


class ReprocessPageRequest(BaseModel):
    language: str = "tam+eng"
    auto_rotate: bool = True
    deskew: bool = True
    denoise: bool = False
    enhance_contrast: bool = True
    multi_column: bool = True
    crop_box: Optional[List[int]] = None  # [x, y, w, h] in pixels


class LegacyTamilRequest(BaseModel):
    source_encoding: str  # bamini | tab
    page_number: Optional[int] = None


class HeaderFooterApplyRequest(BaseModel):
    headers: List[str] = []
    footers: List[str] = []
    remove_page_numbers: bool = True


def _get_authorized_job(job_id: str, user_id: str, db: Session) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == user_id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.get("/{job_id}/pages")
def get_job_pages(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_authorized_job(job_id, current_user.id, db)
    pages = (
        db.query(PageResult)
        .filter(PageResult.job_id == job.id)
        .order_by(PageResult.page_number.asc())
        .all()
    )
    drafts_by_page = {
        d.page_number: d.version
        for d in db.query(Draft).filter(Draft.job_id == job.id, Draft.user_id == current_user.id).all()
    }
    return {
        "job_id": job.id,
        "total_pages": len(pages),
        "pages": [
            {
                "page_number": p.page_number,
                "source_type": p.source_type,
                "extraction_method": p.extraction_method,
                "language_detected": p.language_detected,
                "confidence_avg": p.confidence_avg,
                "raw_text": p.raw_text,
                "edited_text": p.edited_text if p.edited_text is not None else p.raw_text,
                "has_user_edits": p.edited_text is not None and p.edited_text != p.raw_text,
                "draft_version": drafts_by_page.get(p.page_number, 0),
                "has_preview": bool(p.preview_key),
                "has_thumbnail": bool(p.thumbnail_key),
                "model": p.model_json,
            }
            for p in pages
        ],
    }


@router.get("/{job_id}/pages/{page_number}/image")
def get_page_image(
    job_id: str,
    page_number: int,
    request: Request,
    kind: str = Query(default="preview", pattern="^(preview|thumbnail)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_authorized_job(job_id, current_user.id, db)
    page = (
        db.query(PageResult)
        .filter(PageResult.job_id == job.id, PageResult.page_number == page_number)
        .first()
    )
    if not page:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    key = page.preview_key if kind == "preview" else page.thumbnail_key
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No visual preview available for this page")
    try:
        stream_info = storage.open_download_stream(
            key=key,
            filename=Path(key).name,
            media_type="image/png",
            range_header=request.headers.get("range"),
            disposition="inline",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page image not found in storage") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail=str(exc)) from exc
    return StreamingResponse(
        stream_info["iterator"],
        status_code=stream_info["status_code"],
        media_type=stream_info["media_type"],
        headers=stream_info["headers"],
    )


@router.put("/{job_id}/pages/{page_number}")
def save_page_review(
    job_id: str,
    page_number: int,
    payload: SavePageTextRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_authorized_job(job_id, current_user.id, db)
    page = (
        db.query(PageResult)
        .filter(PageResult.job_id == job.id, PageResult.page_number == page_number)
        .first()
    )
    if not page:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")

    # Keep separate Draft record for autosave versioning + optimistic concurrency check
    draft = (
        db.query(Draft)
        .filter(Draft.job_id == job.id, Draft.user_id == current_user.id, Draft.page_number == page_number)
        .first()
    )
    current_version = draft.version if draft else 0
    if payload.expected_version is not None and payload.expected_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Edit conflict: page {page_number} is at version {current_version}, expected {payload.expected_version}.",
        )

    normalized = normalize_unicode(payload.edited_text)
    page.edited_text = normalized

    if draft:
        draft.content_text = normalized
        draft.version += 1
    else:
        draft = Draft(
            job_id=job.id,
            user_id=current_user.id,
            page_number=page_number,
            content_text=normalized,
            version=1,
        )
        db.add(draft)

    db.commit()
    return {
        "job_id": job.id,
        "page_number": page_number,
        "saved": True,
        "is_autosave": payload.is_autosave,
        "draft_version": draft.version,
    }


@router.post("/{job_id}/pages/{page_number}/reprocess")
def reprocess_page_or_region(
    job_id: str,
    page_number: int,
    payload: ReprocessPageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_authorized_job(job_id, current_user.id, db)
    page = (
        db.query(PageResult)
        .filter(PageResult.job_id == job.id, PageResult.page_number == page_number)
        .first()
    )
    if not page or not page.preview_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Page preview image is required for OCR reprocessing")

    crop_tuple = None
    if payload.crop_box and len(payload.crop_box) == 4:
        crop_tuple = (payload.crop_box[0], payload.crop_box[1], payload.crop_box[2], payload.crop_box[3])

    try:
        from app.services.ocr_engine import OCROptions, OCRUnavailableError, run_ocr_on_image
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OCR reprocessing requires the OCR worker environment.",
        ) from exc

    opts = OCROptions(
        language=payload.language,
        auto_rotate=payload.auto_rotate,
        deskew=payload.deskew,
        denoise=payload.denoise,
        enhance_contrast=payload.enhance_contrast,
        multi_column=payload.multi_column,
        crop_box=crop_tuple,
    )
    preview_path = storage.get_local_file(page.preview_key)
    try:
        blocks, conf_avg, warns = run_ocr_on_image(preview_path, job.original_filename, page_number, opts)
    except OCRUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    new_text = "\n\n".join(b.text for b in blocks if b.text.strip())

    # Preserve raw OCR separately if region crop wasn't used, or return region text
    if not crop_tuple:
        page.raw_text = new_text
        page.confidence_avg = conf_avg
        page.extraction_method = "ocr"
        model_dict = dict(page.model_json or {})
        model_dict["blocks"] = [b.model_dump(mode="json") for b in blocks]
        model_dict["confidence_avg"] = conf_avg
        page.model_json = model_dict
        db.commit()

    return {
        "page_number": page_number,
        "is_region_ocr": bool(crop_tuple),
        "text": new_text,
        "confidence_avg": conf_avg,
        "warnings": warns,
        "blocks": [b.model_dump(mode="json") for b in blocks],
    }


@router.post("/{job_id}/pages/{page_number}/grammar-review")
def grammar_review_page(
    job_id: str,
    page_number: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return spelling/grammar suggestions and diff without modifying the saved page."""
    job = _get_authorized_job(job_id, current_user.id, db)
    page = (
        db.query(PageResult)
        .filter(PageResult.job_id == job.id, PageResult.page_number == page_number)
        .first()
    )
    if not page:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    source_text = page.edited_text if page.edited_text is not None else page.raw_text
    return {"page_number": page_number, **review_grammar_and_spelling(source_text)}


@router.post("/{job_id}/headers-footers/preview")
def preview_headers_footers(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_authorized_job(job_id, current_user.id, db)
    pages = db.query(PageResult).filter(PageResult.job_id == job.id).order_by(PageResult.page_number.asc()).all()
    texts = [p.edited_text if p.edited_text is not None else p.raw_text for p in pages]
    return detect_repeated_headers_footers(texts)


@router.post("/{job_id}/headers-footers/apply")
def apply_headers_footers_removal(
    job_id: str,
    payload: HeaderFooterApplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_authorized_job(job_id, current_user.id, db)
    pages = db.query(PageResult).filter(PageResult.job_id == job.id).order_by(PageResult.page_number.asc()).all()
    updated_count = 0
    for p in pages:
        cur = p.edited_text if p.edited_text is not None else p.raw_text
        cleaned = remove_headers_footers_from_text(cur, payload.headers, payload.footers, payload.remove_page_numbers)
        if cleaned != cur:
            p.edited_text = cleaned
            updated_count += 1
    db.commit()
    return {"updated_pages": updated_count}


@router.post("/{job_id}/legacy-tamil/preview")
def preview_legacy_tamil(
    job_id: str,
    payload: LegacyTamilRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.source_encoding.lower() not in SUPPORTED_LEGACY_ENCODINGS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported legacy encoding")
    job = _get_authorized_job(job_id, current_user.id, db)
    q = db.query(PageResult).filter(PageResult.job_id == job.id).order_by(PageResult.page_number.asc())
    if payload.page_number:
        q = q.filter(PageResult.page_number == payload.page_number)
    pages = q.all()
    previews = []
    for p in pages:
        src = p.edited_text if p.edited_text is not None else p.raw_text
        converted, warns = convert_legacy_tamil(src, payload.source_encoding)
        previews.append({
            "page_number": p.page_number,
            "original_preview": src[:500],
            "converted_preview": converted[:500],
            "warnings": warns,
        })
    return {"source_encoding": payload.source_encoding, "previews": previews}


@router.post("/{job_id}/legacy-tamil/apply")
def apply_legacy_tamil(
    job_id: str,
    payload: LegacyTamilRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.source_encoding.lower() not in SUPPORTED_LEGACY_ENCODINGS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported legacy encoding")
    job = _get_authorized_job(job_id, current_user.id, db)
    q = db.query(PageResult).filter(PageResult.job_id == job.id).order_by(PageResult.page_number.asc())
    if payload.page_number:
        q = q.filter(PageResult.page_number == payload.page_number)
    pages = q.all()
    for p in pages:
        src = p.edited_text if p.edited_text is not None else p.raw_text
        converted, _ = convert_legacy_tamil(src, payload.source_encoding)
        p.edited_text = converted
    db.commit()
    return {"converted_pages": len(pages), "source_encoding": payload.source_encoding}
