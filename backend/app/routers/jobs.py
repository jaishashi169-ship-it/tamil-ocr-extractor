from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.dispatch import dispatch_celery_or_fallback
from app.models import Draft, Job, UploadSession, User
from app.services.retention import (
    StorageQuotaExceededError,
    commit_storage_reservation,
    release_all_job_storage_and_reservations,
)
from app.storage import storage

router = APIRouter(prefix="/jobs", tags=["Jobs"])
settings = get_settings()


class ExtractionSettings(BaseModel):
    language: str = Field(default="tam+eng", pattern="^(tam|eng|tam\\+eng)$")
    conversion_mode: str = Field(default="clean_editable", pattern="^(clean_editable|layout_preserved)$")
    workflow_action: str = Field(default="extract", pattern="^(extract|convert|compress)$")
    target_output_format: str = "docx"
    quality_preset: str = Field(default="high", pattern="^(original|high|balanced|smallest|custom)$")
    auto_rotate: bool = True
    deskew: bool = True
    denoise: bool = False
    enhance_contrast: bool = True
    multi_column: bool = True


class CreateJobRequest(BaseModel):
    upload_session_id: str
    batch_id: Optional[str] = None
    settings: ExtractionSettings = Field(default_factory=ExtractionSettings)
    password: Optional[str] = None  # Transient only; never stored or logged
    idempotency_key: Optional[str] = None


class RetryJobRequest(BaseModel):
    password: Optional[str] = None


def _dispatch_job(
    background_tasks: BackgroundTasks,
    job: Job,
    db: Session,
    password: Optional[str],
) -> None:
    """
    Dispatch job to the durable Celery queue with bounded Redis timeouts.
    Falls back to FastAPI BackgroundTasks ONLY when APP_ENV != 'production'
    and ALLOW_BACKGROUND_TASK_FALLBACK is explicitly enabled.
    """
    job.dispatch_attempts = (job.dispatch_attempts or 0) + 1

    def _on_dispatched() -> None:
        job.dispatch_status = "dispatched"
        db.commit()

    def _on_fallback() -> None:
        job.dispatch_status = "fallback"
        db.commit()

    def _on_failed() -> None:
        job.status = "failed"
        job.dispatch_status = "failed"
        job.stage_label = "Dispatch unavailable"
        job.error_message = "Durable job queue is unavailable. Please retry shortly."
        db.commit()

    dispatch_celery_or_fallback(
        background_tasks=background_tasks,
        task_name="app.tasks.process_extraction_job",
        args=[job.id, password],
        sync_fn_name="process_extraction_job_sync",
        on_dispatched=_on_dispatched,
        on_fallback=_on_fallback,
        on_failed=_on_failed,
        resource_id=job.id,
    )


