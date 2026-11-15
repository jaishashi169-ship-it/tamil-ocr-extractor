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


def test_vercel_blob_sdk_adapter_and_range_streaming(monkeypatch, tmp_path):
    import pytest
    from app.storage import PrivateStorage, parse_byte_range

    rw_token = "vercel_blob_rw_storexyz789_mastersecretneverexpose"
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", rw_token)

    st = PrivateStorage()
    st.backend = "vercel_blob"
    assert st.is_vercel_blob() is True
    assert st.is_direct_multipart() is True
    assert st.direct_upload_mode() == "vercel_blob"
    # Verify manual token signing is completely removed in favor of official @vercel/blob/client + vercel-blob SDK
    assert not hasattr(st, "generate_blob_client_token")

    # Manual part presigning is rejected for Vercel Blob in favor of @vercel/blob/client handleUpload
    with pytest.raises(RuntimeError, match="handleUpload"):
        st.get_direct_part_upload_target(
            key="uploads/u1/s1/sample.pdf",
            upload_id="vercel-blob-sdk-managed",
            part_number=1,
            max_size_bytes=500_000_000,
            expires_in=900,
        )

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


def test_atomic_storage_reservations_and_output_enforcement():
    """
    Verify atomic storage reservations cover:
    - Concurrent uploads
    - Originals, previews, exports, and temporary stored objects
    - Failed jobs and abandoned uploads releasing reserved bytes
    """
    from datetime import datetime, timedelta, timezone
    import pytest
    from app.database import Base, SessionLocal, engine
    from app.models import ExportArtifact, Job, StorageReservation, User
    from app.services.retention import (
        StorageQuotaExceededError,
        commit_storage_reservation,
        enforce_storage_budget_and_quota,
        get_storage_budget_snapshot,
        release_all_job_storage_and_reservations,
        release_reservations_for_resource,
        reserve_storage_capacity,
    )

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        db.query(StorageReservation).delete()
        db.query(ExportArtifact).delete()
        db.query(Job).delete()
        db.commit()

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
        db.refresh(active_job)

        # Opportunistic cleanup removes the 400 MB expired job, allowing a 250 MB upload (450 + 250 = 700 <= 750 MB)
        snap = enforce_storage_budget_and_quota(db, user, 250_000_000)
        assert snap["user_used_bytes"] == 450_000_000

        # Concurrent upload reservation #1 reserves 250 MB atomically -> total user usage becomes 700 MB
        reserve_storage_capacity(
            db,
            user=user,
            resource_type="upload",
            resource_id="sess-concurrent-1",
            reserved_bytes=250_000_000,
            storage_key="uploads/c1.pdf",
        )
        snap_after_c1 = get_storage_budget_snapshot(db, user=user)
        assert snap_after_c1["user_used_bytes"] == 700_000_000
        assert snap_after_c1["active_reserved_bytes"] >= 250_000_000

        # Concurrent upload reservation #2 of 100 MB is rejected because 700 + 100 = 800 > 750 MB quota!
        with pytest.raises(StorageQuotaExceededError, match="quota exceeded"):
            reserve_storage_capacity(
                db,
                user=user,
                resource_type="upload",
                resource_id="sess-concurrent-2",
                reserved_bytes=100_000_000,
                storage_key="uploads/c2.pdf",
            )

        # Output generation enforcement: preview/export exceeding remaining 50 MB quota is rejected
        with pytest.raises(StorageQuotaExceededError):
            reserve_storage_capacity(
                db,
                user=user,
                resource_type="preview",
                resource_id=f"{active_job.id}:1",
                reserved_bytes=60_000_000,
                storage_key=f"jobs/{active_job.id}/pages/page_0001.png",
            )

        # Preview within remaining 50 MB quota succeeds and commits actual size
        reserve_storage_capacity(
            db,
            user=user,
            resource_type="preview",
            resource_id=f"{active_job.id}:1",
            reserved_bytes=10_000_000,
            storage_key=f"jobs/{active_job.id}/pages/page_0001.png",
        )
        commit_storage_reservation(
            db,
            user_id=user.id,
            resource_type="preview",
            resource_id=f"{active_job.id}:1",
            actual_bytes=8_000_000,
        )
        assert get_storage_budget_snapshot(db, user=user)["active_committed_bytes"] >= 8_000_000

        # Abandoning upload #1 releases its 250 MB reservation immediately
        released_count = release_reservations_for_resource(
            db, resource_type="upload", resource_id="sess-concurrent-1"
        )
        assert released_count == 1
        assert get_storage_budget_snapshot(db, user=user)["user_used_bytes"] == 458_000_000

        # Releasing all storage/reservations for a failed or deleted job cleans up preview reservations
        release_all_job_storage_and_reservations(db, active_job, delete_original=False)
        assert get_storage_budget_snapshot(db, user=user)["active_committed_bytes"] == 0
    finally:
        db.query(StorageReservation).delete()
        db.query(ExportArtifact).delete()
        db.query(Job).delete()
        db.query(User).filter(User.email == "quota-check@studio.local").delete()
        db.commit()
        db.close()


