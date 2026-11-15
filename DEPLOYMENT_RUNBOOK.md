# Deployment & Operations Runbook — Tamil English Document Studio

## 1. Architecture Overview (Vercel-Only Multi-Service + Worker Daemon)
Root [`vercel.json`](file:///e:/AI%20WEB%20Application%20Development%20lists/Tamil%20OCR%20Extractor/vercel.json) defines a unified Vercel deployment with three components and a scheduled retention cron:
- **`services.frontend` (`frontend/`):** Next.js 15 App Router frontend + server-side `@vercel/blob/client` `handleUpload` token route (`/api/blob/upload`).
- **`services.backend` (`backend/app/main.py`):** Lightweight FastAPI API service (`backend/requirements.txt` without heavy worker packages). Dispatches Celery jobs by stable task names (`app.tasks.finalize_upload`, `app.tasks.process_extraction_job`, `app.tasks.generate_export`, `app.tasks.generate_batch_zip`, `app.tasks.run_scheduled_retention_cleanup`) without importing `app.tasks`, `cv2`, `numpy`, `pytesseract`, `PIL`, `fitz`, or `psd_tools`.
- **`daemons.celery` (`backend/Dockerfile.worker`):** Containerized Celery worker daemon on Vercel Fluid compute (`2Gi` memory request in `iad1`, `--concurrency=2`) containing `tesseract-ocr`, `tesseract-ocr-tam`, `tesseract-ocr-eng`, `poppler-utils`, `libreoffice-writer`, `fonts-noto-core`, `fonts-noto-extra`, PyMuPDF, and OpenCV.
- **`crons` (`/api/v1/internal/cron/retention`):** Platform-scheduled retention and expired-reservation cleanup (`0 3 * * *` daily on Hobby).

> **Retention trigger note:** Three independent triggers all call `cleanup_expired_resources` through the same distributed lock (in-process `threading.Lock` → Redis `SET NX EX` → PostgreSQL `pg_try_advisory_xact_lock`). They are:
> 1. **In-worker scheduler thread** (`start_retention_scheduler_thread`): fires every 900 s inside `celery worker`.
> 2. **Celery task** `run_scheduled_retention_cleanup` dispatched via Celery app scheduler (periodic scheduler, not Celery Beat — `beat_schedule` was removed from `celery_app.py` to prevent Beat from running inside `celery worker`).
> 3. **Vercel Cron** `GET /api/v1/internal/cron/retention` daily at `0 3 * * *`.
> All three yield `{"skipped_locked": true}` if another trigger already holds the lock, preventing overlapping execution.

---

## 2. Official Vercel Hobby Plan Limits (Verified Against Current Official Data)

> [!IMPORTANT]
> These are the **actual** current Hobby limits used in provisioning decisions. Prior reports contained incorrect figures — this section supersedes them.

### 2.1 Vercel Blob (Private Storage)
| Limit | Hobby (Free) | Notes |
|---|---|---|
| Storage cap | **1 GB** (hard stop, no overage) | App budget set to 850 MB to avoid suspension |
| Simple ops/month | **10,000** | Reads, deletes, stat (`head_blob`) |
| Advanced ops/month | **2,000** | `put_blob`, `multipart_upload_blob`, `list`/`query_blobs`, `copy`, TTL-set count as advanced ops |
| CDN cache | Files ≤ 512 MB only | 500 MB originals are right at the edge |
| Data transfer | Shared bandwidth pool | No dedicated Hobby bandwidth limit published |

> [!CAUTION]
> **Operation count is the binding constraint for upload volume.** Each `put_blob` call = 1 advanced op. Multipart upload parts may each count. At 2,000 advanced ops/month, the app can serve roughly **65–100 uploads/month** (depending on file size and chunking), not unlimited. A production deployment serving more users must upgrade to Pro before exhausting this limit. The atomic reservation system prevents storage overflow but **cannot prevent operation-count exhaustion**.

### 2.2 Vercel Fluid Compute / Daemons
| Limit | Hobby (Free) | Notes |
|---|---|---|
| Active CPU | 4–5 CPU-hours/month | Charged on active execution, not idle |
| Provisioned memory | **360–420 GB-hours/month** | Charged continuously while daemon is live |
| Max execution | 300 s per request | |
| Daemon max memory | 2 GiB (configured) | 2 GiB × 24h × ~9 days ≈ 432 GB-hours |

> [!WARNING]
> A single 2 GiB daemon running continuously exhausts **~420 GB-hours in approximately 9 days**, not 7.5 days (prior reports were wrong). After exhaustion the daemon pauses until the next billing cycle. Plan for the worker to be unavailable for the remainder of the month after ~9 days of continuous operation on Hobby. Upgrade to Pro for persistent worker availability.

### 2.3 Neon PostgreSQL (Vercel Marketplace)
| Limit | Free Tier | Notes |
|---|---|---|
| Storage | 0.5 GB | Includes WAL |
| Compute | 100 CU-hours/month | Scales to zero when idle |
| Connections | 100 | Pooled via pgBouncer |

### 2.4 Redis Cloud Free (Vercel Marketplace)
| Limit | Free Tier | Notes |
|---|---|---|
| RAM | 30 MB | Covers worker heartbeats, job state, retention lock |
| Persistence | No | Data is lost on restart |
| Connections | 30 | |

### 2.5 Verdict for Restricted Smoke Testing
Hobby/Free is suitable for **restricted smoke testing only** (1–5 test uploads, one developer). Before public launch, upgrade to Vercel Pro (or equivalent paid tiers) to avoid operation exhaustion and daemon downtime.

---

## 3. Official Vercel Blob Private Storage & Atomic Capacity Reservations
### 3.1 Official SDK Integration (No Manual Token Signing)
- **Browser / Next.js Server (`@vercel/blob` `^2.9.0`):**
  - Client uploads use official `upload(storageKey, file, { access: 'private', handleUploadUrl: '/api/blob/upload', multipart: true, clientPayload })` from `@vercel/blob/client` in [`frontend/src/lib/api.ts`](file:///e:/AI%20WEB%20Application%20Development%20lists/Tamil%20OCR%20Extractor/frontend/src/lib/api.ts).
  - Token exchange runs strictly server-side in [`frontend/src/app/api/blob/upload/route.ts`](file:///e:/AI%20WEB%20Application%20Development%20lists/Tamil%20OCR%20Extractor/frontend/src/app/api/blob/upload/route.ts) via official `handleUpload` from `@vercel/blob/client`, which verifies the upload session and active `StorageReservation` against `POST /api/v1/uploads/{session_id}/authorize-blob` before issuing a path-locked token (`maximumSizeInBytes`, `addRandomSuffix: false`, `allowOverwrite: false`).
  - **Single-use token enforcement:** `authorize-blob` marks `s3_parts_json` with `{blob_token_issued: true}` after the first token issuance; any replay returns HTTP `409`. `complete_upload` and `abort_upload` also reject operations on finalized sessions (status `processing/completed/aborted/failed` or `sha256_hash` already set) with HTTP `409`.
  - `BLOB_READ_WRITE_TOKEN` is **never** exposed to the browser or returned by any API endpoint.
- **Python FastAPI & Celery Worker (`vercel-blob==0.6.0`):**
  - [`backend/app/storage.py`](file:///e:/AI%20WEB%20Application%20Development%20lists/Tamil%20OCR%20Extractor/backend/app/storage.py) uses official `from vercel.blob import sync as vercel_blob_sync` (`put_blob`, `multipart_upload_blob`, `head_blob`, `stream_blob`, `get_blob`, `query_blobs`, `delete_blob`) with `access="private"`.
  - Authenticated downloads stream in bounded `256 KiB` chunks with HTTP `Range` (`206 Partial Content`) support.
  - Existing S3-compatible storage (`STORAGE_BACKEND=s3`) remains fully supported.

### 3.2 Atomic Storage Reservations & Hobby Plan Capacity Note
- **Atomic Reservation Ledger (`StorageBudgetState` + `StorageReservation`, Migration `0004_storage_reservations`):**
  - Every storage allocation acquires a serialized lock on `StorageBudgetState(id=1)` (`SELECT ... FOR UPDATE` on PostgreSQL) via [`backend/app/services/retention.py`](file:///e:/AI%20WEB%20Application%20Development%20lists/Tamil%20OCR%20Extractor/backend/app/services/retention.py).
  - Reservations cover **concurrent uploads (`"upload"`)**, **originals (`"original"`)**, **page previews (`"preview"`)**, **exports (`"export"`)**, and **temporary stored objects (`"temp_object"`)**.
  - Limits are enforced both at upload initialization **and** during worker output generation (`bind_storage_hooks` in `process_extraction_job_sync`, `generate_export_sync`, and `generate_batch_zip_sync`).
  - Failed jobs, failed exports, and abandoned/aborted uploads automatically delete partial storage objects and release their reservations.
- **Important Hobby Storage Allowance Constraint:**
  - On Vercel Hobby (`1 GB` hard store cap), `TOTAL_STORAGE_BUDGET_BYTES=850000000` (`850 MB`) and `USER_STORAGE_QUOTA_BYTES=750000000` (`750 MB`) prevent store suspension.
  - **An `850 MB` total budget does NOT guarantee that every `500 MB` document plus its rendered page previews and generated exports will fit concurrently.** If active originals + reservations + generated outputs exceed `850 MB`, the reservation system deterministically rejects the operation with HTTP `413` / `StorageQuotaExceededError` and cleans up partial outputs.

---

## 4. Restricted Production Smoke Test (Before Opening Public Uploads)
Because Vercel Fluid Compute Daemons (`daemons.celery`) execute in production deployments rather than ephemeral preview deployments:
1. Keep public uploads gated during initial production verification by setting:
   - `REQUIRE_READINESS_FOR_UPLOADS=true` (blocks `POST /api/v1/uploads/init` with HTTP `503 SYSTEM_NOT_READY_FOR_UPLOADS` whenever database, Redis broker, private storage, or verified OCR worker heartbeat is not ready).
   - `RESTRICTED_SMOKE_TEST_MODE=true` and `SMOKE_TEST_SECRET=<strong-random-token>` (blocks public `POST /api/v1/uploads/init` with HTTP `403 RESTRICTED_SMOKE_TEST_MODE` unless the request includes `X-Smoke-Test-Token: <SMOKE_TEST_SECRET>` or an admin JWT).
2. Run the restricted production smoke test script:
   ```bash
   python scripts/run_production_smoke_test.py \
     --base-url https://tamilocrextraxtor.vercel.app \
     --token "$SMOKE_TEST_SECRET"
   ```
3. Only set `RESTRICTED_SMOKE_TEST_MODE=false` after `/health/ready` returns `200`, `POST /api/v1/admin/smoke-test` returns `"smoke_test_passed": true`, and the full upload → OCR → edit → export → Study Studio workflow passes.

---

## 5. Worker Scaling, Quota Monitoring & Recovery Procedures
### 5.1 Worker Scaling
- **Vertical & Concurrency Tuning (`vercel.json` -> `daemons.celery`):**
  - Default configuration runs `1` instance in `iad1` with `memory: "2Gi"` and `--concurrency=2` to stay within Hobby Fluid compute limits while isolating Tesseract/PyMuPDF memory peaks.
  - To scale worker throughput on Pro/Enterprise plans, increase `daemons.celery.instances.iad1` (e.g., `2`–`4`) or increase `resources.requests.memory` to `"4Gi"` with `--concurrency=4`.
### 5.2 Quota & Queue Monitoring
- **Readiness Probe:** `GET /health/ready` (or `GET /api/v1/health/ready`) reports live readiness of PostgreSQL, Redis (`rediss://` with `ssl_cert_reqs=CERT_REQUIRED` + `certifi`), private storage (`vercel-blob` SDK `query_blobs(page_size=1)`), and the Celery worker's Redis heartbeat (`doc_studio:worker:status`, TTL `180s`).
- **Quota & Reservation Breakdown:** `GET /api/v1/admin/quotas` returns:
  - `storage_budget` (`total_used_bytes`, `active_reserved_bytes`, `active_committed_bytes`, `remaining_budget_bytes`),
  - `utilization_percent` and `warning_level` (`normal` < 75%, `warning` ≥ 75%, `critical` ≥ 90%),
  - `reservation_breakdown` grouped by `resource_type` (`upload`, `original`, `preview`, `export`, `temp_object`) and `status`,
  - `queue_depth` (`queued_jobs`, `running_jobs`), and `worker_heartbeat`.
### 5.3 Recovery Procedures
- **Stuck / Orphaned Reservations or Expired Jobs:**
  - Automatic recovery runs every 15 minutes inside the worker daemon (`run_scheduled_retention_cleanup_sync`) and daily via Vercel Cron (`GET /api/v1/internal/cron/retention` with `Authorization: Bearer <CRON_SECRET>`).
  - Manual immediate cleanup: `POST /api/v1/admin/retention/cleanup` (as admin) or `curl -H "Authorization: Bearer $CRON_SECRET" https://tamilocrextraxtor.vercel.app/api/v1/internal/cron/retention`.
- **Worker Heartbeat Loss (`worker.ready: false` on `/health/ready`):**
  1. Check Redis connectivity and usage (`30 MB` cap on Redis Cloud Free) and ensure `noeviction` policy has not blocked writes.
  2. Redeploy or restart the `celery` daemon and inspect worker startup logs for `publish_worker_heartbeat`.
  3. While the worker is offline, `REQUIRE_READINESS_FOR_UPLOADS=true` automatically prevents new public uploads from accumulating in storage.
- **Database Schema Migration & Rollback:**
  - Upgrade to head (`0004_storage_reservations`): `alembic upgrade head`
  - Downgrade one revision if rolling back: `alembic downgrade 0003_s3_direct_upload`
