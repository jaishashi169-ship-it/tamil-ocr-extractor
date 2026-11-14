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
   - `NEXT_PUBLIC_API_URL=https://your-domain.example/api/v1`
   - `GEMINI_API_KEY` / `OPENAI_API_KEY` (if enabling Study Studio)

## 3. Build, Migrate & Start
```bash
docker compose up --build -d
docker compose ps
```
The `backend` container runs `alembic upgrade head` before starting `uvicorn`. Verify the migration head:
```bash
docker compose exec backend alembic current
# Expected: 0002_worker_ownership (head)
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
