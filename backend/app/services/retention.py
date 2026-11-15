from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
from typing import Any, Dict, Iterator, Optional
import uuid
from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dispatch import _redis_kwargs_for_url, is_broker_reachable
from app.models import (
    Draft,
    ExportArtifact,
    Job,
    StorageBudgetState,
    StorageReservation,
    UploadSession,
    User,
)
from app.storage import storage

RETENTION_LOCK_REDIS_KEY = "doc_studio:retention:cleanup_lock"
RETENTION_PG_ADVISORY_LOCK_ID = 748291045
_local_cleanup_lock = threading.Lock()


class _RedisLockRenewer(threading.Thread):
    """
    Background watchdog that extends the Redis retention lock TTL periodically
    while a long cleanup (storage deletions, DB cleanup) is actively running.
    Prevents lock expiry during heavy storage operations.
    """

    def __init__(self, redis_client: Any, lock_key: str, lock_token: str, ttl_seconds: int):
        super().__init__(daemon=True, name="redis-retention-lock-renewer")
        self.redis_client = redis_client
        self.lock_key = lock_key
        self.lock_token = lock_token
        self.ttl_seconds = ttl_seconds
        self.stop_event = threading.Event()

    def run(self) -> None:
        renew_interval = max(0.5, self.ttl_seconds / 3.0)
        while not self.stop_event.wait(renew_interval):
            try:
                res = self.redis_client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                    1,
                    self.lock_key,
                    self.lock_token,
                    self.ttl_seconds,
                )
                if not res:
                    break
            except Exception:
                break

    def stop(self) -> None:
        self.stop_event.set()


class StorageQuotaExceededError(Exception):
    """Raised when a user's storage quota or the total account storage budget would be exceeded."""


@contextmanager
def _try_retention_distributed_lock(db: Session, ttl_seconds: int = 120) -> Iterator[bool]:
    """
    Acquire a non-blocking distributed lock across process threads, Redis (if reachable),
    and PostgreSQL (`pg_try_advisory_xact_lock`) so overlapping retention triggers
    (in-worker scheduler, Celery task, opportunistic check, and Vercel Cron) never collide.
    Holds the PostgreSQL transaction-scoped advisory lock for the full duration of cleanup
    across all external storage operations until completion.
    """
    if not _local_cleanup_lock.acquire(blocking=False):
        yield False
        return

    redis_client = None
    renewer: Optional[_RedisLockRenewer] = None
    lock_token = uuid.uuid4().hex
    try:
        cfg = get_settings()
        broker_url = cfg.CELERY_BROKER_URL or ""
        if is_broker_reachable(broker_url, 0.2, 0.5):
            try:
                import redis

                redis_client = redis.Redis.from_url(
                    broker_url,
                    **_redis_kwargs_for_url(broker_url, 0.2, 0.5),
                )
                acquired_redis = bool(
                    redis_client.set(RETENTION_LOCK_REDIS_KEY, lock_token, nx=True, ex=ttl_seconds)
                )
                if not acquired_redis:
                    redis_client = None
                    yield False
                    return
                # Start active lease renewal watchdog to keep Redis lock alive during long sweeps
                renewer = _RedisLockRenewer(redis_client, RETENTION_LOCK_REDIS_KEY, lock_token, ttl_seconds)
                renewer.start()
            except Exception:
                redis_client = None

        dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
        if dialect == "postgresql":
            pg_acquired = bool(
                db.execute(
                    text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                    {"lock_id": RETENTION_PG_ADVISORY_LOCK_ID},
                ).scalar()
            )
            if not pg_acquired:
                yield False
                return

        yield True
    finally:
        if renewer is not None:
            try:
                renewer.stop()
            except Exception:
                pass
        if redis_client is not None:
            try:
                redis_client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    RETENTION_LOCK_REDIS_KEY,
                    lock_token,
                )
            except Exception:
                pass
        dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
        if dialect == "postgresql":
            # If the session is still active and uncommitted (e.g. no rows modified),
            # rollback ensures the transaction-scoped advisory lock is cleanly released.
            try:
                if db.is_active:
                    db.rollback()
            except Exception:
                pass
        _local_cleanup_lock.release()


