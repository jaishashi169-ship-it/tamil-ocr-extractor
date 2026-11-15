import io
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, or_

from app.celery_app import celery_app
from app.config import get_settings
from app.database import SessionLocal
from app.models import ExportArtifact, Job, PageResult, UploadSession, User
from app.security import detect_format_by_signature
from app.services.document_model import DocumentModel, PageModel, TextBlock, build_reviewed_document_model
from app.services.exporters.export_service import generate_export_bytes
from app.services.importers import CorruptFileError, PasswordRequiredError
from app.services.importers.docx_importer import import_docx
from app.services.importers.image_importer import import_image
from app.services.importers.pdf_importer import import_pdf
from app.services.importers.psd_importer import import_psd
from app.services.importers.text_importer import import_structured_or_text
from app.services.ocr_engine import OCROptions, OCRUnavailableError
from app.storage import storage


class StaleExecutionError(Exception):
    """Raised when a worker loses its execution lease/ownership token."""


def _mark_job_terminal_if_owned(db, job_id: str, execution_token: str, updates: dict) -> bool:
    """Update Job only if this worker still holds the active execution_token."""
    payload = dict(updates)
    payload[Job.updated_at] = datetime.now(timezone.utc)
    payload[Job.lease_expires_at] = None
    updated = (
        db.query(Job)
        .filter(Job.id == job_id, Job.execution_token == execution_token)
        .update(payload, synchronize_session=False)
    )
    db.commit()
    return updated > 0


