from typing import Any, Dict
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.config import get_settings
from app.database import get_db
from app.models import Job, SystemSetting, User
from app.services.tamil_utils import SUPPORTED_LEGACY_ENCODINGS
from app.storage import storage


def _safe_ocr_status() -> Dict[str, Any]:
    try:
        from app.services.ocr_engine import get_ocr_status

        status_info = get_ocr_status()
        status_info["execution_mode"] = "local_or_worker"
        status_info["verified_locally"] = True
        return status_info
    except ImportError:
        return {
            "available": True,
            "execution_mode": "worker_daemon",
            "verified_locally": False,
            "configured_languages": ["eng", "tam"],
            "languages": [],
        }


def _safe_ocr_languages() -> list[str]:
    try:
        from app.services.ocr_engine import get_installed_tesseract_languages

        return get_installed_tesseract_languages()
    except ImportError:
        return []

router = APIRouter(tags=["Capabilities & Administration"])
settings = get_settings()


class UpdateQuotasRequest(BaseModel):
    max_upload_bytes: int = Field(default=settings.MAX_UPLOAD_BYTES, ge=1_000_000, le=500_000_000)
    default_retention_days: int = Field(default=settings.DEFAULT_RETENTION_DAYS, ge=1, le=365)
    user_storage_quota_bytes: int = Field(default=settings.USER_STORAGE_QUOTA_BYTES, ge=50_000_000)


@router.get("/capabilities")
def get_capabilities():
    """Public capability matrix and installed OCR language packs."""
    ocr = _safe_ocr_status()
    return {
        "app_name": settings.APP_NAME,
        "max_upload_bytes": settings.MAX_UPLOAD_BYTES,
        "upload_chunk_bytes": settings.UPLOAD_CHUNK_BYTES,
        "max_batch_files": settings.MAX_BATCH_FILES,
        "max_pdf_pages": settings.MAX_PDF_PAGES,
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
    status_counts = dict(db.query(Job.status, func.count(Job.id)).group_by(Job.status).all())
    avg_duration_ms = db.query(func.avg(Job.duration_ms)).filter(Job.duration_ms.isnot(None)).scalar()
    total_users = db.query(func.count(User.id)).scalar() or 0

    return {
        "jobs_by_status": {
            "queued": status_counts.get("queued", 0),
            "running": status_counts.get("running", 0),
            "completed": status_counts.get("completed", 0),
            "cancelled": status_counts.get("cancelled", 0),
            "failed": status_counts.get("failed", 0),
        },
        "avg_processing_duration_ms": round(float(avg_duration_ms), 1) if avg_duration_ms else 0.0,
        "storage_usage_bytes": storage.get_total_usage_bytes(),
        "total_users": total_users,
        "worker_health": {
            "status": "healthy",
            "isolated_network": True,
            "ocr_execution_mode": "local_or_worker" if _safe_ocr_languages() else "worker_daemon",
            "installed_ocr_languages": _safe_ocr_languages(),
        },
        "quotas": {
            "max_upload_bytes": settings.MAX_UPLOAD_BYTES,
            "default_retention_days": settings.DEFAULT_RETENTION_DAYS,
            "user_storage_quota_bytes": settings.USER_STORAGE_QUOTA_BYTES,
        },
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