def test_direct_vercel_blob_sdk_upload_and_range_download_e2e(monkeypatch):
    from contextlib import contextmanager
    import hashlib
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from app.main import app
    from app.storage import storage
    from app.tasks import finalize_upload_sync

    rw_token = "vercel_blob_rw_storee2e99_secretmasterkey"
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", rw_token)
    monkeypatch.setattr(storage, "backend", "vercel_blob")

    sample_pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
    expected_sha256 = hashlib.sha256(sample_pdf).hexdigest()
    fake_blob_url = "https://storee2e99.private.blob.vercel-storage.com/uploads/bilingual.pdf"

    # Mock official vercel.blob.sync SDK methods matching exact vercel-blob==0.6.0 signatures
    monkeypatch.setattr(
        "app.storage.vercel_blob_sync.head_blob",
        lambda url_or_path, **kwargs: SimpleNamespace(
            size=len(sample_pdf),
            url=fake_blob_url,
            pathname=str(url_or_path),
            content_type="application/pdf",
            etag='"etag-sdk-1"',
            cache_control="private, no-cache",
        ),
    )

    @contextmanager
    def _fake_stream_blob(url_or_path, *, access="private", **kwargs):
        def _gen():
            chunk_size = 262144
            for i in range(0, len(sample_pdf), chunk_size):
                yield sample_pdf[i : i + chunk_size]

        yield _gen()

    monkeypatch.setattr("app.storage.vercel_blob_sync.stream_blob", _fake_stream_blob)
    monkeypatch.setattr(
        "app.storage.vercel_blob_sync.get_blob",
        lambda url_or_path, *, access="private", **kwargs: SimpleNamespace(
            body=sample_pdf,
            metadata=SimpleNamespace(size=len(sample_pdf), status_code=200, content_type="application/pdf"),
        ),
    )
    monkeypatch.setattr(
        "app.storage.vercel_blob_sync.delete_blob",
        lambda url_or_path, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.storage.vercel_blob_sync.query_blobs",
        lambda **kwargs: iter([]),
    )

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
    assert init_data["upload_mode"] == "vercel_blob"
    assert init_data["handle_upload_url"] == "/api/blob/upload"
    assert init_data["upload_authorization_token"]
    assert rw_token not in json_dump_safe(init_data)
    sid = init_data["session_id"]

    # Verify Next.js server-side handleUpload authorization callback (/authorize-blob)
    auth_blob_res = client.post(
        f"/api/v1/uploads/{sid}/authorize-blob",
        json={
            "pathname": init_data["storage_key"],
            "upload_authorization_token": init_data["upload_authorization_token"],
        },
    )
    assert auth_blob_res.status_code == 200, auth_blob_res.text
    auth_blob_data = auth_blob_res.json()
    assert auth_blob_data["upload_id"] == sid
    assert auth_blob_data["expected_size_bytes"] == len(sample_pdf)

    # Rejects mismatched pathname
    bad_path_res = client.post(
        f"/api/v1/uploads/{sid}/authorize-blob",
        json={
            "pathname": "uploads/other/wrong.pdf",
            "upload_authorization_token": init_data["upload_authorization_token"],
        },
    )
    assert bad_path_res.status_code == 403

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

    # Range download (first 8 bytes "%PDF-1.4") via official SDK stream slicing
    range_dl = client.get(dl_path, headers={"Range": "bytes=0-7"})
    assert range_dl.status_code == 206
    assert range_dl.headers["accept-ranges"] == "bytes"
    assert range_dl.headers["content-range"] == f"bytes 0-7/{len(sample_pdf)}"
    assert range_dl.content == b"%PDF-1.4"


