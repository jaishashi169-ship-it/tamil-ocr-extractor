import io
import logging
import threading
import time

logger = logging.getLogger(__name__)
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
from app.services.retention import (
    StorageQuotaExceededError,
    cleanup_expired_resources,
    commit_storage_reservation,
    release_all_job_storage_and_reservations,
    release_reservations_for_resource,
)
from app.storage import bind_storage_hooks, storage


class StaleExecutionError(Exception):
    """Raised when a worker loses its execution lease/ownership token."""


def _mark_job_terminal_if_owned(db, job_id: str, execution_token: str, updates: dict) -> bool:
    """Update Job only if this worker still holds the active execution_token, with retries on transient locks."""
    payload = dict(updates)
    payload[Job.updated_at] = datetime.now(timezone.utc)
    payload[Job.lease_expires_at] = None
    for attempt in range(4):
        try:
            updated = (
                db.query(Job)
                .filter(Job.id == job_id, Job.execution_token == execution_token)
                .update(payload, synchronize_session=False)
            )
            db.commit()
            return updated > 0
        except Exception as exc:
            db.rollback()
            if attempt < 3:
                time.sleep(0.25 * (attempt + 1))
            else:
                logger.error("Failed to mark job %s terminal after retries: %s", job_id, exc)
                return False
    return False


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
    original_key_for_scratch_cleanup: Optional[str] = None
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
            document_type=str(settings_dict.get("document_type", "printed")),
            engine=str(settings_dict.get("engine", "auto")),
            force_ocr=bool(settings_dict.get("force_ocr", False)),
        )

        original_key_for_scratch_cleanup = job.original_storage_key
        file_path = storage.get_local_file(job.original_storage_key)

        if job.file_sha256:
            import hashlib

            h = hashlib.sha256()
            with open(file_path, "rb") as _vf:
                while True:
                    _b = _vf.read(1024 * 1024)
                    if not _b:
                        break
                    h.update(_b)
            if h.hexdigest() != job.file_sha256:
                raise CorruptFileError("Original file integrity check (SHA-256) failed; file may have been modified.")

        _last_progress_mono = 0.0

        def _on_progress(current_page: int, total_pages: int, label: str) -> None:
            nonlocal _last_progress_mono
            now_mono = time.monotonic()
            is_first = (current_page == 1)
            is_last = (current_page == total_pages)
            if not is_first and not is_last and (now_mono - _last_progress_mono < 0.4) and (current_page % 5 != 0):
                return
            _last_progress_mono = now_mono

            hb_now = datetime.now(timezone.utc)
            new_lease = hb_now + timedelta(seconds=settings.JOB_LEASE_SECONDS)
            pct = round(10.0 + (current_page / max(1, total_pages)) * 85.0, 1)
            try:
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
            except (InterruptedError, StaleExecutionError):
                raise
            except Exception as pe:
                db.rollback()
                logger.warning("Transient error updating job %s progress: %s", job_id, pe)

        fmt = job.format_type.lower()
        doc_model: DocumentModel
        job_user_id = job.user_id
        job_expires_at = job.expires_at

        def _on_job_storage_write(clean_key: str, byte_size: int) -> None:
            rtype = "temp_object" if "/tmp/" in clean_key else "preview"
            commit_storage_reservation(
                db,
                user_id=job_user_id,
                resource_type=rtype,
                resource_id=f"{job_id}:{clean_key}",
                actual_bytes=byte_size,
                storage_key=clean_key,
                expires_at=job_expires_at,
                commit=True,
            )

        def _on_job_storage_delete(clean_key: str) -> None:
            release_reservations_for_resource(
                db,
                resource_id=f"{job_id}:{clean_key}",
                commit=True,
            )

        with bind_storage_hooks(on_write=_on_job_storage_write, on_delete=_on_job_storage_delete):
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

        # Clear old page results if retrying and save new page results with retry on lock
        for save_attempt in range(4):
            try:
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
                break
            except Exception as se:
                db.rollback()
                if save_attempt < 3:
                    time.sleep(0.3 * (save_attempt + 1))
                else:
                    raise

    except StaleExecutionError:
        db.rollback()
        return
    except InterruptedError:
        db.rollback()
        job_row = db.query(Job).filter(Job.id == job_id).first()
        if job_row:
            release_all_job_storage_and_reservations(db, job_row, delete_original=False, commit=True)
        _mark_job_terminal_if_owned(
            db, job_id, execution_token,
            {Job.status: "cancelled", Job.stage_label: "Cancelled"},
        )
    except StorageQuotaExceededError as exc:
        db.rollback()
        job_row = db.query(Job).filter(Job.id == job_id).first()
        if job_row:
            release_all_job_storage_and_reservations(db, job_row, delete_original=False, commit=True)
        _mark_job_terminal_if_owned(
            db, job_id, execution_token,
            {
                Job.status: "failed",
                Job.stage_label: "Storage budget exceeded",
                Job.error_message: str(exc),
                Job.duration_ms: int((time.monotonic() - start_ts) * 1000),
            },
        )
    except PasswordRequiredError as exc:
        db.rollback()
        job_row = db.query(Job).filter(Job.id == job_id).first()
        if job_row:
            release_all_job_storage_and_reservations(db, job_row, delete_original=False, commit=True)
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
        job_row = db.query(Job).filter(Job.id == job_id).first()
        if job_row:
            release_all_job_storage_and_reservations(db, job_row, delete_original=False, commit=True)
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
        job_row = db.query(Job).filter(Job.id == job_id).first()
        if job_row:
            release_all_job_storage_and_reservations(db, job_row, delete_original=False, commit=True)
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
        job_row = db.query(Job).filter(Job.id == job_id).first()
        if job_row:
            release_all_job_storage_and_reservations(db, job_row, delete_original=False, commit=True)
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
        storage.cleanup_scratch_key(original_key_for_scratch_cleanup)
        db.close()


