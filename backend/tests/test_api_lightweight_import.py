import os
import subprocess
import sys
from pathlib import Path


def test_api_imports_and_boots_without_worker_packages(tmp_path):
    """
    Simulate a lightweight API deployment environment where heavy worker-only
    packages (cv2, numpy, pytesseract, PIL, fitz, psd_tools) and app.tasks
    are absent. Verify app.main imports cleanly and /health + /api/v1/capabilities work.
    """
    backend_dir = Path(__file__).resolve().parents[1]
    db_file = (tmp_path / "lightweight_api_test.db").as_posix()
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_file}"
    env["SYNC_DATABASE_URL"] = f"sqlite:///{db_file}"

    script = """
import asyncio
import sys
from importlib.abc import MetaPathFinder

BLOCKED_PREFIXES = ("cv2", "numpy", "pytesseract", "PIL", "fitz", "psd_tools", "app.tasks")

class _BlockWorkerPackages(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        for prefix in BLOCKED_PREFIXES:
            if fullname == prefix or fullname.startswith(prefix + "."):
                raise ImportError(f"Blocked worker-only module in lightweight API test: {fullname}")
        return None

sys.meta_path.insert(0, _BlockWorkerPackages())

import httpx
from app.main import app, on_startup

async def _verify():
    on_startup()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        h = await client.get("/health")
        assert h.status_code == 200, h.text
        assert h.json()["status"] == "ok"
        assert h.json()["liveness"] == "ok"

        r = await client.get("/health/ready")
        assert r.status_code == 503, r.text
        r_data = r.json()
        assert r_data["status"] == "not_ready"
        assert r_data["dependencies"]["worker"]["ready"] is False
        assert r_data["dependencies"]["worker"]["status"] == "configured_but_unverified"

        cap = await client.get("/api/v1/capabilities")
        assert cap.status_code == 200, cap.text
        data = cap.json()
        assert data["ocr_status"]["available"] is False
        assert data["ocr_status"]["status"] == "configured_but_unverified"
        assert data["ocr_status"]["execution_mode"] == "worker_daemon"
        assert data["ocr_status"]["verified_locally"] is False
        assert data["ocr_status"]["verified_worker"] is False
        assert data["installed_ocr_languages"] == []
        assert "tam" in data["configured_ocr_languages"]

asyncio.run(_verify())
"""
    out_file = tmp_path / "proc_out.log"
    with open(out_file, "w", encoding="utf-8") as log_f:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(backend_dir),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    output_text = out_file.read_text(encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"Subprocess failed ({proc.returncode}):\n{output_text}"


def test_production_safety_rejects_sqlite_and_local_storage():
    import pytest
    from app.config import Settings

    # Rejects SQLite in production
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="a" * 40,
            CORS_ORIGINS="https://tamilstudio.app",
            ALLOW_BACKGROUND_TASK_FALLBACK=False,
            DATABASE_URL="sqlite:///./prod.db",
            SYNC_DATABASE_URL="sqlite:///./prod.db",
            STORAGE_BACKEND="s3",
            S3_ACCESS_KEY="prod-key",
            S3_SECRET_KEY="prod-secret",
        ).validate_production_safety()

    # Rejects local storage in production
    with pytest.raises(ValueError, match="STORAGE_BACKEND=s3"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="a" * 40,
            CORS_ORIGINS="https://tamilstudio.app",
            ALLOW_BACKGROUND_TASK_FALLBACK=False,
            DATABASE_URL="postgresql+asyncpg://user:pass@db:5432/app",
            SYNC_DATABASE_URL="postgresql://user:pass@db:5432/app",
            STORAGE_BACKEND="local",
        ).validate_production_safety()


def test_rediss_tls_enforces_cert_required():
    import ssl
    import certifi
    from app.celery_app import build_celery_ssl_options
    from app.dispatch import _redis_kwargs_for_url

    assert build_celery_ssl_options("redis://localhost:6379/0") is None
    tls_opts = build_celery_ssl_options("rediss://default:secret@redis.cloud.example:6379/0")
    assert tls_opts is not None
    assert tls_opts["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert tls_opts["ssl_ca_certs"] == certifi.where()

    plain_kw = _redis_kwargs_for_url("redis://localhost:6379/0")
    assert "ssl_cert_reqs" not in plain_kw
    tls_kw = _redis_kwargs_for_url("rediss://default:secret@redis.cloud.example:6379/0")
    assert tls_kw["ssl_cert_reqs"] == "required"
    assert tls_kw["ssl_ca_certs"] == certifi.where()
