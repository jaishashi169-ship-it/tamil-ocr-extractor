import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.config import get_settings
from app.database import engine, get_db
from app.dispatch import _redis_kwargs_for_url, is_broker_reachable
from app.models import Job, StorageReservation, SystemSetting, User
from app.services.retention import (
    cleanup_expired_resources,
    commit_storage_reservation,
    get_storage_budget_snapshot,
    release_reservations_for_resource,
    reserve_storage_capacity,
)
from app.services.tamil_utils import SUPPORTED_LEGACY_ENCODINGS
from app.storage import storage

WORKER_STATUS_REDIS_KEY = "doc_studio:worker:status"


def _get_verified_worker_status() -> Optional[Dict[str, Any]]:
    cfg = get_settings()
    broker_url = cfg.CELERY_BROKER_URL or ""
    if os.environ.get("VERCEL") and ("localhost" in broker_url or "127.0.0.1" in broker_url):
        return None
    if not is_broker_reachable(broker_url, cfg.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS, cfg.REDIS_SOCKET_TIMEOUT_SECONDS):
        return None
    try:
        import redis

        client = redis.from_url(
            broker_url,
            **_redis_kwargs_for_url(
                broker_url,
                cfg.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
                cfg.REDIS_SOCKET_TIMEOUT_SECONDS,
            ),
        )
        raw = client.get(WORKER_STATUS_REDIS_KEY)
        if not raw:
            return None
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def _safe_ocr_status() -> Dict[str, Any]:
    worker_status = _get_verified_worker_status()
    if worker_status and worker_status.get("available"):
        return {
            "available": True,
            "status": "verified_worker",
            "execution_mode": "worker_daemon",
            "verified_locally": False,
            "verified_worker": True,
            "verification_type": worker_status.get("verification_type", "synthetic_ocr_execution_smoke_test"),
            "is_synthetic_smoke_test": bool(worker_status.get("is_synthetic_smoke_test", True)),
            "configured_languages": ["eng", "tam"],
            "languages": worker_status.get("languages", []),
            "tamil_cer": worker_status.get("tamil_cer"),
            "tamil_wer": worker_status.get("tamil_wer"),
            "english_cer": worker_status.get("english_cer"),
            "english_wer": worker_status.get("english_wer"),
            "registered_tasks": worker_status.get("registered_tasks", []),
            "updated_at": worker_status.get("updated_at"),
        }

    if not os.environ.get("VERCEL"):
        try:
            from app.services.ocr_engine import get_ocr_status

            status_info = get_ocr_status()
            is_avail = bool(status_info.get("available"))
            status_info["status"] = "verified_local" if is_avail else "configured_but_unverified"
            status_info["execution_mode"] = "local_or_worker"
            status_info["verified_locally"] = is_avail
            status_info["verified_worker"] = False
            status_info["configured_languages"] = ["eng", "tam"]
            return status_info
        except ImportError:
            pass

    return {
        "available": False,
        "status": "configured_but_unverified",
        "execution_mode": "worker_daemon",
        "verified_locally": False,
        "verified_worker": False,
        "configured_languages": ["eng", "tam"],
        "languages": [],
    }


def _safe_ocr_languages() -> list[str]:
    return _safe_ocr_status().get("languages", [])


