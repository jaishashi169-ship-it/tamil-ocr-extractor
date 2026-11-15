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
    with pytest.raises(ValueError, match="STORAGE_BACKEND=vercel_blob or STORAGE_BACKEND=s3"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="a" * 40,
            CORS_ORIGINS="https://tamilstudio.app",
            ALLOW_BACKGROUND_TASK_FALLBACK=False,
            DATABASE_URL="postgresql+asyncpg://user:pass@db:5432/app",
            SYNC_DATABASE_URL="postgresql://user:pass@db:5432/app",
            STORAGE_BACKEND="local",
        ).validate_production_safety()

    # Rejects vercel_blob without valid BLOB_READ_WRITE_TOKEN
    with pytest.raises(ValueError, match="BLOB_READ_WRITE_TOKEN"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="a" * 40,
            CORS_ORIGINS="https://tamilstudio.app",
            ALLOW_BACKGROUND_TASK_FALLBACK=False,
            DATABASE_URL="postgresql+asyncpg://user:pass@db:5432/app",
            SYNC_DATABASE_URL="postgresql://user:pass@db:5432/app",
            STORAGE_BACKEND="vercel_blob",
            BLOB_READ_WRITE_TOKEN="invalid-token",
        ).validate_production_safety()

    # Accepts valid vercel_blob configuration in production
    Settings(
        APP_ENV="production",
        SECRET_KEY="a" * 40,
        CORS_ORIGINS="https://tamilstudio.app",
        ALLOW_BACKGROUND_TASK_FALLBACK=False,
        DATABASE_URL="postgresql+asyncpg://user:pass@db:5432/app",
        SYNC_DATABASE_URL="postgresql://user:pass@db:5432/app",
        STORAGE_BACKEND="vercel_blob",
        BLOB_READ_WRITE_TOKEN="vercel_blob_rw_storeabc123_supersecretkey999",
    ).validate_production_safety()


def test_vercel_blob_client_token_and_range_streaming(monkeypatch, tmp_path):
    import base64
    import json
    import pytest
    from app.storage import PrivateStorage, parse_byte_range

    rw_token = "vercel_blob_rw_storexyz789_mastersecretneverexpose"
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", rw_token)

    st = PrivateStorage()
    st.backend = "vercel_blob"
    assert st.is_vercel_blob() is True
    assert st.is_direct_multipart() is True
    assert st.direct_upload_mode() == "direct_blob"

    target = st.get_direct_part_upload_target(
        key="uploads/u1/s1/sample.pdf",
        upload_id="mpu_12345",
        part_number=1,
        max_size_bytes=500_000_000,
        expires_in=900,
    )
    assert target["upload_mode"] == "direct_blob"
    assert target["method"] == "POST"
    auth_header = target["headers"]["Authorization"]
    assert auth_header.startswith("Bearer vercel_blob_client_storexyz789_")
    # Master secret must NEVER appear anywhere in target URL or headers
    serialized_target = json.dumps(target)
    assert rw_token not in serialized_target
    assert "mastersecretneverexpose" not in serialized_target

    client_token = auth_header.split(" ", 1)[1]
    encoded_inner = client_token.split("_", 4)[4]
    decoded_inner = base64.b64decode(encoded_inner).decode("utf-8")
    _sig, payload_b64 = decoded_inner.split(".", 1)
    payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
    assert payload["pathname"] == "uploads/u1/s1/sample.pdf"
    assert payload["maximumSizeInBytes"] == 500_000_000
    assert payload["addRandomSuffix"] is False
    assert payload["allowOverwrite"] is True

    # Verify bounded-memory streaming & HTTP Range support
    local_st = PrivateStorage()
    local_st.backend = "local"
    local_st.local_root = tmp_path
    local_st.local_writable = True
    local_st.save_bytes("exports/test.bin", b"0123456789ABCDEF")

    full_stream = local_st.open_download_stream("exports/test.bin", filename="test.bin")
    assert full_stream["status_code"] == 200
    assert full_stream["headers"]["Accept-Ranges"] == "bytes"
    assert full_stream["headers"]["Content-Length"] == "16"
    assert b"".join(full_stream["iterator"]) == b"0123456789ABCDEF"

    partial_stream = local_st.open_download_stream(
        "exports/test.bin",
        filename="test.bin",
        range_header="bytes=4-9",
    )
    assert partial_stream["status_code"] == 206
    assert partial_stream["headers"]["Content-Range"] == "bytes 4-9/16"
    assert partial_stream["headers"]["Content-Length"] == "6"
    assert b"".join(partial_stream["iterator"]) == b"456789"

    with pytest.raises(ValueError):
        parse_byte_range("bytes=50-60", 16)


