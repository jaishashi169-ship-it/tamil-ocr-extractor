from datetime import datetime, timedelta, timezone
from typing import Dict
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Draft, ExportArtifact, Job, UploadSession, User
from app.storage import storage


class StorageQuotaExceededError(Exception):
    """Raised when a user's storage quota or the total account storage budget would be exceeded."""


def cleanup_expired_resources(db: Session, stale_upload_hours: int = 6) -> Dict[str, int]:
    """
    Delete expired jobs, expired export artifacts, and stale incomplete upload sessions
    from both private storage and the database.
    """
    now_utc = datetime.now(timezone.utc)
    stale_cutoff = now_utc - timedelta(hours=stale_upload_hours)

    deleted_exports = 0
    deleted_jobs = 0
    cleaned_uploads = 0

    # 1. Expired standalone export artifacts
    expired_arts = (
        db.query(ExportArtifact)
        .filter(ExportArtifact.expires_at.isnot(None), ExportArtifact.expires_at < now_utc)
        .all()
    )
    for art in expired_arts:
        if art.storage_key:
            storage.delete_key(art.storage_key)
        db.delete(art)
        deleted_exports += 1

    # 2. Expired jobs (cascades PageResult, ExportArtifact, StudyProject, Draft)
    expired_jobs = (
        db.query(Job)
        .filter(Job.expires_at.isnot(None), Job.expires_at < now_utc)
        .all()
    )
    for job in expired_jobs:
        storage.delete_key(job.original_storage_key)
        storage.delete_prefix(f"jobs/{job.id}")
        for exp in list(job.exports or []):
            if exp.storage_key:
                storage.delete_key(exp.storage_key)
        db.query(Draft).filter(Draft.job_id == job.id).delete(synchronize_session=False)
        db.delete(job)
        deleted_jobs += 1

    # 3. Stale incomplete upload sessions
    stale_sessions = (
        db.query(UploadSession)
        .filter(
            UploadSession.status.in_(["uploading", "paused", "failed", "aborted"]),
            UploadSession.updated_at < stale_cutoff,
        )
        .all()
    )
    for sess in stale_sessions:
        storage.abort_chunks(sess.id)
        if sess.s3_upload_id and sess.storage_key and sess.status != "completed":
            storage.abort_multipart_upload(sess.storage_key, sess.s3_upload_id)
        if sess.storage_key and sess.status != "completed":
            storage.delete_key(sess.storage_key)
        db.delete(sess)
        cleaned_uploads += 1

    if deleted_exports or deleted_jobs or cleaned_uploads:
        db.commit()

    return {
        "deleted_jobs": deleted_jobs,
        "deleted_exports": deleted_exports,
        "cleaned_upload_sessions": cleaned_uploads,
    }


def get_storage_budget_snapshot(db: Session, user: User | None = None) -> Dict[str, int]:
    """Return current user usage, total tracked usage, and configured quota/budget ceilings."""
    cfg = get_settings()
    total_job_bytes = int(db.query(func.coalesce(func.sum(Job.file_size_bytes), 0)).scalar() or 0)
    total_export_bytes = int(
        db.query(func.coalesce(func.sum(ExportArtifact.file_size_bytes), 0))
        .filter(ExportArtifact.status == "completed")
        .scalar()
        or 0
    )
    tracked_total = total_job_bytes + total_export_bytes
    backend_usage = storage.get_total_usage_bytes() if (storage.is_vercel_blob() or storage.is_s3()) else 0
    effective_total_used = max(tracked_total, backend_usage)

    user_used = 0
    effective_user_quota = int(cfg.USER_STORAGE_QUOTA_BYTES)
    if user is not None:
        u_jobs = int(
            db.query(func.coalesce(func.sum(Job.file_size_bytes), 0))
            .filter(Job.user_id == user.id)
            .scalar()
            or 0
        )
        u_exports = int(
            db.query(func.coalesce(func.sum(ExportArtifact.file_size_bytes), 0))
            .filter(ExportArtifact.user_id == user.id, ExportArtifact.status == "completed")
            .scalar()
            or 0
        )
        user_used = u_jobs + u_exports
        raw_user_quota = int(user.storage_quota_bytes or cfg.USER_STORAGE_QUOTA_BYTES)
        effective_user_quota = min(raw_user_quota, int(cfg.TOTAL_STORAGE_BUDGET_BYTES))

    return {
        "user_used_bytes": user_used,
        "effective_user_quota_bytes": effective_user_quota,
        "total_used_bytes": effective_total_used,
        "total_budget_bytes": int(cfg.TOTAL_STORAGE_QUOTA_BYTES if hasattr(cfg, "TOTAL_STORAGE_QUOTA_BYTES") else cfg.TOTAL_STORAGE_BUDGET_BYTES),
    }


def enforce_storage_budget_and_quota(db: Session, user: User, additional_bytes: int) -> Dict[str, int]:
    """
    Run opportunistic retention cleanup, then enforce both per-user quota
    and total account storage budget.
    """
    cleanup_expired_resources(db)
    snap = get_storage_budget_snapshot(db, user=user)
    if snap["user_used_bytes"] + additional_bytes > snap["effective_user_quota_bytes"]:
        raise StorageQuotaExceededError(
            f"User storage quota exceeded ({snap['user_used_bytes'] + additional_bytes:,} / "
            f"{snap['effective_user_quota_bytes']:,} bytes). Please delete older jobs before uploading."
        )
    if snap["total_used_bytes"] + additional_bytes > snap["total_budget_bytes"]:
        raise StorageQuotaExceededError(
            f"Total account storage budget exceeded ({snap['total_used_bytes'] + additional_bytes:,} / "
            f"{snap['total_budget_bytes']:,} bytes). Delete existing jobs or run retention cleanup to free space."
        )
    return snap