def process_extraction_job_sync(
    job_id: str,
    password: Optional[str] = None,
    reclaim_stale: bool = False,  # Kept for signature compatibility; reclaim always requires an expired lease
) -> None:
    """
    Execute document extraction/OCR for a Job with atomic lease/ownership control.
    - Claims only 'queued' jobs, OR 'running' jobs whose lease_expires_at has expired.
    - Guards all progress updates, PageResult writes, and terminal transitions with execution_token.
    - Note: `password` is passed transiently in memory and NEVER persisted or logged.
    """
    start_ts = time.monotonic()
    settings = get_settings()
    execution_token = uuid.uuid4().hex
    now_utc = datetime.now(timezone.utc)
    lease_expires_at = now_utc + timedelta(seconds=settings.JOB_LEASE_SECONDS)

    db = SessionLocal()
    try:
        claim_filter = and_(
            Job.id == job_id,
            or_(
                Job.status == "queued",
                and_(
                    Job.status == "running",
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now_utc),
                ),
            ),
        )
        claimed = (
            db.query(Job)
            .filter(claim_filter)
            .update(
                {
                    Job.status: "running",
                    Job.execution_token: execution_token,
                    Job.lease_expires_at: lease_expires_at,
                    Job.heartbeat_at: now_utc,
                    Job.progress_percent: 5.0,
                    Job.stage_label: "Preparing file",
                    Job.error_message: None,
                    Job.needs_password: False,
                    Job.updated_at: now_utc,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        if claimed == 0:
            return

        job = db.query(Job).filter(Job.id == job_id, Job.execution_token == execution_token).first()
        if not job or job.status == "cancelled":
            return

        settings_dict = job.settings_json or {}
        ocr_opts = OCROptions(
            language=settings_dict.get("language", "tam+eng"),
            auto_rotate=bool(settings_dict.get("auto_rotate", True)),
            deskew=bool(settings_dict.get("deskew", True)),
            denoise=bool(settings_dict.get("denoise", False)),
            enhance_contrast=bool(settings_dict.get("enhance_contrast", True)),
            multi_column=bool(settings_dict.get("multi_column", True)),
        )

        file_path = storage.get_local_file(job.original_storage_key)

        def _on_progress(current_page: int, total_pages: int, label: str) -> None:
            hb_now = datetime.now(timezone.utc)
            new_lease = hb_now + timedelta(seconds=settings.JOB_LEASE_SECONDS)
            pct = round(10.0 + (current_page / max(1, total_pages)) * 85.0, 1)
            updated = (
                db.query(Job)
                .filter(
                    Job.id == job_id,
                    Job.execution_token == execution_token,
                    Job.status == "running",
                )
                .update(
                    {
                        Job.page_count: total_pages,
                        Job.processed_pages: current_page,
                        Job.progress_percent: pct,
                        Job.stage_label: label,
                        Job.heartbeat_at: hb_now,
                        Job.lease_expires_at: new_lease,
                        Job.updated_at: hb_now,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated == 0:
                current = db.query(Job).filter(Job.id == job_id).first()
                if current and current.status == "cancelled":
                    raise InterruptedError("Job cancelled by user")
                raise StaleExecutionError("Execution lease lost to a newer worker")

        fmt = job.format_type.lower()
        doc_model: DocumentModel
        if fmt == "pdf":
            doc_model = import_pdf(file_path, job.id, job.original_filename, ocr_opts, password=password, progress_cb=_on_progress)
        elif fmt in {"docx", "doc"}:
            doc_model = import_docx(file_path, job.id, job.original_filename, is_legacy_doc=(fmt == "doc"), progress_cb=_on_progress)
        elif fmt in {"png", "jpg", "jpeg", "webp", "tiff", "bmp"}:
            doc_model = import_image(file_path, job.id, job.original_filename, fmt, ocr_opts, progress_cb=_on_progress)
        elif fmt == "psd":
            doc_model = import_psd(file_path, job.id, job.original_filename, ocr_opts, progress_cb=_on_progress)
        elif fmt in {"txt", "md", "html", "csv", "json"}:
            doc_model = import_structured_or_text(file_path, job.id, job.original_filename, fmt, progress_cb=_on_progress)
        else:
            raise ValueError(f"Unsupported format '{fmt}'.")

        # Verify ownership before mutating PageResult rows or marking completed
        still_owner = (
            db.query(Job)
            .filter(Job.id == job_id, Job.execution_token == execution_token, Job.status == "running")
            .first()
        )
        if not still_owner:
            db.rollback()
            return

        # Clear old page results if retrying
        db.query(PageResult).filter(PageResult.job_id == job.id).delete(synchronize_session=False)

        for page in doc_model.pages:
            plain = page.to_plain_text()
            pr = PageResult(
                job_id=job.id,
                page_number=page.page_number,
                source_type=page.source_type,
                extraction_method=page.extraction_method,
                language_detected=page.language_detected,
                confidence_avg=page.confidence_avg,
                raw_text=plain,
                edited_text=None,
                model_json=page.model_dump(mode="json"),
                thumbnail_key=(page.metadata or {}).get("thumbnail_key"),
                preview_key=(page.metadata or {}).get("preview_key"),
            )
            db.add(pr)

        done_now = datetime.now(timezone.utc)
        completed_rows = (
            db.query(Job)
            .filter(Job.id == job_id, Job.execution_token == execution_token, Job.status == "running")
            .update(
                {
                    Job.page_count: len(doc_model.pages),
                    Job.processed_pages: len(doc_model.pages),
                    Job.metadata_json: {**(doc_model.metadata or {}), "warnings": doc_model.warnings},
                    Job.status: "completed",
                    Job.progress_percent: 100.0,
                    Job.stage_label: "Completed",
                    Job.duration_ms: int((time.monotonic() - start_ts) * 1000),
                    Job.lease_expires_at: None,
                    Job.heartbeat_at: done_now,
                    Job.updated_at: done_now,
                },
                synchronize_session=False,
            )
        )
        if completed_rows == 0:
            db.rollback()
            return
        db.commit()

    except StaleExecutionError:
        db.rollback()
        return
    except InterruptedError:
        db.rollback()
        _mark_job_terminal_if_owned(
            db, job_id, execution_token,
            {Job.status: "cancelled", Job.stage_label: "Cancelled"},
        )
    except PasswordRequiredError as exc:
        db.rollback()
        _mark_job_terminal_if_owned(
            db, job_id, execution_token,
            {
                Job.status: "failed",
                Job.needs_password: True,
                Job.stage_label: "Password required",
                Job.error_message: str(exc),
            },
        )
    except OCRUnavailableError as exc:
        db.rollback()
        _mark_job_terminal_if_owned(
            db, job_id, execution_token,
            {
                Job.status: "failed",
                Job.stage_label: "OCR service unavailable",
                Job.error_message: str(exc),
                Job.duration_ms: int((time.monotonic() - start_ts) * 1000),
            },
        )
    except (CorruptFileError, ValueError) as exc:
        db.rollback()
        _mark_job_terminal_if_owned(
            db, job_id, execution_token,
            {
                Job.status: "failed",
                Job.stage_label: "Failed",
                Job.error_message: str(exc),
                Job.duration_ms: int((time.monotonic() - start_ts) * 1000),
            },
        )
    except Exception as exc:
        db.rollback()
        _mark_job_terminal_if_owned(
            db, job_id, execution_token,
            {
                Job.status: "failed",
                Job.stage_label: "Failed",
                Job.error_message: f"Processing error: {type(exc).__name__}",
                Job.duration_ms: int((time.monotonic() - start_ts) * 1000),
            },
        )
    finally:
        db.close()


@celery_app.task(bind=True, name="app.tasks.process_extraction_job")
def process_extraction_job_task(self, job_id: str, password: Optional[str] = None) -> None:
    process_extraction_job_sync(job_id, password=password)


def finalize_upload_sync(session_id: str) -> None:
    """
    Durable worker finalization for uploads:
    - Completes S3 multipart or assembles local chunks.
    - Enforces 500 MB size limit and magic-byte content signature.
    - Computes streaming SHA-256 outside FastAPI request handlers.
    - Marks session 'completed' only after all validation succeeds.
    """
    settings = get_settings()
    db = SessionLocal()
    try:
        session = db.query(UploadSession).filter(UploadSession.id == session_id).first()
        if not session or session.status in {"completed", "aborted"}:
            return

        if session.s3_upload_id and session.storage_key and storage.is_s3():
            dest_key = session.storage_key
            parts = sorted(session.s3_parts_json or [], key=lambda p: p.get("PartNumber", 0))
            expected_parts = list(range(1, session.total_chunks + 1))
            if [p.get("PartNumber") for p in parts] != expected_parts:
                session.status = "failed"
                db.commit()
                return

            storage.complete_multipart_upload(dest_key, session.s3_upload_id, parts)
            actual_bytes = storage.head_object_size(dest_key)
            if actual_bytes > settings.MAX_UPLOAD_BYTES:
                storage.delete_key(dest_key)
                session.status = "aborted"
                db.commit()
                return

            header = storage.read_head_bytes(dest_key, 4096)
            fmt, err = detect_format_by_signature(header, session.filename)
            if err or not fmt:
                storage.delete_key(dest_key)
                session.status = "aborted"
                db.commit()
                return

            _, sha256_hex = storage.compute_sha256_streaming(dest_key)
        else:
            dest_key = f"uploads/{session.user_id}/{session.id}/{session.filename}"
            actual_bytes, sha256_hex = storage.assemble_chunks(session.id, session.total_chunks, dest_key)
            if actual_bytes > settings.MAX_UPLOAD_BYTES:
                storage.delete_key(dest_key)
                session.status = "aborted"
                db.commit()
                return

            assembled_path = storage.get_local_file(dest_key)
            with open(assembled_path, "rb") as f:
                header = f.read(4096)
            fmt, err = detect_format_by_signature(header, session.filename)
            if err or not fmt:
                storage.delete_key(dest_key)
                session.status = "aborted"
                db.commit()
                return

        session.storage_key = dest_key
        session.sha256_hash = sha256_hex
        session.format_type = fmt
        session.expected_size_bytes = actual_bytes
        session.status = "completed"
        db.commit()
    except Exception:
        db.rollback()
        session = db.query(UploadSession).filter(UploadSession.id == session_id).first()
        if session and session.status == "processing":
            session.status = "failed"
            db.commit()
    finally:
        db.close()


@celery_app.task(bind=True, name="app.tasks.finalize_upload")
def finalize_upload_task(self, session_id: str) -> None:
    finalize_upload_sync(session_id)


def generate_export_sync(artifact_id: str, options: Optional[Dict[str, Any]] = None) -> None:
    """Generate a single-job export artifact in a worker and upload to private storage."""
    db = SessionLocal()
    try:
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if not art or art.status == "completed":
            return
        job = db.query(Job).filter(Job.id == art.job_id).first()
        user = db.query(User).filter(User.id == art.user_id).first()
        if not job or job.status != "completed":
            art.status = "failed"
            db.commit()
            return

        doc_model = build_reviewed_document_model(job, db)
        data_bytes, ext, warnings, baseline_bytes, savings_pct = generate_export_bytes(
            doc_model, art.export_format, art.quality_preset, options or {}
        )
        if not data_bytes:
            art.status = "failed"
            db.commit()
            return

        stem = Path(job.original_filename).stem
        storage_key = f"jobs/{job.id}/exports/{stem}_{art.export_format}_{art.quality_preset}_{art.id[:8]}.{ext}"
        storage.save_bytes(storage_key, data_bytes)

        retention_days = user.retention_days if user else get_settings().DEFAULT_RETENTION_DAYS
        art.storage_key = storage_key
        art.file_size_bytes = len(data_bytes)
        art.uncompressed_size_bytes = baseline_bytes
        art.savings_percent = savings_pct
        art.warnings_json = warnings
        art.status = "completed"
        art.expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days)
        db.commit()
    except Exception:
        db.rollback()
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if art:
            art.status = "failed"
            db.commit()
    finally:
        db.close()


@celery_app.task(bind=True, name="app.tasks.generate_export")
def generate_export_task(self, artifact_id: str, options: Optional[Dict[str, Any]] = None) -> None:
    generate_export_sync(artifact_id, options=options)


def generate_batch_zip_sync(
    artifact_id: str,
    user_id: str,
    job_ids: List[str],
    export_format: str,
    quality_preset: str,
) -> None:
    """Generate a multi-job ZIP archive in a worker and upload to private storage."""
    db = SessionLocal()
    try:
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if not art or art.status == "completed":
            return
        jobs = (
            db.query(Job)
            .filter(Job.id.in_(job_ids), Job.user_id == user_id, Job.status == "completed")
            .all()
        )
        if not jobs:
            art.status = "failed"
            db.commit()
            return

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for job in jobs:
                doc_model = build_reviewed_document_model(job, db)
                data_bytes, ext, _, _, _ = generate_export_bytes(doc_model, export_format, quality_preset, {})
                stem = Path(job.original_filename).stem
                zf.writestr(f"{stem}_{job.id[:6]}.{ext}", data_bytes)

        zip_bytes = buf.getvalue()
        key = f"batches/{user_id}/batch_{art.id[:8]}.zip"
        storage.save_bytes(key, zip_bytes)

        art.storage_key = key
        art.file_size_bytes = len(zip_bytes)
        art.uncompressed_size_bytes = len(zip_bytes)
        art.savings_percent = 0.0
        art.status = "completed"
        db.commit()
    except Exception:
        db.rollback()
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if art:
            art.status = "failed"
            db.commit()
    finally:
        db.close()


@celery_app.task(bind=True, name="app.tasks.generate_batch_zip")
def generate_batch_zip_task(
    self,
    artifact_id: str,
    user_id: str,
    job_ids: List[str],
    export_format: str,
    quality_preset: str,
) -> None:
    generate_batch_zip_sync(artifact_id, user_id, job_ids, export_format, quality_preset)


WORKER_STATUS_REDIS_KEY = "doc_studio:worker:status"
_cached_worker_ocr_probe: Optional[Dict[str, Any]] = None


def collect_worker_runtime_status(force_reprobe: bool = False) -> Dict[str, Any]:
    """Run inside the Celery worker to verify Tesseract languages, registered tasks, and real bilingual Tamil-English OCR."""
    global _cached_worker_ocr_probe
    if _cached_worker_ocr_probe is None or force_reprobe:
        from app.services.ocr_engine import get_ocr_status, ocr_pil_image
        from PIL import Image, ImageDraw, ImageFont

        ocr_info = get_ocr_status()
        langs = ocr_info.get("languages", [])
        bilingual_smoke_passed = False
        tamil_unicode_detected = False
        smoke_sample = ""
        if ocr_info.get("available") and "eng" in langs:
            try:
                img = Image.new("RGB", (760, 180), color=(255, 255, 255))
                draw = ImageDraw.Draw(img)
                tamil_font = None
                for candidate in (
                    "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",
                    "/usr/share/fonts/truetype/noto/NotoSansTamil-Bold.ttf",
                    "C:/Windows/Fonts/Nirmala.ttf",
                    "C:/Windows/Fonts/latha.ttf",
                ):
                    if Path(candidate).exists():
                        tamil_font = ImageFont.truetype(candidate, 32)
                        break
                if tamil_font is not None and "tam" in langs:
                    draw.text((24, 28), "தமிழ் ஆவணம்", fill=(0, 0, 0), font=tamil_font)
                    draw.text((24, 96), "TAMIL OCR STUDIO 2026", fill=(0, 0, 0), font=tamil_font)
                else:
                    draw.text((24, 60), "TAMIL OCR STUDIO 2026", fill=(0, 0, 0))
                res = ocr_pil_image(img, OCROptions(language="tam+eng" if "tam" in langs else "eng"))
                smoke_sample = (res.get("text") or "").strip()
                tamil_unicode_detected = any("\u0B80" <= ch <= "\u0BFF" for ch in smoke_sample)
                bilingual_smoke_passed = (
                    ("OCR" in smoke_sample.upper() or "TAMIL" in smoke_sample.upper() or "2026" in smoke_sample)
                    and (tamil_unicode_detected or tamil_font is None)
                )
            except Exception:
                bilingual_smoke_passed = False

        registered = sorted(t for t in celery_app.tasks.keys() if t.startswith("app.tasks."))
        _cached_worker_ocr_probe = {
            "available": bool(ocr_info.get("available") and "tam" in langs and "eng" in langs and bilingual_smoke_passed),
            "tesseract_available": bool(ocr_info.get("available")),
            "languages": langs,
            "bilingual_ocr_verified": bilingual_smoke_passed,
            "tamil_unicode_verified": tamil_unicode_detected,
            "smoke_sample": smoke_sample,
            "registered_tasks": registered,
        }
    return {
        **_cached_worker_ocr_probe,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def publish_worker_heartbeat(force_reprobe: bool = False) -> Optional[Dict[str, Any]]:
    import json
    import redis
    from app.dispatch import _redis_kwargs_for_url

    settings = get_settings()
    try:
        payload = collect_worker_runtime_status(force_reprobe=force_reprobe)
        client = redis.from_url(
            settings.CELERY_BROKER_URL,
            **_redis_kwargs_for_url(
                settings.CELERY_BROKER_URL,
                settings.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
                settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            ),
        )
        client.set(WORKER_STATUS_REDIS_KEY, json.dumps(payload), ex=180)
        return payload
    except Exception:
        return None


try:
    from celery.signals import heartbeat_sent, worker_ready

    @worker_ready.connect
    def _on_worker_ready(**_kwargs: Any) -> None:
        publish_worker_heartbeat(force_reprobe=True)

    @heartbeat_sent.connect
    def _on_worker_heartbeat(**_kwargs: Any) -> None:
        publish_worker_heartbeat(force_reprobe=False)
except Exception:
    pass