def evaluate_readiness() -> Dict[str, Any]:
    cfg = get_settings()
    is_serverless_or_prod = cfg.APP_ENV == "production" or bool(os.environ.get("VERCEL"))

    # 1. Database check
    if is_serverless_or_prod and cfg.SYNC_DATABASE_URL.startswith("sqlite"):
        db_status: Dict[str, Any] = {
            "ready": False,
            "driver": "sqlite",
            "reason": "Managed PostgreSQL is required; SQLite or /tmp database is not permitted on Vercel/production.",
        }
    else:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db_status = {"ready": True, "driver": str(engine.url.drivername)}
        except Exception as exc:
            db_status = {"ready": False, "driver": str(engine.url.drivername), "reason": str(exc)}

    # 2. Broker check
    broker_url = cfg.CELERY_BROKER_URL or ""
    if is_serverless_or_prod and ("localhost" in broker_url or "127.0.0.1" in broker_url):
        broker_status: Dict[str, Any] = {
            "ready": False,
            "reason": "Managed Redis broker is required; CELERY_BROKER_URL points to localhost.",
        }
    elif is_broker_reachable(broker_url, cfg.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS, cfg.REDIS_SOCKET_TIMEOUT_SECONDS):
        broker_status = {"ready": True}
    else:
        broker_status = {
            "ready": False,
            "fallback_enabled": bool(cfg.ALLOW_BACKGROUND_TASK_FALLBACK and not is_serverless_or_prod),
            "reason": "Redis broker unreachable.",
        }

    # 3. Durable storage check
    storage_status = storage.check_readiness()

    # 4. Worker & OCR verification check
    ocr = _safe_ocr_status()
    worker_ready = bool(ocr.get("available"))
    worker_status_report: Dict[str, Any] = {
        "ready": worker_ready,
        "status": ocr.get("status", "configured_but_unverified"),
        "execution_mode": ocr.get("execution_mode", "worker_daemon"),
        "verified_worker": bool(ocr.get("verified_worker")),
        "verified_locally": bool(ocr.get("verified_locally")),
        "languages": ocr.get("languages", []),
        "configured_languages": ocr.get("configured_languages", ["eng", "tam"]),
    }
    if not worker_ready:
        worker_status_report["reason"] = "No reachable worker with verified Tesseract (tam+eng) heartbeat."

    broker_ok = broker_status["ready"] or bool(broker_status.get("fallback_enabled"))
    overall_ready = bool(db_status["ready"] and broker_ok and storage_status["ready"] and worker_ready)
    return {
        "status": "ready" if overall_ready else "not_ready",
        "ready": overall_ready,
        "environment": cfg.APP_ENV,
        "dependencies": {
            "database": db_status,
            "broker": broker_status,
            "storage": storage_status,
            "worker": worker_status_report,
        },
    }

router = APIRouter(tags=["Capabilities & Administration"])
settings = get_settings()


class UpdateQuotasRequest(BaseModel):
    max_upload_bytes: int = Field(default=settings.MAX_UPLOAD_BYTES, ge=1_000_000, le=500_000_000)
    default_retention_days: int = Field(default=settings.DEFAULT_RETENTION_DAYS, ge=1, le=365)
    user_storage_quota_bytes: int = Field(default=settings.USER_STORAGE_QUOTA_BYTES, ge=50_000_000)
    total_storage_budget_bytes: int = Field(default=settings.TOTAL_STORAGE_BUDGET_BYTES, ge=100_000_000)


@router.get("/health/ready")
def get_api_readiness():
    """Readiness probe reporting database, broker, storage, and verified OCR worker status."""
    report = evaluate_readiness()
    return JSONResponse(status_code=200 if report["ready"] else 503, content=report)


@router.get("/capabilities")
def get_capabilities():
    """Public capability matrix and installed OCR language packs."""
    cfg = get_settings()
    ocr = _safe_ocr_status()
    return {
        "app_name": cfg.APP_NAME,
        "max_upload_bytes": cfg.MAX_UPLOAD_BYTES,
        "upload_chunk_bytes": cfg.UPLOAD_CHUNK_BYTES,
        "user_storage_quota_bytes": cfg.USER_STORAGE_QUOTA_BYTES,
        "total_storage_budget_bytes": cfg.TOTAL_STORAGE_BUDGET_BYTES,
        "storage_capacity_note": (
            "Atomic reservations enforce total_storage_budget_bytes across concurrent uploads, originals, "
            "page previews, exports, and temporary objects. On Hobby plans (850 MB safety budget within 1 GB cap), "
            "a 500 MB upload plus generated previews and exports may exceed remaining capacity and is NOT guaranteed "
            "to fit without prior cleanup or a larger storage plan."
        ),
        "upload_access_policy": {
            "require_readiness_for_uploads": bool(cfg.REQUIRE_READINESS_FOR_UPLOADS),
            "restricted_smoke_test_mode": bool(cfg.RESTRICTED_SMOKE_TEST_MODE),
        },
        "storage_backend": cfg.STORAGE_BACKEND.lower(),
        "max_batch_files": cfg.MAX_BATCH_FILES,
        "max_pdf_pages": cfg.MAX_PDF_PAGES,
        "installed_ocr_languages": ocr["languages"],
        "configured_ocr_languages": ocr.get("configured_languages", ocr["languages"]),
        "ocr_status": ocr,
        "supported_legacy_tamil_encodings": SUPPORTED_LEGACY_ENCODINGS,
        "supported_imports": ["pdf", "docx", "doc", "txt", "md", "png", "jpg", "jpeg", "webp", "tiff", "bmp", "psd", "html", "csv", "json"],
        "supported_exports": ["txt", "md", "docx", "pdf", "searchable_pdf", "html", "json", "csv", "xlsx", "png", "jpg", "webp", "zip"],
        "quality_presets": ["original", "high", "balanced", "smallest", "custom"],
    }