def _serialize_job(job: Job) -> Dict[str, Any]:
    return {
        "job_id": job.id,
        "batch_id": job.batch_id,
        "original_filename": job.original_filename,
        "format_type": job.format_type,
        "file_size_bytes": job.file_size_bytes,
        "file_sha256": job.file_sha256,
        "status": job.status,
        "progress_percent": job.progress_percent,
        "stage_label": job.stage_label,
        "page_count": job.page_count,
        "processed_pages": job.processed_pages,
        "settings": job.settings_json,
        "metadata": job.metadata_json,
        "error_message": job.error_message,
        "needs_password": job.needs_password,
        "duration_ms": job.duration_ms,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_job(
    payload: CreateJobRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.idempotency_key:
        existing = (
            db.query(Job)
            .filter(Job.user_id == current_user.id, Job.idempotency_key == payload.idempotency_key)
            .first()
        )
        if existing:
            return _serialize_job(existing)

    is_guest_or_admin = current_user.email.endswith("@studio.local") or current_user.role in ("guest", "admin")
    if is_guest_or_admin:
        session = db.query(UploadSession).filter(UploadSession.id == payload.upload_session_id).first()
        if session and session.user_id != current_user.id:
            session.user_id = current_user.id
            db.commit()
            db.refresh(session)
    else:
        session = (
            db.query(UploadSession)
            .filter(UploadSession.id == payload.upload_session_id, UploadSession.user_id == current_user.id)
            .first()
        )

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload session not found.",
        )

    if session.status != "completed":
        total = session.total_chunks or 1
        received = len(session.received_chunks or [])
        if session.status == "processing" or received >= total:
            try:
                from app.tasks import finalize_upload_sync
                finalize_upload_sync(session.id)
                db.refresh(session)
            except Exception as fe:
                pass

    if not session.storage_key or not session.format_type or session.status != "completed":
        status_info = f" (current status: {session.status})" if session else ""
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload session is not completed or missing validated file{status_info}.",
        )

    if payload.batch_id:
        batch_count = db.query(Job).filter(Job.user_id == current_user.id, Job.batch_id == payload.batch_id).count()
        if batch_count >= settings.MAX_BATCH_FILES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Batch exceeds maximum allowed {settings.MAX_BATCH_FILES} files.",
            )

    expires_at = datetime.now(timezone.utc) + timedelta(days=current_user.retention_days)
    job = Job(
        user_id=current_user.id,
        batch_id=payload.batch_id,
        upload_session_id=session.id,
        original_filename=session.filename,
        format_type=session.format_type,
        file_size_bytes=session.expected_size_bytes,
        file_sha256=session.sha256_hash,
        original_storage_key=session.storage_key,
        status="queued",
        progress_percent=0.0,
        stage_label="Queued",
        settings_json=payload.settings.model_dump(),
        idempotency_key=payload.idempotency_key,
        expires_at=expires_at,
    )
    db.add(job)
    db.flush()

    try:
        commit_storage_reservation(
            db,
            user_id=current_user.id,
            resource_type="upload",
            resource_id=session.id,
            actual_bytes=int(session.expected_size_bytes),
            new_resource_type="original",
            new_resource_id=job.id,
            storage_key=session.storage_key,
            expires_at=expires_at,
            commit=False,
        )
    except StorageQuotaExceededError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc

    db.commit()
    db.refresh(job)

    _dispatch_job(background_tasks, job, db, payload.password)
    return _serialize_job(job)


@router.get("")
def list_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    batch_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Job).filter(Job.user_id == current_user.id)
    if batch_id:
        q = q.filter(Job.batch_id == batch_id)
    total = q.count()
    items = q.order_by(Job.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize_job(j) for j in items],
    }


@router.get("/{job_id}")
def get_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return _serialize_job(job)


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status in {"queued", "running"}:
        job.status = "cancelled"
        job.stage_label = "Cancelled"
        release_all_job_storage_and_reservations(db, job, delete_original=False, commit=False)
        db.commit()
        db.refresh(job)
    return _serialize_job(job)


@router.post("/{job_id}/retry")
def retry_job(
    job_id: str,
    payload: RetryJobRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status == "queued":
        return _serialize_job(job)
    if job.status == "running":
        now_utc = datetime.now(timezone.utc)
        lease_exp = job.lease_expires_at
        if lease_exp is not None and lease_exp.tzinfo is None:
            lease_exp = lease_exp.replace(tzinfo=timezone.utc)
        if lease_exp is not None and lease_exp > now_utc:
            return _serialize_job(job)
    job.status = "queued"
    job.dispatch_status = "pending"
    job.execution_token = None
    job.lease_expires_at = None
    job.progress_percent = 0.0
    job.stage_label = "Queued for retry"
    job.error_message = None
    job.needs_password = False
    db.commit()
    db.refresh(job)
    _dispatch_job(background_tasks, job, db, payload.password)
    return _serialize_job(job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job_now(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete-now action covering originals, generated results, previews, and saved drafts."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    is_guest_or_admin = current_user.email.endswith("@studio.local") or current_user.role in ("guest", "admin")
    if job.user_id != current_user.id and not is_guest_or_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this job")

    release_all_job_storage_and_reservations(db, job, delete_original=True, commit=False)
    db.query(Draft).filter(Draft.job_id == job.id).delete()
    db.delete(job)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
