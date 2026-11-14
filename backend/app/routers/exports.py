import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import create_download_token, get_current_user, verify_download_token
from app.config import get_settings
from app.database import get_db
from app.models import ExportArtifact, Job, PageResult, User
from app.services.document_model import DocumentModel, PageModel, TextBlock
from app.services.exporters.export_service import generate_export_bytes
from app.storage import storage

router = APIRouter(tags=["Exports & Downloads"])
settings = get_settings()


class CreateExportRequest(BaseModel):
    export_format: str = Field(pattern="^(txt|md|docx|pdf|searchable_pdf|html|json|csv|xlsx|png|jpg|jpeg|webp)$")
    quality_preset: str = Field(default="high", pattern="^(original|high|balanced|smallest|custom)$")
    options: Dict[str, Any] = Field(default_factory=dict)


class BatchZipRequest(BaseModel):
    job_ids: List[str]
    export_format: str = "txt"
    quality_preset: str = "high"


def _build_reviewed_document_model(job: Job, db: Session) -> DocumentModel:
    pages = (
        db.query(PageResult)
        .filter(PageResult.job_id == job.id)
        .order_by(PageResult.page_number.asc())
        .all()
    )
    page_models: List[PageModel] = []
    for p in pages:
        pm = PageModel(**(p.model_json or {"page_number": p.page_number}))
        # Apply user edits while preserving raw OCR in original model
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
                        text=para,
                        raw_ocr_text=orig_blk.raw_ocr_text if orig_blk else None,
                        language=pm.language_detected,
                        extraction_method="user_edited",
                        confidence=orig_blk.confidence if orig_blk else pm.confidence_avg,
                        bbox=orig_blk.bbox if orig_blk else None,
                        source_ref=orig_blk.source_ref if orig_blk else None,
                    )
                )
            pm.blocks = new_blocks
        page_models.append(pm)

    return DocumentModel(
        job_id=job.id,
        source_filename=job.original_filename,
        format_type=job.format_type,
        conversion_mode=(job.settings_json or {}).get("conversion_mode", "clean_editable"),
        pages=page_models,
        metadata=job.metadata_json or {},
    )


def _serialize_artifact(art: ExportArtifact) -> Dict[str, Any]:
    return {
        "artifact_id": art.id,
        "job_id": art.job_id,
        "export_format": art.export_format,
        "quality_preset": art.quality_preset,
        "file_size_bytes": art.file_size_bytes,
        "uncompressed_size_bytes": art.uncompressed_size_bytes,
        "savings_percent": art.savings_percent,
        "warnings": art.warnings_json or [],
        "status": art.status,
        "created_at": art.created_at.isoformat() if art.created_at else None,
    }