@router.get("/admin/overview")
def get_admin_overview(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Administrator dashboard: job counts, processing duration, storage usage, worker health, capabilities.
    Never exposes user document contents.
    """
    cfg = get_settings()
    status_counts = dict(db.query(Job.status, func.count(Job.id)).group_by(Job.status).all())
    avg_duration_ms = db.query(func.avg(Job.duration_ms)).filter(Job.duration_ms.isnot(None)).scalar()
    total_users = db.query(func.count(User.id)).scalar() or 0
    budget_snap = get_storage_budget_snapshot(db)

    return {
        "jobs_by_status": {
            "queued": status_counts.get("queued", 0),
            "running": status_counts.get("running", 0),
            "completed": status_counts.get("completed", 0),
            "cancelled": status_counts.get("cancelled", 0),
            "failed": status_counts.get("failed", 0),
        },
        "avg_processing_duration_ms": round(float(avg_duration_ms), 1) if avg_duration_ms else 0.0,
        "storage_usage_bytes": budget_snap["total_used_bytes"],
        "storage_budget": budget_snap,
        "total_users": total_users,
        "worker_health": {
            "status": "healthy" if _safe_ocr_status().get("available") else "degraded",
            "isolated_network": True,
            "ocr_execution_mode": "local_or_worker" if _safe_ocr_languages() else "worker_daemon",
            "installed_ocr_languages": _safe_ocr_languages(),
        },
        "quotas": {
            "max_upload_bytes": cfg.MAX_UPLOAD_BYTES,
            "default_retention_days": cfg.DEFAULT_RETENTION_DAYS,
            "user_storage_quota_bytes": cfg.USER_STORAGE_QUOTA_BYTES,
            "total_storage_budget_bytes": cfg.TOTAL_STORAGE_BUDGET_BYTES,
        },
    }


@router.get("/admin/quotas")
def get_admin_quotas(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Detailed storage quota, active reservation breakdown, queue depth, and threshold monitoring."""
    cfg = get_settings()
    budget = get_storage_budget_snapshot(db)
    eff_expr = func.coalesce(
        func.nullif(StorageReservation.actual_bytes, 0),
        StorageReservation.reserved_bytes,
        0,
    )
    by_type_rows = (
        db.query(
            StorageReservation.resource_type,
            StorageReservation.status,
            func.count(StorageReservation.id),
            func.coalesce(func.sum(eff_expr), 0),
        )
        .filter(StorageReservation.status.in_(["reserved", "committed"]))
        .group_by(StorageReservation.resource_type, StorageReservation.status)
        .all()
    )
    reservation_breakdown = [
        {
            "resource_type": r_type,
            "status": r_status,
            "count": int(cnt or 0),
            "bytes": int(total_b or 0),
        }
        for r_type, r_status, cnt, total_b in by_type_rows
    ]
    used_pct = round(
        (budget["total_used_bytes"] / max(1, budget["total_budget_bytes"])) * 100.0,
        2,
    )
    queued_jobs = int(db.query(func.count(Job.id)).filter(Job.status == "queued").scalar() or 0)
    running_jobs = int(db.query(func.count(Job.id)).filter(Job.status == "running").scalar() or 0)
    return {
        "storage_budget": budget,
        "utilization_percent": used_pct,
        "warning_level": "critical" if used_pct >= 90 else ("warning" if used_pct >= 75 else "normal"),
        "reservation_breakdown": reservation_breakdown,
        "queue_depth": {
            "queued_jobs": queued_jobs,
            "running_jobs": running_jobs,
        },
        "worker_heartbeat": _get_verified_worker_status(),
        "upload_gating": {
            "require_readiness_for_uploads": bool(cfg.REQUIRE_READINESS_FOR_UPLOADS),
            "restricted_smoke_test_mode": bool(cfg.RESTRICTED_SMOKE_TEST_MODE),
        },
    }


@router.post("/admin/retention/cleanup")
def trigger_retention_cleanup(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trigger immediate retention cleanup of expired jobs, exports, and stale upload sessions."""
    summary = cleanup_expired_resources(db)
    budget = get_storage_budget_snapshot(db)
    return {"cleaned": summary, "storage_budget": budget}


@router.get("/internal/cron/retention")
@router.post("/internal/cron/retention")
def scheduled_cron_retention_cleanup(
    authorization: Optional[str] = Header(default=None),
    x_cron_secret: Optional[str] = Header(default=None, alias="X-Cron-Secret"),
    db: Session = Depends(get_db),
):
    """
    Platform-scheduled retention & expired-reservation cleanup endpoint (compatible with Vercel Cron).
    Requires Authorization: Bearer <CRON_SECRET> or X-Cron-Secret header.
    """
    cfg = get_settings()
    expected = (cfg.CRON_SECRET or cfg.SECRET_KEY or "").strip()
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    elif x_cron_secret:
        supplied = x_cron_secret.strip()

    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing cron authorization secret.",
        )

    summary = cleanup_expired_resources(db)
    budget = get_storage_budget_snapshot(db)
    return {
        "status": "completed",
        "trigger": "scheduled_cron",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cleaned": summary,
        "storage_budget": budget,
    }


@router.post("/admin/smoke-test")
def run_restricted_production_smoke_test(
    x_smoke_test_token: Optional[str] = Header(default=None, alias="X-Smoke-Test-Token"),
    db: Session = Depends(get_db),
):
    """
    Restricted production smoke test endpoint.
    Authorized via X-Smoke-Test-Token matching SMOKE_TEST_SECRET (or SECRET_KEY when SMOKE_TEST_SECRET is unset).
    Verifies readiness, atomic storage reservation + object write/read/delete round-trip, and worker status
    while keeping public uploads blocked when RESTRICTED_SMOKE_TEST_MODE=true or REQUIRE_READINESS_FOR_UPLOADS=true.
    """
    cfg = get_settings()
    expected = (cfg.SMOKE_TEST_SECRET or cfg.SECRET_KEY or "").strip()
    supplied = (x_smoke_test_token or "").strip()
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Valid X-Smoke-Test-Token header is required for restricted production smoke test.",
        )

    readiness = evaluate_readiness()
    probe_id = f"smoke-{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    probe_key = f"smoke-tests/{probe_id}.txt"
    probe_payload = b"Tamil-English Document Studio Smoke Probe: \xe0\xae\xa4\xe0\xae\xae\xe0\xae\xbf\xe0\xae\xb4\xe0\xaf\x8d + English"
    storage_roundtrip_ok = False
    storage_error: Optional[str] = None

    smoke_user = db.query(User).first()
    if smoke_user is None:
        smoke_user = User(
            email="smoke-probe@studio.local",
            hashed_password="x",
            role="admin",
            storage_quota_bytes=cfg.USER_STORAGE_QUOTA_BYTES,
            retention_days=1,
        )
        db.add(smoke_user)
        db.commit()
        db.refresh(smoke_user)

    try:
        reserve_storage_capacity(
            db,
            user=smoke_user,
            resource_type="temp_object",
            resource_id=probe_id,
            reserved_bytes=len(probe_payload),
            storage_key=probe_key,
            ttl_seconds=300,
        )
        storage.save_bytes(probe_key, probe_payload, content_type="text/plain; charset=utf-8")
        commit_storage_reservation(
            db,
            user_id=smoke_user.id,
            resource_type="temp_object",
            resource_id=probe_id,
            actual_bytes=len(probe_payload),
            storage_key=probe_key,
        )
        downloaded = storage.read_bytes(probe_key)
        storage_roundtrip_ok = downloaded == probe_payload
    except Exception as exc:
        storage_error = str(exc)
    finally:
        try:
            storage.delete_key(probe_key)
        except Exception:
            pass
        try:
            release_reservations_for_resource(
                db, resource_type="temp_object", resource_id=probe_id
            )
        except Exception:
            pass

    budget = get_storage_budget_snapshot(db)
    return {
        "smoke_test_passed": bool(readiness["ready"] and storage_roundtrip_ok),
        "storage_roundtrip_ok": storage_roundtrip_ok,
        "storage_error": storage_error,
        "readiness": readiness,
        "storage_budget": budget,
        "public_uploads_blocked": bool(
            cfg.RESTRICTED_SMOKE_TEST_MODE or (cfg.REQUIRE_READINESS_FOR_UPLOADS and not readiness["ready"])
        ),
    }


@router.put("/admin/quotas")
def update_admin_quotas(
    payload: UpdateQuotasRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rec = db.query(SystemSetting).filter(SystemSetting.key == "quotas").first()
    val: Dict[str, Any] = payload.model_dump()
    if rec:
        rec.value_json = val
    else:
        rec = SystemSetting(key="quotas", value_json=val)
        db.add(rec)
    db.commit()
    return {"updated": True, "quotas": val}
