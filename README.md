# Tamil English Document Studio

**Convert documents. Keep your words.**

Tamil English Document Studio is a full-stack application for extracting text from uploaded documents and images, recognizing Tamil and English via OCR, reviewing and editing extracted Unicode text side-by-side, generating structured Study Guides & Question Banks, and exporting to supported formats.

## Architecture

- **Frontend (`frontend/`)**: Next.js 15 + React 19 + TypeScript + Tailwind CSS (Noto Sans & Noto Sans Tamil, Light/Dark themes, English/தமிழ் UI switch, 500 MB chunked resumable upload).
- **Backend API (`backend/`)**: Python FastAPI + SQLAlchemy + Alembic migrations + Pydantic v2.
- **Background Worker**: Celery + Redis job queue running on an isolated internal Docker network (`internal_net`) with Tesseract (`tam+eng`), PyMuPDF, `python-docx`, `psd-tools`, OpenCV, ReportLab, `openpyxl`, and isolated LibreOffice (`soffice`) for legacy `.doc`.
- **Database & Storage**: PostgreSQL 16, Redis 7, and private S3/MinIO or local private storage with expiring signed download links.

## Quick Start (Docker Compose)

1. Copy `.env.example` to `.env` and update `SECRET_KEY` and storage credentials:
   ```bash
   cp .env.example .env
   ```
2. Build and start all services:
   ```bash
   docker compose up --build -d
   ```
3. Open the application:
   - Frontend: http://localhost:3000
   - Backend API Docs (Swagger): http://localhost:8000/docs

## Local Development

### Backend
```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Run backend automated tests:
```bash
cd backend
pytest -v
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

Run frontend automated checks:
```bash
cd frontend
npx tsc --noEmit
npm test
npm run build
```

## Production Deployment Notes

- Set `APP_ENV=production`, a strong `SECRET_KEY`, and `STORAGE_BACKEND=s3` with private bucket policies.
- Keep the `worker` service on an internal-only network without external internet egress.
- Configure your reverse proxy (Nginx/Caddy/ALB) to allow `client_max_body_size` for 5 MB upload chunks (total file limit is 500,000,000 bytes).
- Never log document text, document passwords, or signed download tokens.