def json_dump_safe(obj) -> str:
    import json

    return json.dumps(obj)


def test_scheduled_retention_cron_and_restricted_upload_gating(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.main import app
    from app.tasks import run_scheduled_retention_cleanup_sync

    cfg = get_settings()
    client = TestClient(app)

    # 1. Verify worker scheduled retention cleanup runs independently of uploads
    sched_res = run_scheduled_retention_cleanup_sync()
    assert sched_res["status"] == "completed"
    assert "cleaned" in sched_res
    assert "storage_budget" in sched_res

    # 2. Verify platform cron endpoint /api/v1/internal/cron/retention requires secret and executes cleanup
    unauth_cron = client.get("/api/v1/internal/cron/retention")
    assert unauth_cron.status_code == 401

    monkeypatch.setattr(cfg, "CRON_SECRET", "cron-secret-test-999")
    auth_cron = client.get(
        "/api/v1/internal/cron/retention",
        headers={"Authorization": "Bearer cron-secret-test-999"},
    )
    assert auth_cron.status_code == 200, auth_cron.text
    assert auth_cron.json()["status"] == "completed"
    assert auth_cron.json()["trigger"] == "scheduled_cron"

    # 3. Verify RESTRICTED_SMOKE_TEST_MODE blocks public uploads unless valid X-Smoke-Test-Token is provided
    guest = client.post("/api/v1/auth/guest").json()
    headers = {"Authorization": f"Bearer {guest['access_token']}"}
    monkeypatch.setattr(cfg, "RESTRICTED_SMOKE_TEST_MODE", True)
    monkeypatch.setattr(cfg, "SMOKE_TEST_SECRET", "smoke-token-abc123")

    blocked_res = client.post(
        "/api/v1/uploads/init",
        headers=headers,
        json={"filename": "public.pdf", "expected_size_bytes": 1024},
    )
    assert blocked_res.status_code == 403
    assert blocked_res.json()["detail"]["code"] == "RESTRICTED_SMOKE_TEST_MODE"

    allowed_res = client.post(
        "/api/v1/uploads/init",
        headers={**headers, "X-Smoke-Test-Token": "smoke-token-abc123"},
        json={"filename": "smoke.pdf", "expected_size_bytes": 1024},
    )
    assert allowed_res.status_code == 201, allowed_res.text

    # 4. Verify REQUIRE_READINESS_FOR_UPLOADS blocks public uploads when readiness is not_ready
    monkeypatch.setattr(cfg, "RESTRICTED_SMOKE_TEST_MODE", False)
    monkeypatch.setattr(cfg, "REQUIRE_READINESS_FOR_UPLOADS", True)
    monkeypatch.setattr(
        "app.routers.admin.evaluate_readiness",
        lambda: {"status": "not_ready", "ready": False, "dependencies": {}},
    )
    not_ready_res = client.post(
        "/api/v1/uploads/init",
        headers=headers,
        json={"filename": "notready.pdf", "expected_size_bytes": 1024},
    )
    assert not_ready_res.status_code == 503
    assert not_ready_res.json()["detail"]["code"] == "SYSTEM_NOT_READY_FOR_UPLOADS"


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
