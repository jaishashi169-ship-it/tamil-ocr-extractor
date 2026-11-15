from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import create_download_token, get_current_user, verify_download_token
from app.config import get_settings
from app.database import get_db
from app.dispatch import dispatch_celery_or_fallback
from app.models import ExportArtifact, Job, User
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


@router.post("/jobs/{job_id}/exports", status_code=status.HTTP_202_ACCEPTED)
def create_export(
    job_id: str,
    payload: CreateExportRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job must be completed before exporting")

    art = ExportArtifact(
        job_id=job.id,
        user_id=current_user.id,
        export_format=payload.export_format,
        quality_preset=payload.quality_preset,
        compression_options_json=payload.options,
        storage_key="",
        file_size_bytes=0,
        warnings_json=[],
        status="processing",
        expires_at=datetime.now(timezone.utc) + timedelta(days=current_user.retention_days),
    )
    db.add(art)
    db.commit()
    db.refresh(art)

    def _on_failed() -> None:
        art.status = "failed"
        db.commit()

    dispatch_celery_or_fallback(
        background_tasks=background_tasks,
        task_name="app.tasks.generate_export",
        args=[art.id, payload.options],
        sync_fn_name="generate_export_sync",
        on_failed=_on_failed,
        resource_id=art.id,
    )
    db.refresh(art)
    return _serialize_artifact(art)


@router.get("/jobs/{job_id}/exports/{artifact_id}")
def get_export_status(
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
    if art.status != "completed" or not art.storage_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Export is not ready (status: {art.status})")
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
    if art.status != "completed" or not art.storage_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Export is not ready (status: {art.status})")
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


@router.post("/jobs/batch/zip", status_code=status.HTTP_202_ACCEPTED)
def create_batch_zip(
    payload: BatchZipRequest,
    background_tasks: BackgroundTasks,
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

    art = ExportArtifact(
        job_id=jobs[0].id,
        user_id=current_user.id,
        export_format="zip",
        quality_preset=payload.quality_preset,
        storage_key="",
        file_size_bytes=0,
        uncompressed_size_bytes=0,
        savings_percent=0.0,
        warnings_json=[],
        status="processing",
    )
    db.add(art)
    db.commit()
    db.refresh(art)

    def _on_failed() -> None:
        art.status = "failed"
        db.commit()

    dispatch_celery_or_fallback(
        background_tasks=background_tasks,
        task_name="app.tasks.generate_batch_zip",
        args=[art.id, current_user.id, payload.job_ids, payload.export_format, payload.quality_preset],
        sync_fn_name="generate_batch_zip_sync",
        on_failed=_on_failed,
        resource_id=art.id,
    )
    db.refresh(art)
    return {
        "artifact_id": art.id,
        "job_id": jobs[0].id,
        "status": art.status,
        "file_size_bytes": art.file_size_bytes,
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
