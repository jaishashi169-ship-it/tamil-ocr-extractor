import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import and_, or_

from app.celery_app import celery_app
from app.config import get_settings
from app.database import SessionLocal
from app.models import Job, PageResult
from app.services.document_model import DocumentModel
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
