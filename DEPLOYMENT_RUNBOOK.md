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
   - `ALLOW_BACKGROUND_TASK_FALLBACK=false`
   - `NEXT_PUBLIC_API_URL=https://your-domain.example/api/v1` (or omit when deploying multi-service on Vercel with same-origin `/api/(.*)` routing)
   - `STORAGE_BACKEND=s3` with `S3_ENDPOINT_URL`, `S3_PUBLIC_ENDPOINT_URL` (browser-reachable presigned URL host), `S3_BUCKET_NAME`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, and `S3_REGION` for direct-to-storage 500 MB uploads/downloads (bypasses Vercel Functions' 4.5 MB request/response limit)
   - `GEMINI_API_KEY` / `OPENAI_API_KEY` (if enabling Study Studio)

### S3 / MinIO Bucket CORS (Required for Direct Browser Uploads)
When `STORAGE_BACKEND=s3`, configure bucket CORS to allow `PUT` and `GET` from your frontend origin and expose `ETag` so the browser can register multipart part ETags:
```json
[
  {
    "AllowedOrigins": ["https://your-frontend.example"],
    "AllowedMethods": ["GET", "PUT", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

### Vercel Multi-Service vs. Docker Worker Architecture
- Root `vercel.json` defines `frontend` (Next.js) and `backend` (FastAPI `app/main.py`) with `/api/(.*)` and `/health` routed to `backend` and `/(.*)` routed to `frontend`.
- Heavy OCR (`tesseract-ocr` `tam`+`eng`, `soffice`, Celery workers, PostgreSQL, Redis) still requires the containerized Docker worker stack (`docker-compose.yml`). Set `NEXT_PUBLIC_API_URL` if routing browser API calls to an external container backend instead of Vercel's `/api` service.

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