def test_edit_distance_cer_and_wer():
    from app.tasks import compute_cer_and_wer

    exact_ta = compute_cer_and_wer("தமிழ் ஆவணம்", "தமிழ் ஆவணம்")
    assert exact_ta["cer"] == 0.0
    assert exact_ta["wer"] == 0.0
    assert exact_ta["char_accuracy"] == 1.0

    exact_en = compute_cer_and_wer("TAMIL OCR STUDIO 2026", "TAMIL OCR STUDIO 2026")
    assert exact_en["cer"] == 0.0
    assert exact_en["wer"] == 0.0
    assert exact_en["char_accuracy"] == 1.0

    # 1 substitution in 4-word, 21-char string -> WER = 1/4 = 0.25, CER = 1/21 = 0.0476
    sub_en = compute_cer_and_wer("TAMIL OCR STUDIO 2025", "TAMIL OCR STUDIO 2026")
    assert sub_en["wer"] == 0.25
    assert sub_en["cer"] == round(1 / 21, 4)


def test_retention_cleanup_and_storage_budget():
    from datetime import datetime, timedelta, timezone
    import pytest
    from app.database import Base, SessionLocal, engine
    from app.models import ExportArtifact, Job, User
    from app.services.retention import (
        StorageQuotaExceededError,
        cleanup_expired_resources,
        enforce_storage_budget_and_quota,
    )

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = User(
            email="quota-check@studio.local",
            hashed_password="x",
            role="user",
            storage_quota_bytes=750_000_000,
            retention_days=7,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        expired_job = Job(
            user_id=user.id,
            original_filename="expired.pdf",
            format_type="pdf",
            file_size_bytes=400_000_000,
            original_storage_key="uploads/expired.pdf",
            status="completed",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        active_job = Job(
            user_id=user.id,
            original_filename="active.pdf",
            format_type="pdf",
            file_size_bytes=450_000_000,
            original_storage_key="uploads/active.pdf",
            status="completed",
            expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        )
        db.add_all([expired_job, active_job])
        db.commit()

        # Opportunistic cleanup removes the 400 MB expired job, allowing a 250 MB upload (450 + 250 = 700 <= 750 MB)
        snap = enforce_storage_budget_and_quota(db, user, 250_000_000)
        assert snap["user_used_bytes"] == 450_000_000

        # A 350 MB upload exceeds the 750 MB per-user quota (450 + 350 = 800 > 750 MB)
        with pytest.raises(StorageQuotaExceededError, match="quota exceeded"):
            enforce_storage_budget_and_quota(db, user, 350_000_000)
    finally:
        db.query(ExportArtifact).delete()
        db.query(Job).delete()
        db.query(User).filter(User.email == "quota-check@studio.local").delete()
        db.commit()
        db.close()


def test_direct_vercel_blob_multipart_upload_and_range_download_e2e(monkeypatch):
    import hashlib
    from fastapi.testclient import TestClient
    from app.main import app
    from app.storage import storage
    from app.tasks import finalize_upload_sync

    rw_token = "vercel_blob_rw_storee2e99_secretmasterkey"
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", rw_token)
    monkeypatch.setattr(storage, "backend", "vercel_blob")

    sample_pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
    expected_sha256 = hashlib.sha256(sample_pdf).hexdigest()
    completed_mpu = {}

    class _FakeStreamRes:
        def __init__(self, data: bytes, status_code: int = 200, headers: dict | None = None):
            self._data = data
            self.status_code = status_code
            self.headers = headers or {"content-length": str(len(data)), "content-type": "application/pdf"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def iter_bytes(self, chunk_size: int = 262144):
            for i in range(0, len(self._data), chunk_size):
                yield self._data[i : i + chunk_size]

        def close(self):
            pass

    class _FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, headers=None, content=None):
            action = (headers or {}).get("x-mpu-action")
            if action == "create":
                class _R:
                    status_code = 200
                    def raise_for_status(self): pass
                    def json(self): return {"key": "uploads/key.pdf", "uploadId": "blob_mpu_001"}
                return _R()
            if action == "complete":
                completed_mpu["parts"] = content
                class _R:
                    status_code = 200
                    def raise_for_status(self): pass
                    def json(self): return {"url": "https://storee2e99.private.blob.vercel-storage.com/uploads/key.pdf"}
                return _R()
            class _R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {}
            return _R()

        def get(self, url, headers=None):
            class _R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self):
                    if "url=" in url:
                        return {"size": len(sample_pdf), "url": url}
                    return {"blobs": [], "hasMore": False}
            return _R()

        def stream(self, method, url, headers=None):
            rng = (headers or {}).get("Range")
            if rng and rng.startswith("bytes=0-"):
                end = int(rng.split("-")[1])
                return _FakeStreamRes(sample_pdf[: end + 1], status_code=206)
            return _FakeStreamRes(sample_pdf, status_code=200)

        def build_request(self, method, url, headers=None):
            return {"method": method, "url": url, "headers": headers or {}}

        def send(self, req, stream=False):
            rng = req["headers"].get("Range")
            if rng == "bytes=0-7":
                slice_data = sample_pdf[:8]
                return _FakeStreamRes(
                    slice_data,
                    status_code=206,
                    headers={
                        "content-length": "8",
                        "content-range": f"bytes 0-7/{len(sample_pdf)}",
                        "content-type": "application/pdf",
                    },
                )
            return _FakeStreamRes(sample_pdf, status_code=200)

        def close(self):
            pass

    monkeypatch.setattr("app.storage.httpx.Client", _FakeHttpxClient)

    client = TestClient(app)
    guest = client.post("/api/v1/auth/guest").json()
    headers = {"Authorization": f"Bearer {guest['access_token']}"}

    init_res = client.post(
        "/api/v1/uploads/init",
        headers=headers,
        json={"filename": "bilingual.pdf", "expected_size_bytes": len(sample_pdf)},
    )
    assert init_res.status_code == 201, init_res.text
    init_data = init_res.json()
    assert init_data["upload_mode"] == "direct_blob"
    sid = init_data["session_id"]

    part_url_res = client.get(f"/api/v1/uploads/{sid}/part-url/0", headers=headers)
    assert part_url_res.status_code == 200
    part_target = part_url_res.json()
    assert part_target["upload_mode"] == "direct_blob"
    assert part_target["method"] == "POST"
    assert rw_token not in str(part_target)

    reg_res = client.post(
        f"/api/v1/uploads/{sid}/parts/0",
        headers=headers,
        json={"etag": '"etag-blob-part-1"', "size_bytes": len(sample_pdf)},
    )
    assert reg_res.status_code == 200
    assert reg_res.json()["received_chunks"] == [0]

    finalize_upload_sync(sid)

    status_res = client.get(f"/api/v1/uploads/{sid}", headers=headers)
    assert status_res.status_code == 200
    st_data = status_res.json()
    assert st_data["status"] == "completed"
    assert st_data["format_type"] == "pdf"
    assert st_data["sha256_hash"] == expected_sha256

    job_res = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={"upload_session_id": sid, "settings": {"language": "tam+eng"}},
    )
    assert job_res.status_code == 201
    job_id = job_res.json()["job_id"]

    orig_link_res = client.get(f"/api/v1/jobs/{job_id}/original/link", headers=headers)
    assert orig_link_res.status_code == 200
    dl_path = orig_link_res.json()["download_url"]
    assert orig_link_res.json()["direct_storage"] is False

    # Range download (first 8 bytes "%PDF-1.4")
    range_dl = client.get(dl_path, headers={"Range": "bytes=0-7"})
    assert range_dl.status_code == 206
    assert range_dl.headers["accept-ranges"] == "bytes"
    assert range_dl.headers["content-range"] == f"bytes 0-7/{len(sample_pdf)}"
    assert range_dl.content == b"%PDF-1.4"


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
