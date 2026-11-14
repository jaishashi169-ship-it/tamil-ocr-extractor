# Capability Matrix — Tamil English Document Studio

Statuses follow evidence-based verification:
- **Verified**: Exercised directly in automated tests or direct execution in this workspace without mocking the core capability.
- **Verified (Mocked External Boundary)**: Exercised end-to-end against real database/storage/API routes with the external service (LLM API or Redis broker) mocked/simulated.
- **Externally Blocked (Host Missing Dependency)**: Code is implemented and tested up to the external boundary, but cannot execute live on this host because the required runtime (`docker`, `tesseract-ocr` + `tam`/`eng`, `libreoffice-writer`, `redis-server`, or `GEMINI_API_KEY`/`OPENAI_API_KEY`) is not installed/configured.

## 1. Infrastructure, Queue & Async Execution

| Capability | Status | Exact Checks & Results |
|---|---|---|
| **Job Dispatch Hardening & Production Queue Requirement** | **Verified (Mocked Broker)** | `test_production_dispatch_requires_celery_and_fails_job_on_redis_unavailable`, `test_dev_dispatch_without_fallback_flag_returns_503`, `test_publish_failure_after_redis_ping_succeeds`, `test_production_rejects_placeholder_secret`: HTTP 503 `QUEUE_UNAVAILABLE` (`Retry-After: 5`) and `status="failed"` when Redis/publish fails; startup rejects placeholder `SECRET_KEY` in production. |
| **Safe Worker Ownership, Leases & Restart Recovery** | **Verified (DB Lease Simulation)** | `test_worker_idempotency_duplicate_delivery_and_restart_recovery`: atomic `queued -> running` claim with `execution_token` and `lease_expires_at`; live leases rejected even on `redelivered=True`; expired leases reclaimed with a new token; stale-worker writes rejected. |
| **Bounded Async Polling & Celery Task Execution** | **Verified (Thread / Eager Task)** | `test_async_polling_and_timeout_failure`: bounded polling until terminal state, timeout failure reporting `last_status`, and `process_extraction_job_task.apply`. |
| **Alembic Migrations** | **Verified** | Fresh DB `alembic upgrade head` cleanly ran `0001_initial -> 0002_worker_ownership (head)`. |
| **Live Local Integration (`uvicorn`)** | **Verified** | `scripts/verify_integration.py --base-url http://127.0.0.1:8019`: Health, async job completion, SHA-256 original download, Unicode edit/save/reload, and `txt`/`md`/`docx`/`searchable_pdf` exports passed (`OCR` and `Live Study Studio` reported `BLOCKED`). |
| **Docker Compose Full-Stack Integration** | **Externally Blocked** | `docker` / `docker compose` not installed on this Windows host. |

## 2. Supported Input Formats (Validated by Extension + Magic-Byte Signature)

| Format | Extraction Method | Status | Notes & Exact Checks |
|---|---|---|---|
| **PDF** (`.pdf`) | Page-by-page evaluation: direct embedded text (PyMuPDF) or Tesseract OCR (`tam+eng`) | **Verified** (embedded/encrypted/corrupt); **Externally Blocked** (scanned OCR) | `test_real_english_pdf_tamil_docx_corrupt_and_encrypted_pdf`, `test_compression_presets_and_export_file_opening`. |
| **Word** (`.docx`) | Direct extraction (`python-docx`) with zip-bomb archive checks | **Verified** | Verified Tamil + English `.docx` extraction and round-trip export parsing. |
| **Legacy Word** (`.doc`) | Isolated headless LibreOffice conversion to `.docx` | **Externally Blocked** | `soffice` binary not installed on host (included in `backend/Dockerfile`). |
| **Images** (`.png`, `.jpg`, `.jpeg`, `.webp`, `.tiff`, `.bmp`) | Tesseract OCR (`tam+eng`) with rotation, deskew, contrast, denoise, multi-column | **Externally Blocked** | `tesseract` not installed on host. `test_ocr_unavailable_fails_cleanly_when_tesseract_missing` verified clean `status="failed"` handling. |
| **Photoshop** (`.psd`) | Composite preview + layer tree + editable `type` layer extraction (`psd-tools`) | **Externally Blocked (No PSD Sample)** | Code implemented; no real `.psd` fixture tested yet. |
| **Structured & Text** (`.txt`, `.md`, `.html`, `.csv`, `.json`) | Direct Unicode UTF-8/detected encoding parsing | **Verified** | Verified in `test_api_workflow.py`, `test_security_and_limits.py` (`bleach`), `test_study_studio.py`. |

## 3. Upload, Security & Export Capabilities

| Capability | Status | Exact Checks & Results |
|---|---|---|
| **500,000,000-Byte Limit & Streamed Enforcement** | **Verified** | `test_500mb_limit_enforcement`, `test_streamed_upload_boundary_resumability_auth_and_sha256`. |
| **Resumable Upload, Auth Isolation & SHA-256 Original Download** | **Verified** | Pause/status/resume recovery, cross-user 404 isolation, and exact SHA-256 match on `GET /original/link`. |
| **Compression Presets & Smallest-Candidate Selection** | **Verified** | `searchable_pdf` `smallest` selected lossless PNG (`650,485 B`) over larger JPEG (`659,178 B`); `balanced=715,596 B`, `high=650,485 B`. `jpg` preview: `high=111,373 B`, `balanced=80,331 B`, `smallest=64,620 B`. |
| **Export Formats (`txt`, `md`, `docx`, `pdf`, `searchable_pdf`, `html`, `json`, `csv`, `xlsx`, `jpg`)** | **Verified** | All opened and validated (`python-docx`, `PyMuPDF`, `openpyxl`, `PIL.Image`, `json.loads`, UTF-8) with Tamil Unicode preservation. |
| **Frontend Checks** | **Verified** | `npx tsc --noEmit` (0 errors), `npm test` (5/5 Vitest tests), `npm run build` (Next.js 15.5.29 production build). |

## 4. Study Studio & Review Capabilities

| Capability | Status | Exact Checks & Results |
|---|---|---|
| **Optimistic Concurrency & Non-destructive Grammar Review** | **Verified** | `test_optimistic_concurrency_and_grammar_review`. |
| **Study Studio Unconfigured Guard** | **Verified** | `test_study_studio_unconfigured_returns_503` & `verify_integration.py` (HTTP 503 `AI_PROVIDER_UNCONFIGURED`). |
| **Study Studio (1,2,3,5,10M, Locks, Duplicates, Coverage, Question Bank & Answer Key Exports)** | **Verified (Mocked AI)**; **Externally Blocked (Live LLM)** | `test_study_studio_all_marks_lock_verification_and_exports`. Live LLM generation requires `GEMINI_API_KEY` or `OPENAI_API_KEY`. |