@celery_app.task(bind=True, name="app.tasks.process_extraction_job")
def process_extraction_job_task(self, job_id: str, password: Optional[str] = None) -> None:
    process_extraction_job_sync(job_id, password=password)


def finalize_upload_sync(session_id: str) -> None:
    """
    Durable worker finalization for uploads:
    - Completes S3 multipart or verifies official Vercel Blob upload or assembles local chunks.
    - Enforces 500 MB size limit, magic-byte content signature, and atomic storage reservation commit.
    - Computes streaming SHA-256 outside FastAPI request handlers.
    - Marks session 'completed' only after all validation succeeds; releases reservation on failure.
    """
    settings = get_settings()
    db = SessionLocal()
    original_key_for_scratch_cleanup: Optional[str] = None
    try:
        q = db.query(UploadSession).filter(UploadSession.id == session_id)
        dialect = db.bind.dialect.name if db.bind is not None else ""
        if dialect == "postgresql":
            q = q.with_for_update()
        session = q.first()
        if not session or session.status in {"completed", "aborted", "failed"} or session.sha256_hash:
            return

        if session.s3_upload_id and session.storage_key and storage.is_direct_multipart():
            dest_key = session.storage_key
            if session.s3_upload_id != "vercel-blob-sdk-managed":
                parts = sorted(session.s3_parts_json or [], key=lambda p: p.get("PartNumber", 0))
                expected_parts = list(range(1, session.total_chunks + 1))
                if [p.get("PartNumber") for p in parts] != expected_parts:
                    release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
                    session.status = "failed"
                    db.commit()
                    return
                storage.complete_multipart_upload(dest_key, session.s3_upload_id, parts)

            actual_bytes = storage.head_object_size(dest_key)
            if actual_bytes > settings.MAX_UPLOAD_BYTES:
                storage.delete_key(dest_key)
                release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
                session.status = "aborted"
                db.commit()
                return

            header = storage.read_head_bytes(dest_key, 4096)
            fmt, err = detect_format_by_signature(header, session.filename)
            if err or not fmt:
                storage.delete_key(dest_key)
                release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
                session.status = "aborted"
                db.commit()
                return

            _, sha256_hex = storage.compute_sha256_streaming(dest_key)
        else:
            dest_key = f"uploads/{session.user_id}/{session.id}/{session.filename}"
            original_key_for_scratch_cleanup = dest_key
            actual_bytes, sha256_hex = storage.assemble_chunks(session.id, session.total_chunks, dest_key)
            if actual_bytes > settings.MAX_UPLOAD_BYTES:
                storage.delete_key(dest_key)
                release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
                session.status = "aborted"
                db.commit()
                return

            assembled_path = storage.get_local_file(dest_key)
            with open(assembled_path, "rb") as f:
                header = f.read(4096)
            fmt, err = detect_format_by_signature(header, session.filename)
            if err or not fmt:
                storage.delete_key(dest_key)
                release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
                session.status = "aborted"
                db.commit()
                return

        try:
            commit_storage_reservation(
                db,
                user_id=session.user_id,
                resource_type="upload",
                resource_id=session.id,
                actual_bytes=actual_bytes,
                storage_key=dest_key,
                commit=False,
            )
        except StorageQuotaExceededError:
            storage.delete_key(dest_key)
            release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
            session.status = "aborted"
            db.commit()
            return

        finalized_rows = (
            db.query(UploadSession)
            .filter(
                UploadSession.id == session.id,
                UploadSession.sha256_hash.is_(None),
                UploadSession.status.in_(["uploading", "processing"]),
            )
            .update(
                {
                    UploadSession.storage_key: dest_key,
                    UploadSession.sha256_hash: sha256_hex,
                    UploadSession.format_type: fmt,
                    UploadSession.expected_size_bytes: actual_bytes,
                    UploadSession.status: "completed",
                },
                synchronize_session=False,
            )
        )
        if finalized_rows == 0:
            db.rollback()
            return
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Upload finalization failed for session %s: %s", session_id, exc)
        session = db.query(UploadSession).filter(UploadSession.id == session_id).first()
        # Never delete an original that another finalizer already accepted (sha256 recorded).
        if session and session.status == "processing" and not session.sha256_hash:
            if session.storage_key:
                storage.delete_key(session.storage_key)
            release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
            session.status = "failed"
            db.commit()
    finally:
        storage.cleanup_scratch_key(original_key_for_scratch_cleanup)
        db.close()


