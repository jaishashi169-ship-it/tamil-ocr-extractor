# API Documentation — Tamil English Document Studio

Base URL: `/api/v1`

All endpoints (except `/health`, `/api/v1/auth/*`, `/api/v1/capabilities`, and `/api/v1/downloads/{token}`) require `Authorization: Bearer <access_token>`.

## Authentication
- `POST /auth/guest` — Create an isolated temporary user session.
- `POST /auth/register` — Register a user account.
- `POST /auth/login` — Authenticate and receive a JWT access token.
- `GET /auth/me` — Current user profile and quotas.

## Chunked 500 MB Uploads
- `POST /uploads/init` — Initialize a chunked upload session (enforces 500,000,000-byte limit and user quota).
- `PUT /uploads/{session_id}/chunk/{chunk_index}` — Upload a chunk (`multipart/form-data` field `chunk`).
- `GET /uploads/{session_id}` — Check received chunks for resume.
- `POST /uploads/{session_id}/pause` — Mark upload paused.
- `POST /uploads/{session_id}/complete` — Assemble chunks, compute SHA-256, and validate magic-byte signature.
- `DELETE /uploads/{session_id}` — Abort upload and clean up chunks.

## Extraction Jobs
- `POST /jobs` — Start an extraction/conversion job (`idempotency_key` supported; transient `password` never stored).
- `GET /jobs?page=1&page_size=20` — Paginated list of user jobs.
- `GET /jobs/{job_id}` — Job status, progress percentage, stage label, page counts, errors.
- `POST /jobs/{job_id}/cancel` — Cancel queued/running job.
- `POST /jobs/{job_id}/retry` — Retry failed job (with optional transient password).
- `DELETE /jobs/{job_id}` — Delete-now (removes original, previews, exports, drafts, and DB records).

## Review & Editing
- `GET /jobs/{job_id}/pages` — Read extracted pages, raw OCR text, edited text, confidence, and block metadata.
- `GET /jobs/{job_id}/pages/{page_number}/image?kind=preview|thumbnail` — Authorized page preview image.
- `PUT /jobs/{job_id}/pages/{page_number}` — Save reviewed text / autosave draft (supports `expected_version` optimistic concurrency).
- `POST /jobs/{job_id}/pages/{page_number}/grammar-review` — Non-destructive Tamil/English spelling & grammar suggestions and diff.
- `POST /jobs/{job_id}/pages/{page_number}/reprocess` — Re-run OCR on full page or `crop_box: [x, y, w, h]`.
- `POST /jobs/{job_id}/headers-footers/preview` & `/apply` — Detect and remove repeated headers/footers.
- `POST /jobs/{job_id}/legacy-tamil/preview` & `/apply` — Explicit `bamini` or `tab` to Unicode conversion.

## Exports & Downloads
- `POST /jobs/{job_id}/exports` — Generate export (`txt`, `md`, `docx`, `pdf`, `searchable_pdf`, `html`, `json`, `csv`, `xlsx`, `png`, `jpg`, `webp`) with quality preset (`high`, `balanced`, `smallest`, `custom`).
- `GET /jobs/{job_id}/exports/{artifact_id}/link` — Expiring authorized download link.
- `GET /jobs/{job_id}/original/link` — Expiring authorized download link for unchanged original.
- `POST /jobs/batch/zip` — Package multiple completed jobs into a ZIP.
- `GET /downloads/{token}` — Stream authorized file.

## Study Studio
- `GET /study/status` — Check whether a real AI provider (`GEMINI_API_KEY` / `OPENAI_API_KEY`) is configured.
- `POST /study/projects` — Generate Study Guide & Question Bank (1, 2, 3, 5, 10 marks) from a completed job (returns 503 if AI provider is unconfigured).
- `GET /study/projects/{project_id}` — Fetch project with optional filters (`chapter`, `marks`, `difficulty`, `verification_status`).
- `PATCH /study/items/{item_id}` — Edit, approve, lock, or report issue on a study item.
- `POST /study/projects/{project_id}/regenerate` — Regenerate unlocked items while preserving locked items.
- `POST /study/projects/{project_id}/export` — Download `docx`, `pdf`, `txt`, `md`, `xlsx`, `json`, or `zip`.

## Administration & Capabilities
- `GET /capabilities` — Supported formats, limits, and installed Tesseract languages.
- `GET /admin/overview` — Admin-only job counts, avg duration, storage usage, worker health (no document contents exposed).
- `PUT /admin/quotas` — Update system quotas.