def _acquire_budget_lock(db: Session) -> StorageBudgetState:
    """
    Acquire a row-level lock on the singleton StorageBudgetState row (id=1) so concurrent
    uploads and worker output generators cannot over-allocate the storage budget.
    Ensures row id=1 exists via ON CONFLICT DO NOTHING so concurrent workers never race on initial INSERT.
    """
    cfg = get_settings()
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    now_utc = datetime.now(timezone.utc)

    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(StorageBudgetState)
            .values(
                id=1,
                total_budget_bytes=int(cfg.TOTAL_STORAGE_BUDGET_BYTES),
                updated_at=now_utc,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        db.execute(stmt)

    query = db.query(StorageBudgetState).filter(StorageBudgetState.id == 1)
    if dialect != "sqlite":
        query = query.with_for_update()
    state = query.first()
    if state is None:
        state = StorageBudgetState(
            id=1,
            total_budget_bytes=int(cfg.TOTAL_STORAGE_BUDGET_BYTES),
            updated_at=now_utc,
        )
        db.add(state)
        db.flush()
    else:
        state.total_budget_bytes = int(cfg.TOTAL_STORAGE_BUDGET_BYTES)
        state.updated_at = now_utc
        db.flush()
    return state


def _effective_reservation_expr():
    return case(
        (StorageReservation.reserved_bytes >= StorageReservation.actual_bytes, StorageReservation.reserved_bytes),
        else_=StorageReservation.actual_bytes,
    )


def cleanup_expired_resources(db: Session, stale_upload_hours: int = 6) -> Dict[str, Any]:
    """
    Idempotently delete expired jobs, expired export artifacts, stale incomplete upload sessions,
    and expired/abandoned storage reservations from both private storage and the database.
    Uses a non-blocking distributed lock across process threads, Redis, and PostgreSQL so
    overlapping triggers (worker scheduler, Celery Beat/task, opportunistic check, and Vercel Cron)
    never collide.
    """
    with _try_retention_distributed_lock(db) as acquired:
        if not acquired:
            return {
                "skipped_locked": True,
                "deleted_jobs": 0,
                "deleted_exports": 0,
                "cleaned_upload_sessions": 0,
                "released_reservations": 0,
            }

        now_utc = datetime.now(timezone.utc)
        stale_cutoff = now_utc - timedelta(hours=stale_upload_hours)
        dialect = db.bind.dialect.name if db.bind is not None else "sqlite"

        deleted_exports = 0
        deleted_jobs = 0
        cleaned_uploads = 0
        released_reservations = 0

        # 1. Expired standalone export artifacts
        exp_arts_q = db.query(ExportArtifact).filter(
            ExportArtifact.expires_at.isnot(None),
            ExportArtifact.expires_at < now_utc,
        )
        if dialect == "postgresql":
            exp_arts_q = exp_arts_q.with_for_update(skip_locked=True)
        expired_arts = exp_arts_q.all()
        for art in expired_arts:
            if art.storage_key:
                storage.delete_key(art.storage_key)
            released_reservations += release_reservations_for_resource(
                db, resource_type="export", resource_id=art.id, commit=False
            )
            deleted = (
                db.query(ExportArtifact)
                .filter(ExportArtifact.id == art.id)
                .delete(synchronize_session=False)
            )
            if deleted:
                deleted_exports += 1

        # 2. Expired jobs (cascades PageResult, ExportArtifact, StudyProject, Draft)
        exp_jobs_q = db.query(Job).filter(
            Job.expires_at.isnot(None),
            Job.expires_at < now_utc,
        )
        if dialect == "postgresql":
            exp_jobs_q = exp_jobs_q.with_for_update(skip_locked=True)
        expired_jobs = exp_jobs_q.all()
        for job in expired_jobs:
            release_all_job_storage_and_reservations(db, job, delete_original=True, commit=False)
            db.query(Draft).filter(Draft.job_id == job.id).delete(synchronize_session=False)
            db.delete(job)
            deleted_jobs += 1

        # 3. Stale incomplete or failed/aborted upload sessions
        stale_q = db.query(UploadSession).filter(
            UploadSession.status.in_(["uploading", "paused", "failed", "aborted"]),
            UploadSession.updated_at < stale_cutoff,
        )
        if dialect == "postgresql":
            stale_q = stale_q.with_for_update(skip_locked=True)
        stale_sessions = stale_q.all()
        for sess in stale_sessions:
            storage.abort_chunks(sess.id)
            if sess.s3_upload_id and sess.storage_key and sess.status != "completed":
                storage.abort_multipart_upload(sess.storage_key, sess.s3_upload_id)
            if sess.storage_key and sess.status != "completed":
                storage.delete_key(sess.storage_key)
            released_reservations += release_reservations_for_resource(
                db, resource_type="upload", resource_id=sess.id, commit=False
            )
            deleted_s = (
                db.query(UploadSession)
                .filter(UploadSession.id == sess.id, UploadSession.status != "completed")
                .delete(synchronize_session=False)
            )
            if deleted_s:
                cleaned_uploads += 1

        # 4. Expired active reservations (abandoned uploads, crashed temp objects, expired outputs)
        exp_res_q = db.query(StorageReservation).filter(
            StorageReservation.status.in_(["reserved", "committed"]),
            StorageReservation.expires_at.isnot(None),
            StorageReservation.expires_at < now_utc,
        )
        if dialect == "postgresql":
            exp_res_q = exp_res_q.with_for_update(skip_locked=True)
        expired_res = exp_res_q.all()
        for res in expired_res:
            claimed = (
                db.query(StorageReservation)
                .filter(
                    StorageReservation.id == res.id,
                    StorageReservation.status.in_(["reserved", "committed"]),
                )
                .update(
                    {
                        StorageReservation.status: "expired",
                        StorageReservation.reserved_bytes: 0,
                        StorageReservation.actual_bytes: 0,
                        StorageReservation.updated_at: now_utc,
                    },
                    synchronize_session=False,
                )
            )
            if claimed:
                if res.storage_key and res.resource_type in {"upload", "temp_object", "export", "preview"}:
                    storage.delete_key(res.storage_key)
                released_reservations += 1

        if deleted_exports or deleted_jobs or cleaned_uploads or released_reservations:
            db.commit()

        return {
            "skipped_locked": False,
            "deleted_jobs": deleted_jobs,
            "deleted_exports": deleted_exports,
            "cleaned_upload_sessions": cleaned_uploads,
            "released_reservations": released_reservations,
        }


def get_storage_budget_snapshot(db: Session, user: Optional[User] = None) -> Dict[str, Any]:
    """
    Return current user usage, total tracked/reserved usage, and configured quota/budget ceilings.
    Accounts for all active ('reserved' + 'committed') StorageReservation rows plus any legacy
    untracked Job or ExportArtifact rows.
    """
    cfg = get_settings()
    active_statuses = ("reserved", "committed")
    eff_expr = _effective_reservation_expr()

    active_reserved_only = int(
        db.query(func.coalesce(func.sum(eff_expr), 0))
        .filter(StorageReservation.status == "reserved")
        .scalar()
        or 0
    )
    active_committed_only = int(
        db.query(func.coalesce(func.sum(eff_expr), 0))
        .filter(StorageReservation.status == "committed")
        .scalar()
        or 0
    )
    total_reservations_bytes = active_reserved_only + active_committed_only

    # Include any Job / ExportArtifact rows that were inserted directly without a reservation row
    reserved_job_ids = {
        r[0]
        for r in db.query(StorageReservation.resource_id)
        .filter(
            StorageReservation.resource_type == "original",
            StorageReservation.status.in_(active_statuses),
        )
        .all()
    }
    untracked_jobs_q = db.query(func.coalesce(func.sum(Job.file_size_bytes), 0))
    if reserved_job_ids:
        untracked_jobs_q = untracked_jobs_q.filter(~Job.id.in_(reserved_job_ids))
    untracked_job_bytes = int(untracked_jobs_q.scalar() or 0)

    reserved_export_ids = {
        r[0]
        for r in db.query(StorageReservation.resource_id)
        .filter(
            StorageReservation.resource_type == "export",
            StorageReservation.status.in_(active_statuses),
        )
        .all()
    }
    untracked_exports_q = db.query(func.coalesce(func.sum(ExportArtifact.file_size_bytes), 0)).filter(
        ExportArtifact.status == "completed"
    )
    if reserved_export_ids:
        untracked_exports_q = untracked_exports_q.filter(~ExportArtifact.id.in_(reserved_export_ids))
    untracked_export_bytes = int(untracked_exports_q.scalar() or 0)

    tracked_total = total_reservations_bytes + untracked_job_bytes + untracked_export_bytes
    backend_usage = storage.get_total_usage_bytes() if (storage.is_vercel_blob() or storage.is_s3()) else 0
    effective_total_used = max(tracked_total, backend_usage + active_reserved_only)

    user_used = 0
    effective_user_quota = min(int(cfg.USER_STORAGE_QUOTA_BYTES), int(cfg.TOTAL_STORAGE_BUDGET_BYTES))
    if user is not None:
        u_res = int(
            db.query(func.coalesce(func.sum(eff_expr), 0))
            .filter(
                StorageReservation.user_id == user.id,
                StorageReservation.status.in_(active_statuses),
            )
            .scalar()
            or 0
        )
        u_jobs_q = db.query(func.coalesce(func.sum(Job.file_size_bytes), 0)).filter(Job.user_id == user.id)
        if reserved_job_ids:
            u_jobs_q = u_jobs_q.filter(~Job.id.in_(reserved_job_ids))
        u_untracked_jobs = int(u_jobs_q.scalar() or 0)

        u_exports_q = db.query(func.coalesce(func.sum(ExportArtifact.file_size_bytes), 0)).filter(
            ExportArtifact.user_id == user.id, ExportArtifact.status == "completed"
        )
        if reserved_export_ids:
            u_exports_q = u_exports_q.filter(~ExportArtifact.id.in_(reserved_export_ids))
        u_untracked_exports = int(u_exports_q.scalar() or 0)

        user_used = u_res + u_untracked_jobs + u_untracked_exports
        raw_user_quota = int(user.storage_quota_bytes or cfg.USER_STORAGE_QUOTA_BYTES)
        effective_user_quota = min(raw_user_quota, int(cfg.TOTAL_STORAGE_BUDGET_BYTES))

    total_budget = int(cfg.TOTAL_STORAGE_BUDGET_BYTES)
    return {
        "user_used_bytes": user_used,
        "effective_user_quota_bytes": effective_user_quota,
        "active_reserved_bytes": active_reserved_only,
        "active_committed_bytes": active_committed_only,
        "total_used_bytes": effective_total_used,
        "total_budget_bytes": total_budget,
        "remaining_budget_bytes": max(0, total_budget - effective_total_used),
        "hobby_capacity_note": (
            "An 850 MB budget guards the 1 GB Hobby cap and does not guarantee a 500 MB document "
            "plus derived page previews and exports will fit concurrently."
        ),
    }


def enforce_storage_budget_and_quota(db: Session, user: User, additional_bytes: int) -> Dict[str, Any]:
    """
    Run opportunistic retention cleanup, then check whether `additional_bytes` fits within
    both the user's quota and the total account storage budget.
    """
    cleanup_expired_resources(db)
    snap = get_storage_budget_snapshot(db, user=user)
    if snap["user_used_bytes"] + additional_bytes > snap["effective_user_quota_bytes"]:
        raise StorageQuotaExceededError(
            f"User storage quota exceeded ({snap['user_used_bytes'] + additional_bytes:,} / "
            f"{snap['effective_user_quota_bytes']:,} bytes). Please delete older jobs or exports first."
        )
    if snap["total_used_bytes"] + additional_bytes > snap["total_budget_bytes"]:
        raise StorageQuotaExceededError(
            f"Total account storage budget exceeded ({snap['total_used_bytes'] + additional_bytes:,} / "
            f"{snap['total_budget_bytes']:,} bytes). Note: the 850 MB Hobby budget does not guarantee "
            f"every 500 MB document plus previews and exports will fit concurrently."
        )
    return snap


def reserve_storage_capacity(
    db: Session,
    *,
    user: User,
    resource_type: str,
    resource_id: str,
    reserved_bytes: int,
    storage_key: Optional[str] = None,
    ttl_seconds: int = 21_600,
    expires_at: Optional[datetime] = None,
    commit: bool = True,
) -> StorageReservation:
    """
    Atomically lock StorageBudgetState(id=1), verify capacity across all active concurrent
    uploads and outputs, and create or update a 'reserved' StorageReservation row.
    """
    cleanup_expired_resources(db)
    _acquire_budget_lock(db)
    now_utc = datetime.now(timezone.utc)

    existing = (
        db.query(StorageReservation)
        .filter(
            StorageReservation.resource_type == resource_type,
            StorageReservation.resource_id == resource_id,
            StorageReservation.status.in_(["reserved", "committed"]),
        )
        .first()
    )
    prior_bytes = max(int(existing.reserved_bytes or 0), int(existing.actual_bytes or 0)) if existing else 0
    delta_bytes = max(0, int(reserved_bytes) - prior_bytes)

    snap = get_storage_budget_snapshot(db, user=user)
    if snap["user_used_bytes"] + delta_bytes > snap["effective_user_quota_bytes"]:
        raise StorageQuotaExceededError(
            f"User storage quota exceeded ({snap['user_used_bytes'] + delta_bytes:,} / "
            f"{snap['effective_user_quota_bytes']:,} bytes) while reserving {resource_type}."
        )
    if snap["total_used_bytes"] + delta_bytes > snap["total_budget_bytes"]:
        raise StorageQuotaExceededError(
            f"Total account storage budget exceeded ({snap['total_used_bytes'] + delta_bytes:,} / "
            f"{snap['total_budget_bytes']:,} bytes) while reserving {resource_type}."
        )

    resolved_expires = expires_at or (now_utc + timedelta(seconds=ttl_seconds))
    if existing is not None:
        existing.reserved_bytes = int(reserved_bytes)
        if storage_key:
            existing.storage_key = storage_key
        existing.expires_at = resolved_expires
        existing.updated_at = now_utc
        res = existing
    else:
        res = StorageReservation(
            user_id=user.id,
            resource_type=resource_type,
            resource_id=resource_id,
            storage_key=storage_key,
            reserved_bytes=int(reserved_bytes),
            actual_bytes=0,
            status="reserved",
            expires_at=resolved_expires,
            created_at=now_utc,
            updated_at=now_utc,
        )
        db.add(res)

    if commit:
        db.commit()
        db.refresh(res)
    else:
        db.flush()
    return res


def commit_storage_reservation(
    db: Session,
    *,
    user_id: str,
    resource_type: str,
    resource_id: str,
    actual_bytes: int,
    new_resource_type: Optional[str] = None,
    new_resource_id: Optional[str] = None,
    storage_key: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    commit: bool = True,
) -> StorageReservation:
    """
    Atomically verify and commit actual output/upload bytes under the StorageBudgetState lock.
    If actual_bytes exceeds the originally reserved bytes and breaches the user or store budget,
    raises StorageQuotaExceededError so the caller can abort and clean up the object.
    """
    _acquire_budget_lock(db)
    now_utc = datetime.now(timezone.utc)
    user = db.query(User).filter(User.id == user_id).first()

    existing = (
        db.query(StorageReservation)
        .filter(
            StorageReservation.resource_type == resource_type,
            StorageReservation.resource_id == resource_id,
            StorageReservation.status.in_(["reserved", "committed"]),
        )
        .first()
    )
    prior_bytes = max(int(existing.reserved_bytes or 0), int(existing.actual_bytes or 0)) if existing else 0
    delta_bytes = max(0, int(actual_bytes) - prior_bytes)

    if delta_bytes > 0:
        snap = get_storage_budget_snapshot(db, user=user)
        if user is not None and snap["user_used_bytes"] + delta_bytes > snap["effective_user_quota_bytes"]:
            raise StorageQuotaExceededError(
                f"User storage quota exceeded ({snap['user_used_bytes'] + delta_bytes:,} / "
                f"{snap['effective_user_quota_bytes']:,} bytes) during {new_resource_type or resource_type} commit."
            )
        if snap["total_used_bytes"] + delta_bytes > snap["total_budget_bytes"]:
            raise StorageQuotaExceededError(
                f"Total storage budget exceeded ({snap['total_used_bytes'] + delta_bytes:,} / "
                f"{snap['total_budget_bytes']:,} bytes) during {new_resource_type or resource_type} commit."
            )

    if existing is not None:
        if new_resource_type:
            existing.resource_type = new_resource_type
        if new_resource_id:
            existing.resource_id = new_resource_id
        if storage_key:
            existing.storage_key = storage_key
        existing.reserved_bytes = int(actual_bytes)
        existing.actual_bytes = int(actual_bytes)
        existing.status = "committed"
        if expires_at is not None:
            existing.expires_at = expires_at
        existing.updated_at = now_utc
        res = existing
    else:
        res = StorageReservation(
            user_id=user_id,
            resource_type=new_resource_type or resource_type,
            resource_id=new_resource_id or resource_id,
            storage_key=storage_key,
            reserved_bytes=int(actual_bytes),
            actual_bytes=int(actual_bytes),
            status="committed",
            expires_at=expires_at,
            created_at=now_utc,
            updated_at=now_utc,
        )
        db.add(res)

    if commit:
        db.commit()
        db.refresh(res)
    else:
        db.flush()
    return res


def release_reservations_for_resource(
    db: Session,
    *,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    resource_id_prefix: Optional[str] = None,
    commit: bool = True,
) -> int:
    """Release active storage reservations matching a resource_id or prefix."""
    now_utc = datetime.now(timezone.utc)
    q = db.query(StorageReservation).filter(StorageReservation.status.in_(["reserved", "committed"]))
    if resource_type:
        q = q.filter(StorageReservation.resource_type == resource_type)
    if resource_id:
        q = q.filter(StorageReservation.resource_id == resource_id)
    if resource_id_prefix:
        q = q.filter(StorageReservation.resource_id.like(f"{resource_id_prefix}%"))

    rows = q.all()
    for row in rows:
        row.status = "released"
        row.reserved_bytes = 0
        row.actual_bytes = 0
        row.updated_at = now_utc
    if commit and rows:
        db.commit()
    elif rows:
        db.flush()
    return len(rows)


def release_all_job_storage_and_reservations(
    db: Session,
    job: Job,
    *,
    delete_original: bool = False,
    commit: bool = True,
) -> None:
    """
    Delete stored artifacts for a job (previews, exports, and optionally original)
    and atomically release their storage reservations.
    """
    if delete_original and job.original_storage_key:
        storage.delete_key(job.original_storage_key)
        release_reservations_for_resource(
            db, resource_type="original", resource_id=job.id, commit=False
        )
        if job.upload_session_id:
            release_reservations_for_resource(
                db, resource_type="upload", resource_id=job.upload_session_id, commit=False
            )
    storage.delete_prefix(f"jobs/{job.id}")
    release_reservations_for_resource(
        db, resource_type="preview", resource_id_prefix=f"{job.id}:", commit=False
    )
    release_reservations_for_resource(
        db, resource_type="temp_object", resource_id_prefix=f"{job.id}:", commit=False
    )
    for exp in list(job.exports or []):
        if exp.storage_key:
            storage.delete_key(exp.storage_key)
        release_reservations_for_resource(
            db, resource_type="export", resource_id=exp.id, commit=False
        )
    if commit:
        db.commit()
    else:
        db.flush()