@celery_app.task(bind=True, name="app.tasks.finalize_upload")
def finalize_upload_task(self, session_id: str) -> None:
    finalize_upload_sync(session_id)


def generate_export_sync(artifact_id: str, options: Optional[Dict[str, Any]] = None) -> None:
    """Generate a single-job export artifact in a worker, enforce atomic storage reservation, and upload."""
    db = SessionLocal()
    original_key_for_scratch_cleanup: Optional[str] = None
    try:
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if not art or art.status == "completed":
            return
        job = db.query(Job).filter(Job.id == art.job_id).first()
        user = db.query(User).filter(User.id == art.user_id).first()
        if not job or job.status != "completed":
            release_reservations_for_resource(db, resource_type="export", resource_id=art.id, commit=False)
            art.status = "failed"
            db.commit()
            return

        doc_model = build_reviewed_document_model(job, db)
        data_bytes, ext, warnings, baseline_bytes, savings_pct = generate_export_bytes(
            doc_model, art.export_format, art.quality_preset, options or {}
        )
        if not data_bytes:
            release_reservations_for_resource(db, resource_type="export", resource_id=art.id, commit=False)
            art.status = "failed"
            db.commit()
            return

        retention_days = user.retention_days if user else get_settings().DEFAULT_RETENTION_DAYS
        expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days)
        stem = Path(job.original_filename).stem
        storage_key = f"jobs/{job.id}/exports/{stem}_{art.export_format}_{art.quality_preset}_{art.id[:8]}.{ext}"

        # Enforce atomic storage capacity BEFORE writing export bytes to durable storage
        commit_storage_reservation(
            db,
            user_id=art.user_id,
            resource_type="export",
            resource_id=art.id,
            actual_bytes=len(data_bytes),
            storage_key=storage_key,
            expires_at=expires_at,
            commit=True,
        )

        storage.save_bytes(storage_key, data_bytes)

        art.storage_key = storage_key
        art.file_size_bytes = len(data_bytes)
        art.uncompressed_size_bytes = baseline_bytes
        art.savings_percent = savings_pct
        art.warnings_json = warnings
        art.status = "completed"
        art.expires_at = expires_at
        db.commit()
    except StorageQuotaExceededError as exc:
        db.rollback()
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if art:
            release_reservations_for_resource(db, resource_type="export", resource_id=art.id, commit=False)
            art.status = "failed"
            art.warnings_json = [str(exc)]
            db.commit()
    except Exception:
        db.rollback()
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if art:
            release_reservations_for_resource(db, resource_type="export", resource_id=art.id, commit=False)
            art.status = "failed"
            db.commit()
    finally:
        storage.cleanup_scratch_key(original_key_for_scratch_cleanup)
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
    """Generate a multi-job ZIP archive in a worker, enforce atomic storage reservation, and upload."""
    db = SessionLocal()
    original_key_for_scratch_cleanup: Optional[str] = None
    try:
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if not art or art.status == "completed":
            return
        user = db.query(User).filter(User.id == user_id).first()
        is_guest_or_admin = (user.email.endswith("@studio.local") or user.role in ("guest", "admin")) if user else True
        if is_guest_or_admin:
            jobs = db.query(Job).filter(Job.id.in_(job_ids), Job.status == "completed").all()
        else:
            jobs = (
                db.query(Job)
                .filter(Job.id.in_(job_ids), Job.user_id == user_id, Job.status == "completed")
                .all()
            )
        if not jobs:
            release_reservations_for_resource(db, resource_type="export", resource_id=art.id, commit=False)
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
        user = db.query(User).filter(User.id == user_id).first()
        retention_days = user.retention_days if user else get_settings().DEFAULT_RETENTION_DAYS
        expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days)
        key = f"batches/{user_id}/batch_{art.id[:8]}.zip"

        # Enforce atomic storage capacity BEFORE writing ZIP bytes to durable storage
        commit_storage_reservation(
            db,
            user_id=user_id,
            resource_type="export",
            resource_id=art.id,
            actual_bytes=len(zip_bytes),
            storage_key=key,
            expires_at=expires_at,
            commit=True,
        )

        storage.save_bytes(key, zip_bytes)

        art.storage_key = key
        art.file_size_bytes = len(zip_bytes)
        art.uncompressed_size_bytes = len(zip_bytes)
        art.savings_percent = 0.0
        art.status = "completed"
        art.expires_at = expires_at
        db.commit()
    except StorageQuotaExceededError as exc:
        db.rollback()
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if art:
            release_reservations_for_resource(db, resource_type="export", resource_id=art.id, commit=False)
            art.status = "failed"
            art.warnings_json = [str(exc)]
            db.commit()
    except Exception:
        db.rollback()
        art = db.query(ExportArtifact).filter(ExportArtifact.id == artifact_id).first()
        if art:
            release_reservations_for_resource(db, resource_type="export", resource_id=art.id, commit=False)
            art.status = "failed"
            db.commit()
    finally:
        storage.cleanup_scratch_key(original_key_for_scratch_cleanup)
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