@router.post("/jobs/{job_id}/exports", status_code=status.HTTP_201_CREATED)
def create_export(
    job_id: str,
    payload: CreateExportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job must be completed before exporting")

    doc_model = _build_reviewed_document_model(job, db)
    try:
        data_bytes, ext, warnings, baseline_bytes, savings_pct = generate_export_bytes(
            doc_model, payload.export_format, payload.quality_preset, payload.options
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not data_bytes:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Generated export was empty")

    stem = Path(job.original_filename).stem
    artifact_id = f"{payload.export_format}_{payload.quality_preset}_{int(datetime.now(timezone.utc).timestamp())}"
    storage_key = f"jobs/{job.id}/exports/{stem}_{artifact_id}.{ext}"
    storage.save_bytes(storage_key, data_bytes)

    art = ExportArtifact(
        job_id=job.id,
        user_id=current_user.id,
        export_format=payload.export_format,
        quality_preset=payload.quality_preset,
        compression_options_json=payload.options,
        storage_key=storage_key,
        file_size_bytes=len(data_bytes),
        uncompressed_size_bytes=baseline_bytes,
        savings_percent=savings_pct,
        warnings_json=warnings,
        status="completed",
        expires_at=datetime.now(timezone.utc) + timedelta(days=current_user.retention_days),
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    return _serialize_artifact(art)


@router.get("/jobs/{job_id}/exports")
def list_exports(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    arts = db.query(ExportArtifact).filter(ExportArtifact.job_id == job.id).order_by(ExportArtifact.created_at.desc()).all()
    return {"items": [_serialize_artifact(a) for a in arts]}


@router.get("/jobs/{job_id}/exports/{artifact_id}/link")
def get_export_download_link(
    job_id: str,
    artifact_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    art = (
        db.query(ExportArtifact)
        .filter(ExportArtifact.id == artifact_id, ExportArtifact.job_id == job_id, ExportArtifact.user_id == current_user.id)
        .first()
    )
    if not art:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export artifact not found")
    if storage.is_s3():
        url = storage.presign_download_url(
            art.storage_key, Path(art.storage_key).name, settings.DOWNLOAD_TOKEN_EXPIRE_SECONDS
        )
        return {"download_url": url, "expires_in_seconds": settings.DOWNLOAD_TOKEN_EXPIRE_SECONDS, "direct_storage": True}
    token = create_download_token(current_user.id, "export", art.id)
    return {"download_url": f"/api/v1/downloads/{token}", "expires_in_seconds": settings.DOWNLOAD_TOKEN_EXPIRE_SECONDS, "direct_storage": False}


@router.get("/jobs/{job_id}/original/link")
def get_original_download_link(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if storage.is_s3():
        url = storage.presign_download_url(
            job.original_storage_key, job.original_filename, settings.DOWNLOAD_TOKEN_EXPIRE_SECONDS
        )
        return {"download_url": url, "expires_in_seconds": settings.DOWNLOAD_TOKEN_EXPIRE_SECONDS, "direct_storage": True}
    token = create_download_token(current_user.id, "original", job.id)
    return {"download_url": f"/api/v1/downloads/{token}", "expires_in_seconds": settings.DOWNLOAD_TOKEN_EXPIRE_SECONDS, "direct_storage": False}


@router.post("/jobs/batch/zip")
def create_batch_zip(
    payload: BatchZipRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not payload.job_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No job IDs provided")
    jobs = (
        db.query(Job)
        .filter(Job.id.in_(payload.job_ids), Job.user_id == current_user.id, Job.status == "completed")
        .all()
    )
    if not jobs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No completed jobs found for batch ZIP")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for job in jobs:
            doc_model = _build_reviewed_document_model(job, db)
            data_bytes, ext, _, _, _ = generate_export_bytes(doc_model, payload.export_format, payload.quality_preset, {})
            stem = Path(job.original_filename).stem
            zf.writestr(f"{stem}_{job.id[:6]}.{ext}", data_bytes)

    zip_bytes = buf.getvalue()
    key = f"batches/{current_user.id}/batch_{int(datetime.now(timezone.utc).timestamp())}.zip"
    storage.save_bytes(key, zip_bytes)

    art = ExportArtifact(
        job_id=jobs[0].id,
        user_id=current_user.id,
        export_format="zip",
        quality_preset=payload.quality_preset,
        storage_key=key,
        file_size_bytes=len(zip_bytes),
        uncompressed_size_bytes=len(zip_bytes),
        savings_percent=0.0,
        warnings_json=[],
        status="completed",
    )
    db.add(art)
    db.commit()
    db.refresh(art)
    if storage.is_s3():
        dl_url = storage.presign_download_url(key, Path(key).name, settings.DOWNLOAD_TOKEN_EXPIRE_SECONDS)
    else:
        token = create_download_token(current_user.id, "export", art.id)
        dl_url = f"/api/v1/downloads/{token}"
    return {
        "artifact_id": art.id,
        "file_size_bytes": len(zip_bytes),
        "download_url": dl_url,
        "direct_storage": storage.is_s3(),
    }


@router.get("/downloads/{token}")
def download_authorized_file(token: str, db: Session = Depends(get_db)):
    payload = verify_download_token(token)
    user_id = payload.get("sub")
    rtype = payload.get("rtype")
    rid = payload.get("rid")

    if rtype == "original":
        job = db.query(Job).filter(Job.id == rid, Job.user_id == user_id).first()
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Original file not found")
        path = storage.get_local_file(job.original_storage_key)
        return FileResponse(path, filename=job.original_filename)

    if rtype == "export":
        art = db.query(ExportArtifact).filter(ExportArtifact.id == rid, ExportArtifact.user_id == user_id).first()
        if not art:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export file not found")
        path = storage.get_local_file(art.storage_key)
        return FileResponse(path, filename=Path(art.storage_key).name)

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid download resource")
