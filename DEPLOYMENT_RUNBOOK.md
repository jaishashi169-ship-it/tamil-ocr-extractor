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

### Vercel Frontend + Docker Backend/Worker Architecture
- Root `vercel.json` is configured for a **frontend-only Next.js deployment** (`frontend/`), so Vercel does **not** bundle the Python backend (which exceeds Vercel's 500 MB Lambda limit due to OCR/PDF/office dependencies). If you set Root Directory to `frontend` in Vercel Project Settings, keep `NEXT_PUBLIC_API_URL` configured in Vercel's build environment variables.
- Deploy FastAPI (`backend`), Celery (`worker`), PostgreSQL (`postgres`), Redis (`redis`), and private object storage (`minio` or external S3) on a Docker host using `docker-compose.yml` (`docker compose up --build -d backend worker`). Do **not** publish PostgreSQL (`5432`) or Redis (`6379`) ports publicly; `docker-compose.yml` isolates them on `internal_net`.

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