def _levenshtein_distance(seq_a: List[Any], seq_b: List[Any]) -> int:
    """Compute standard Levenshtein edit distance (substitutions + insertions + deletions)."""
    if not seq_a:
        return len(seq_b)
    if not seq_b:
        return len(seq_a)
    prev = list(range(len(seq_b) + 1))
    for i, item_a in enumerate(seq_a, start=1):
        curr = [i] + [0] * len(seq_b)
        for j, item_b in enumerate(seq_b, start=1):
            cost = 0 if item_a == item_b else 1
            curr[j] = min(
                prev[j] + 1,      # deletion
                curr[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    return prev[-1]


def compute_cer_and_wer(actual: str, expected: str) -> Dict[str, float]:
    """
    Compute standard edit-distance Character Error Rate (CER) and Word Error Rate (WER):
    - CER = (S_c + D_c + I_c) / N_c
    - WER = (S_w + D_w + I_w) / N_w
    """
    import unicodedata

    def _normalize_text(val: str) -> str:
        norm = unicodedata.normalize("NFC", val or "")
        cleaned = "".join(ch for ch in norm if ch not in ("\u200c", "\u200d"))
        return " ".join(cleaned.split()).upper()

    hyp = _normalize_text(actual)
    ref = _normalize_text(expected)
    if not ref:
        return {"cer": 0.0, "wer": 0.0, "char_accuracy": 1.0, "word_accuracy": 1.0}

    char_dist = _levenshtein_distance(list(hyp), list(ref))
    cer = round(char_dist / max(1, len(ref)), 4)

    hyp_words = hyp.split() if hyp else []
    ref_words = ref.split()
    word_dist = _levenshtein_distance(hyp_words, ref_words)
    wer = round(word_dist / max(1, len(ref_words)), 4)

    return {
        "cer": cer,
        "wer": wer,
        "char_accuracy": round(max(0.0, 1.0 - cer), 4),
        "word_accuracy": round(max(0.0, 1.0 - wer), 4),
    }


def collect_worker_runtime_status(force_reprobe: bool = False) -> Dict[str, Any]:
    """
    Run inside the Celery worker to execute a synthetic OCR execution smoke test
    and evaluate CER/WER via Levenshtein edit distance against reference transcripts.
    """
    global _cached_worker_ocr_probe
    if _cached_worker_ocr_probe is None or force_reprobe:
        import tempfile
        from app.services.ocr_engine import get_ocr_status, run_ocr_on_image
        from PIL import Image, ImageDraw, ImageFont

        ocr_info = get_ocr_status()
        langs = ocr_info.get("languages", [])
        expected_tamil = "தமிழ் ஆவணம்"
        expected_english = "TAMIL OCR STUDIO 2026"
        smoke_sample = ""
        extracted_tamil_line = ""
        extracted_english_line = ""
        tamil_metrics = {"cer": 1.0, "wer": 1.0, "char_accuracy": 0.0, "word_accuracy": 0.0}
        english_metrics = {"cer": 1.0, "wer": 1.0, "char_accuracy": 0.0, "word_accuracy": 0.0}
        smoke_passed = False

        if ocr_info.get("available") and "eng" in langs:
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    img_path = Path(tmpdir) / "bilingual_smoke.png"
                    img = Image.new("RGB", (960, 240), color=(255, 255, 255))
                    draw = ImageDraw.Draw(img)
                    tamil_font = None
                    latin_font = None
                    for candidate in (
                        "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",
                        "/usr/share/fonts/truetype/noto/NotoSansTamil-Bold.ttf",
                        "C:/Windows/Fonts/Nirmala.ttf",
                        "C:/Windows/Fonts/latha.ttf",
                    ):
                        if Path(candidate).exists():
                            tamil_font = ImageFont.truetype(candidate, 36)
                            break
                    for candidate in (
                        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                        "C:/Windows/Fonts/arial.ttf",
                        "C:/Windows/Fonts/Nirmala.ttf",
                    ):
                        if Path(candidate).exists():
                            latin_font = ImageFont.truetype(candidate, 36)
                            break

                    if tamil_font is not None and "tam" in langs:
                        draw.text((32, 32), expected_tamil, fill=(0, 0, 0), font=tamil_font)
                        draw.text((32, 128), expected_english, fill=(0, 0, 0), font=latin_font)
                    else:
                        draw.text((32, 80), expected_english, fill=(0, 0, 0), font=latin_font)

                    img.save(img_path, format="PNG")
                    blocks, _conf, _warns = run_ocr_on_image(
                        img_path,
                        "bilingual_smoke.png",
                        1,
                        OCROptions(language="tam+eng" if "tam" in langs else "eng"),
                    )
                    smoke_sample = " | ".join(b.text.strip() for b in blocks if b.text.strip())
                    full_text = " ".join(b.text.strip() for b in blocks if b.text.strip())
                    extracted_tamil_line = "".join(
                        ch for ch in full_text if ("\u0B80" <= ch <= "\u0BFF") or ch.isspace()
                    ).strip()
                    extracted_english_line = "".join(
                        ch for ch in full_text if ch.isascii() and (ch.isalnum() or ch.isspace())
                    ).strip()
                    tamil_metrics = compute_cer_and_wer(extracted_tamil_line, expected_tamil)
                    english_metrics = compute_cer_and_wer(extracted_english_line, expected_english)
                    smoke_passed = bool(
                        english_metrics["cer"] <= 0.15
                        and (tamil_metrics["cer"] <= 0.15 or tamil_font is None or "tam" not in langs)
                    )
            except Exception as exc:
                smoke_sample = f"error: {exc}"
                smoke_passed = False

        registered = sorted(t for t in celery_app.tasks.keys() if t.startswith("app.tasks."))
        _cached_worker_ocr_probe = {
            "verification_type": "synthetic_ocr_execution_smoke_test",
            "is_synthetic_smoke_test": True,
            "available": bool(ocr_info.get("available") and "tam" in langs and "eng" in langs and smoke_passed),
            "tesseract_available": bool(ocr_info.get("available")),
            "languages": langs,
            "smoke_test_passed": smoke_passed,
            "reference_tamil": expected_tamil,
            "extracted_tamil": extracted_tamil_line,
            "tamil_cer": tamil_metrics["cer"],
            "tamil_wer": tamil_metrics["wer"],
            "tamil_char_accuracy": tamil_metrics["char_accuracy"],
            "reference_english": expected_english,
            "extracted_english": extracted_english_line,
            "english_cer": english_metrics["cer"],
            "english_wer": english_metrics["wer"],
            "english_char_accuracy": english_metrics["char_accuracy"],
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


def run_scheduled_retention_cleanup_sync() -> Dict[str, Any]:
    """Run periodic retention and reservation expiry cleanup against the database and storage."""
    from app.services.retention import cleanup_expired_resources, get_storage_budget_snapshot

    db = SessionLocal()
    try:
        cleaned = cleanup_expired_resources(db)
        budget = get_storage_budget_snapshot(db)
        return {
            "status": "completed",
            "cleaned": cleaned,
            "storage_budget": budget,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        db.rollback()
        return {
            "status": "error",
            "error": str(exc)[:300],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        db.close()


if celery_app is not None:
    @celery_app.task(name="app.tasks.run_scheduled_retention_cleanup", bind=True)
    def run_scheduled_retention_cleanup(self) -> Dict[str, Any]:
        return run_scheduled_retention_cleanup_sync()
else:
    def run_scheduled_retention_cleanup() -> Dict[str, Any]:
        return run_scheduled_retention_cleanup_sync()


_retention_scheduler_started = False


def start_retention_scheduler_thread() -> bool:
    """
    Start a lightweight daemon thread inside the worker process that periodically
    runs retention and expired-reservation cleanup, independent of user uploads.
    """
    global _retention_scheduler_started
    if _retention_scheduler_started:
        return False
    _retention_scheduler_started = True

    import threading

    def _loop() -> None:
        settings = get_settings()
        interval = max(60, int(getattr(settings, "RETENTION_CLEANUP_INTERVAL_SECONDS", 900) or 900))
        while True:
            try:
                run_scheduled_retention_cleanup_sync()
                publish_worker_heartbeat(force_reprobe=False)
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="retention-cleanup-scheduler", daemon=True)
    t.start()
    return True


try:
    from celery.signals import heartbeat_sent, worker_ready

    @worker_ready.connect
    def _on_worker_ready(**_kwargs: Any) -> None:
        publish_worker_heartbeat(force_reprobe=True)
        start_retention_scheduler_thread()

    @heartbeat_sent.connect
    def _on_worker_heartbeat(**_kwargs: Any) -> None:
        publish_worker_heartbeat(force_reprobe=False)
except Exception:
    pass

