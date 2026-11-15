# Deployment & Operations Runbook — Tamil English Document Studio

## 1. Prerequisites
- Docker Engine 24+ and Docker Compose v2
- Reverse proxy (Nginx / Caddy / ALB) with HTTPS (TLS 1.2+) and `client_max_body_size 25m` (to accommodate 5 MB upload chunks + multipart overhead)
- Optional for Study Studio: `GEMINI_API_KEY` or `OPENAI_API_KEY`

## 2. Configuration
1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Set production values in `.env`:
   - `APP_ENV=production`
   - `SECRET_KEY=<64-char random hex string>` (startup fails if left at the dev placeholder or `<32` chars)
   - `CORS_ORIGINS=https://your-frontend.vercel.app` (exact frontend origin(s), comma-separated; wildcard `*` is rejected in `APP_ENV=production`)
   - `ALLOW_BACKGROUND_TASK_FALLBACK=false`
   - `NEXT_PUBLIC_API_URL=https://api.your-domain.example/api/v1` (required for Vercel frontend builds; `api.ts` normalizes trailing slashes/`/api/v1` and fails clearly in production if unset)
   - `STORAGE_BACKEND=s3` with `S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL` (browser-reachable presigned URL host), `S3_BUCKET_NAME`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, and `S3_REGION` for direct-to-storage 500 MB uploads/downloads (8 MiB multipart parts via `S3_MULTIPART_PART_BYTES=8388608`)
   - `GEMINI_API_KEY` / `OPENAI_API_KEY` (if enabling Study Studio)

### S3 / MinIO Bucket CORS (Required for Direct Browser Uploads)
When `STORAGE_BACKEND=s3`, configure bucket CORS to allow `PUT` and `GET` from your frontend origin and expose `ETag` so the browser can register multipart part ETags:
```json
[
  {
    "AllowedOrigins": ["https://your-frontend.vercel.app"],
    "AllowedMethods": ["GET", "PUT", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

### Vercel Frontend + API/Worker Dependency Split
- Root `vercel.json` is currently configured for a **frontend-only Next.js deployment** (`frontend/`), so Vercel does **not** bundle the Python backend until the Vercel account plan/Services/Daemons capabilities and managed PostgreSQL/Redis/S3 resources are verified.
- **Lightweight API vs. Worker split:**
  - `backend/requirements-api.txt` + `backend/Dockerfile.api` contain only the FastAPI API dependencies and dispatch Celery jobs by stable task names (`app.tasks.finalize_upload`, `app.tasks.process_extraction_job`, `app.tasks.generate_export`, `app.tasks.generate_batch_zip`) without importing `app.tasks`, `cv2`, `numpy`, `pytesseract`, `PIL`, `fitz`, or `psd_tools`.
  - `backend/requirements.txt` + `backend/Dockerfile.worker` contain the full OCR/document/office stack (`tesseract-ocr-tam`, `tesseract-ocr-eng`, `poppler-utils`, `libreoffice-writer`, `fonts-noto-core`, `fonts-noto-extra`, PyMuPDF, OpenCV) for the Celery worker (`celery -A app.tasks.celery_app worker --loglevel=info --concurrency=2`).
  - `/api/v1/capabilities` and `/api/v1/admin/overview` report `ocr_execution_mode: "worker_daemon"` and `verified_locally: false` when running in the lightweight API environment without local OCR binaries.
- For Docker Compose hosting, `docker-compose.yml` runs FastAPI (`backend`), Celery (`worker`), PostgreSQL (`postgres`), Redis (`redis`), and private object storage (`minio` or external S3) with PostgreSQL (`5432`) and Redis (`6379`) isolated on `internal_net`.

## 3. Build, Migrate & Start
```bash
docker compose up --build -d
docker compose ps
```
The `backend` container runs `alembic upgrade head` before starting `uvicorn`. Verify the migration head:
```bash
docker compose exec backend alembic current
# Expected: 0003_s3_direct_upload (head)
```
Verify OCR languages inside the isolated `worker` container:
```bash
docker compose exec worker tesseract --list-langs
# Expected: eng, tam
```

## 4. Smoke Test
Run the reproducible integration verification script against the deployed backend:
```bash
python scripts/verify_integration.py --base-url http://localhost:8000
```

## 5. Backup & Restore
- **PostgreSQL:**
  - Backup: `docker compose exec -T postgres pg_dump -U studio tamil_doc_studio > backup_$(date +%F).sql`
  - Restore: `cat backup_YYYY-MM-DD.sql | docker compose exec -T postgres psql -U studio tamil_doc_studio`
- **Private Storage Volume (`private_storage`):**
  - Backup the Docker volume `/var/lib/docker/volumes/..._private_storage/_data` (or S3/MinIO bucket with versioning/replication in `STORAGE_BACKEND=s3` mode).

## 6. Rollback Procedure
1. Stop application containers: `docker compose stop frontend backend worker`
2. If rolling back schema from `0002_worker_ownership` to `0001_initial`:
   `docker compose run --rm backend alembic downgrade 0001_initial`
3. Deploy the previous container image tag / source revision and start services: `docker compose up -d`.
