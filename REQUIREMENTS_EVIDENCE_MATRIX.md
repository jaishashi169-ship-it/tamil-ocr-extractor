# Requirements Evidence Matrix — Tamil English Document Studio

| Requirement | Implementation Location | Verification / Command | Status |
|---|---|---|---|
| **Max upload 500,000,000 bytes** | `backend/app/config.py`, `backend/app/routers/uploads.py` | `test_500mb_limit_enforcement`, `test_streamed_upload_boundary_resumability_auth_and_sha256` | **PASS** |
| **Resumable chunked & direct-S3 uploads & private storage** | `backend/app/routers/uploads.py`, `backend/app/storage.py`, `0003_s3_direct_upload.py` | `test_chunked_upload_idempotency_conflict_and_resume`, `test_streamed_upload_boundary_resumability_auth_and_sha256`, `test_direct_s3_multipart_upload_and_presigned_downloads` | **PASS** (local & mocked S3); **BLOCKED** (live S3/MinIO CORS) |
| **Safe worker ownership, leases & idempotency** | `backend/app/models.py`, `backend/app/tasks.py`, `0002_worker_ownership_and_dispatch.py` | `test_worker_idempotency_duplicate_delivery_and_restart_recovery`, `alembic upgrade head` | **PASS** (DB/task level); **BLOCKED** (live Redis/Celery container) |
| **Bounded API dispatch & worker reconnection** | `backend/app/routers/jobs.py`, `backend/app/celery_app.py` | `test_production_dispatch_requires_celery_and_fails_job_on_redis_unavailable`, `test_publish_failure_after_redis_ping_succeeds` | **PASS** (mocked broker); **BLOCKED** (live Redis outage) |
| **Tamil, English & mixed extraction (direct)** | `backend/app/services/importers/`, `backend/app/services/tamil_utils.py` | `test_tamil_and_model.py`, `test_real_english_pdf_tamil_docx_corrupt_and_encrypted_pdf`, `verify_integration.py` | **PASS** |
| **Real scanned Tamil–English OCR (`tam+eng`)** | `backend/app/services/ocr_engine.py` | `test_ocr_unavailable_fails_cleanly_when_tesseract_missing` | **BLOCKED** (`tesseract` not installed on host) |
| **Legacy `.doc` conversion** | `backend/app/services/importers/docx_importer.py` | Code inspected (`soffice` in `backend/Dockerfile`) | **BLOCKED** (`soffice` not installed on host) |
| **PSD editable text & raster fallback** | `backend/app/services/importers/psd_importer.py` | Code inspected | **BLOCKED** (no real `.psd` fixture tested) |
| **Editable review, autosave & grammar review** | `backend/app/routers/pages.py`, `backend/app/services/study/grammar.py` | `test_optimistic_concurrency_and_grammar_review`, `verify_integration.py` | **PASS** |
| **Original, converted & compressed downloads** | `backend/app/routers/exports.py`, `backend/app/services/exporters/` | `test_compression_presets_and_export_file_opening`, `verify_integration.py` | **PASS** |
| **AI Study Guides & 1/2/3/5/10M Question Banks** | `backend/app/routers/study.py`, `backend/app/services/study/` | `test_study_studio_unconfigured_returns_503`, `test_study_studio_all_marks_lock_verification_and_exports` | **PASS** (503 guard & mocked AI); **BLOCKED** (live LLM key) |
| **Responsive English/தமிழ் frontend** | `frontend/src/` | `npx tsc --noEmit`, `npm test` (5/5), `npm run build`, headless Chrome (`320..1440` px, `0` overflow/console errors) | **PASS** (local browser); **BLOCKED** (live HTTPS preview) |
