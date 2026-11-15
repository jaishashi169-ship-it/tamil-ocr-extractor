from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import threading
import time
import zipfile
from unittest.mock import MagicMock, patch

import fitz
import openpyxl
import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Job, PageResult
from app.services.study.ai_provider import AIStudyItemSchema, AIStudySupportSchema
from app.tasks import process_extraction_job_sync, process_extraction_job_task

client = TestClient(app)


def _auth_headers() -> dict:
    res = client.post("/api/v1/auth/guest")
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _upload_chunks(headers: dict, filename: str, data: bytes, chunk_size: int = 5_242_880) -> str:
    init_res = client.post(
        "/api/v1/uploads/init",
        json={
            "filename": filename,
            "expected_size_bytes": len(data),
            "chunk_size_bytes": max(65_536, chunk_size),
        },
        headers=headers,
    )
    assert init_res.status_code == 201, init_res.text
    sid = init_res.json()["session_id"]

    eff_chunk = init_res.json()["chunk_size_bytes"]
    idx = 0
    for offset in range(0, len(data), eff_chunk):
        part = data[offset : offset + eff_chunk]
        c_res = client.put(
            f"/api/v1/uploads/{sid}/chunk/{idx}",
            files={"chunk": (f"chunk_{idx}", part, "application/octet-stream")},
            headers=headers,
        )
        assert c_res.status_code == 200, c_res.text
        idx += 1

    comp_res = client.post(f"/api/v1/uploads/{sid}/complete", headers=headers)
    assert comp_res.status_code in {200, 202}, comp_res.text
    return sid


def _poll_job_until_terminal(
    headers: dict,
    job_id: str,
    timeout_s: float = 15.0,
    interval_s: float = 0.05,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last_job: dict = {}
    while time.monotonic() < deadline:
        res = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        assert res.status_code == 200, res.text
        last_job = res.json()
        if last_job.get("status") in {"completed", "failed", "cancelled"}:
            return last_job
        time.sleep(interval_s)
    raise AssertionError(
        f"Job {job_id} timed out after {timeout_s}s; "
        f"last_status={last_job.get('status')}, "
        f"stage={last_job.get('stage_label')}, "
        f"error={last_job.get('error_message')}"
    )


# ---------------------------------------------------------------------------
# 1. Job Dispatch Hardening & Idempotency Tests
# ---------------------------------------------------------------------------


def test_production_dispatch_requires_celery_and_fails_job_on_redis_unavailable(monkeypatch):
    headers = _auth_headers()
    sid = _upload_chunks(headers, "prod_test.txt", "தமிழ் மற்றும் English".encode("utf-8"))

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://127.0.0.1:6399/0")
    get_settings.cache_clear()
    try:
        res = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers)
        assert res.status_code == 503
        detail = res.json()["detail"]
        assert detail["code"] == "QUEUE_UNAVAILABLE"
        assert detail["retryable"] is True
        assert res.headers.get("Retry-After") == "5"

        # Job must NOT be left permanently queued
        job_res = client.get(f"/api/v1/jobs/{detail['job_id']}", headers=headers)
        assert job_res.status_code == 200
        assert job_res.json()["status"] == "failed"
        assert job_res.json()["stage_label"] == "Dispatch unavailable"
    finally:
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
        get_settings.cache_clear()


def test_dev_dispatch_without_fallback_flag_returns_503(monkeypatch):
    headers = _auth_headers()
    sid = _upload_chunks(headers, "no_fallback.txt", b"Hello world")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ALLOW_BACKGROUND_TASK_FALLBACK", "false")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://127.0.0.1:6399/0")
    get_settings.cache_clear()
    try:
        res = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers)
        assert res.status_code == 503
        job_id = res.json()["detail"]["job_id"]
        job_res = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
        assert job_res["status"] == "failed"
    finally:
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("ALLOW_BACKGROUND_TASK_FALLBACK", raising=False)
        monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
        get_settings.cache_clear()


def test_publish_failure_after_redis_ping_succeeds(monkeypatch):
    headers = _auth_headers()
    sid = _upload_chunks(headers, "pub_fail.txt", "Publish failure test".encode("utf-8"))

    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    # Case A: Production -> publish failure must return 503 & mark job failed
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        with patch("app.dispatch.is_broker_reachable", return_value=True), patch(
            "app.dispatch.celery_app.send_task",
            side_effect=RuntimeError("Broker connection dropped during publish"),
        ):
            res = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers)
            assert res.status_code == 503
            job_id = res.json()["detail"]["job_id"]
            assert client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()["status"] == "failed"
    finally:
        monkeypatch.delenv("APP_ENV", raising=False)
        get_settings.cache_clear()

    # Case B: Development with ALLOW_BACKGROUND_TASK_FALLBACK=true -> falls back cleanly
    sid2 = _upload_chunks(headers, "pub_fail_dev.txt", "Fallback succeeds".encode("utf-8"))
    with patch("app.dispatch.is_broker_reachable", return_value=True), patch(
        "app.dispatch.celery_app.send_task",
        side_effect=RuntimeError("Broker connection dropped during publish"),
    ):
        res2 = client.post("/api/v1/jobs", json={"upload_session_id": sid2}, headers=headers)
        assert res2.status_code == 201
        job2 = _poll_job_until_terminal(headers, res2.json()["job_id"])
        assert job2["status"] == "completed"


def test_worker_idempotency_duplicate_delivery_and_restart_recovery():
    headers = _auth_headers()
    sid = _upload_chunks(headers, "idem.txt", "Idempotent worker test\nதமிழ்".encode("utf-8"))

    # Idempotency key prevents duplicate job creation
    j1 = client.post(
        "/api/v1/jobs",
        json={"upload_session_id": sid, "idempotency_key": "idem-key-123"},
        headers=headers,
    ).json()
    j2 = client.post(
        "/api/v1/jobs",
        json={"upload_session_id": sid, "idempotency_key": "idem-key-123"},
        headers=headers,
    ).json()
    assert j1["job_id"] == j2["job_id"]

    final = _poll_job_until_terminal(headers, j1["job_id"])
    assert final["status"] == "completed"

    # Duplicate delivery on completed job must be a no-op (no duplicate PageResult rows)
    process_extraction_job_task.run(j1["job_id"])
    db = SessionLocal()
    try:
        page_count = db.query(PageResult).filter(PageResult.job_id == j1["job_id"]).count()
        assert page_count == 1

        # Simulate worker crash leaving job in 'running' state with a LIVE (unexpired) lease
        job_row = db.query(Job).filter(Job.id == j1["job_id"]).first()
        job_row.status = "running"
        job_row.execution_token = "live-worker-token-1"
        job_row.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=120)
        db.commit()
    finally:
        db.close()

    # Live lease must NOT be reclaimed even on redelivery
    process_extraction_job_sync(j1["job_id"], reclaim_stale=True)
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.id == j1["job_id"]).first()
        assert row.status == "running"
        assert row.execution_token == "live-worker-token-1"

        # Now expire the lease -> recovery must claim with a new execution_token and complete
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        db.commit()
    finally:
        db.close()

    process_extraction_job_sync(j1["job_id"])
    db = SessionLocal()
    try:
        recovered = db.query(Job).filter(Job.id == j1["job_id"]).first()
        assert recovered.status == "completed"
        assert recovered.execution_token != "live-worker-token-1"
        assert db.query(PageResult).filter(PageResult.job_id == j1["job_id"]).count() == 1

        # Stale worker with old token cannot overwrite the newer execution
        from app.tasks import _mark_job_terminal_if_owned
        wrote = _mark_job_terminal_if_owned(
            db, j1["job_id"], "live-worker-token-1", {Job.status: "failed", Job.stage_label: "Stale overwrite"}
        )
        assert wrote is False
        assert db.query(Job).filter(Job.id == j1["job_id"]).first().status == "completed"
    finally:
        db.close()


def test_production_rejects_placeholder_secret():
    from app.config import Settings

    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(APP_ENV="production", SECRET_KEY="dev-insecure-secret-change-in-production").validate_production_safety()

    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(APP_ENV="production", SECRET_KEY="too-short").validate_production_safety()

    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(APP_ENV="production", SECRET_KEY="A" * 40, CORS_ORIGINS="*").validate_production_safety()

    # Strong 32+ char secret + explicit CORS origin + durable PostgreSQL & S3 succeeds
    Settings(
        APP_ENV="production",
        SECRET_KEY="A" * 40,
        CORS_ORIGINS="https://tamil-doc-studio.vercel.app",
        ALLOW_BACKGROUND_TASK_FALLBACK=False,
        DATABASE_URL="postgresql+asyncpg://user:pass@db:5432/app",
        SYNC_DATABASE_URL="postgresql://user:pass@db:5432/app",
        STORAGE_BACKEND="s3",
        S3_ACCESS_KEY="prod-access-key",
        S3_SECRET_KEY="prod-secret-key",
    ).validate_production_safety()


# ---------------------------------------------------------------------------
# 2. Async Polling & Thread/Celery Execution Tests
# ---------------------------------------------------------------------------


def test_async_polling_and_timeout_failure():
    headers = _auth_headers()
    sid = _upload_chunks(headers, "async_poll.txt", "Async polling verification".encode("utf-8"))

    # Create job without running background task yet by mocking _dispatch_job to run in a delayed thread
    def _delayed_worker(bg, job, db, password):
        def _run():
            time.sleep(0.2)
            process_extraction_job_task.apply(args=[job.id, password])

        threading.Thread(target=_run, daemon=True).start()

    with patch("app.routers.jobs._dispatch_job", side_effect=_delayed_worker):
        created = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers).json()
        assert created["status"] == "queued"

        # Bounded polling with too-short timeout must fail with last_status
        with pytest.raises(AssertionError, match="last_status=queued"):
            _poll_job_until_terminal(headers, created["job_id"], timeout_s=0.05, interval_s=0.02)

        # Bounded polling with sufficient timeout completes via Celery task execution
        done = _poll_job_until_terminal(headers, created["job_id"], timeout_s=10.0, interval_s=0.05)
        assert done["status"] == "completed"


# ---------------------------------------------------------------------------
# 3. Upload Boundary, Resumability, Auth, SHA-256 & Export Verification
# ---------------------------------------------------------------------------


def test_streamed_upload_boundary_resumability_auth_and_sha256(monkeypatch):
    headers_a = _auth_headers()
    headers_b = _auth_headers()

    # Exact 500,000,000 init allowed, 500,000,001 rejected
    ok = client.post(
        "/api/v1/uploads/init",
        json={"filename": "max500.pdf", "expected_size_bytes": 500_000_000},
        headers=headers_a,
    )
    assert ok.status_code == 201
    client.delete(f"/api/v1/uploads/{ok.json()['session_id']}", headers=headers_a)

    over = client.post(
        "/api/v1/uploads/init",
        json={"filename": "over500.pdf", "expected_size_bytes": 500_000_001},
        headers=headers_a,
    )
    assert over.status_code == 413

    # Streamed chunked upload exceeding MAX_UPLOAD_BYTES (using bounded 200 KB cap for fast streamed fixture)
    monkeypatch.setattr("app.routers.uploads.settings.MAX_UPLOAD_BYTES", 200_000)
    init_stream = client.post(
        "/api/v1/uploads/init",
        json={"filename": "stream_test.txt", "expected_size_bytes": 200_000, "chunk_size_bytes": 100_000},
        headers=headers_a,
    )
    assert init_stream.status_code == 201
    sid_stream = init_stream.json()["session_id"]

    # Chunk 0: 100,000 bytes OK
    r0 = client.put(
        f"/api/v1/uploads/{sid_stream}/chunk/0",
        files={"chunk": ("c0", b"A" * 100_000, "application/octet-stream")},
        headers=headers_a,
    )
    assert r0.status_code == 200
    # Chunk 1: 100,001 bytes -> streamed total 200,001 > 200,000 -> rejected 413
    r1 = client.put(
        f"/api/v1/uploads/{sid_stream}/chunk/1",
        files={"chunk": ("c1", b"B" * 100_001, "application/octet-stream")},
        headers=headers_a,
    )
    assert r1.status_code == 413
    client.delete(f"/api/v1/uploads/{sid_stream}", headers=headers_a)
    monkeypatch.undo()

    # Resumable upload recovery (pause -> status -> resume -> complete) + cross-user auth
    payload = ("Resumable Tamil-English upload: தமிழ்\n" * 4000).encode("utf-8")  # ~172 KB
    expected_sha = hashlib.sha256(payload).hexdigest()
    init_r = client.post(
        "/api/v1/uploads/init",
        json={"filename": "resume.txt", "expected_size_bytes": len(payload), "chunk_size_bytes": 65_536},
        headers=headers_a,
    )
    assert init_r.status_code == 201
    sid = init_r.json()["session_id"]
    total_chunks = init_r.json()["total_chunks"]
    assert total_chunks >= 2

    # Upload first chunk, then pause
    c0 = client.put(
        f"/api/v1/uploads/{sid}/chunk/0",
        files={"chunk": ("c0", payload[:65_536], "application/octet-stream")},
        headers=headers_a,
    )
    assert c0.status_code == 200
    assert client.post(f"/api/v1/uploads/{sid}/pause", headers=headers_a).status_code == 200

    # Cross-user authorization check: User B cannot inspect, upload to, or complete User A's session
    assert client.get(f"/api/v1/uploads/{sid}", headers=headers_b).status_code == 404
    assert (
        client.put(
            f"/api/v1/uploads/{sid}/chunk/1",
            files={"chunk": ("c1", payload[65_536:131_072], "application/octet-stream")},
            headers=headers_b,
        ).status_code
        == 404
    )

    # User A resumes remaining chunks
    status_res = client.get(f"/api/v1/uploads/{sid}", headers=headers_a).json()
    assert status_res["received_chunks"] == [0]
    for idx in range(1, total_chunks):
        part = payload[idx * 65_536 : (idx + 1) * 65_536]
        assert (
            client.put(
                f"/api/v1/uploads/{sid}/chunk/{idx}",
                files={"chunk": (f"c{idx}", part, "application/octet-stream")},
                headers=headers_a,
            ).status_code
            == 200
        )

    comp_res = client.post(f"/api/v1/uploads/{sid}/complete", headers=headers_a)
    assert comp_res.status_code in {200, 202}, comp_res.text
    comp = client.get(f"/api/v1/uploads/{sid}", headers=headers_a).json()
    assert comp["status"] == "completed"
    assert comp["sha256_hash"] == expected_sha

    # Start job & verify unchanged original download SHA-256
    job = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers_a).json()
    done = _poll_job_until_terminal(headers_a, job["job_id"])
    assert done["status"] == "completed"

    # Cross-user job access blocked
    assert client.get(f"/api/v1/jobs/{job['job_id']}", headers=headers_b).status_code == 404

    orig_link = client.get(f"/api/v1/jobs/{job['job_id']}/original/link", headers=headers_a).json()
    orig_dl = client.get(orig_link["download_url"])
    assert orig_dl.status_code == 200
    assert hashlib.sha256(orig_dl.content).hexdigest() == expected_sha


def test_compression_presets_and_export_file_opening():
    headers = _auth_headers()

    # Build a multi-page PDF with embedded text and a smooth photographic-style raster image for compression preset testing
    pdf_doc = fitz.open()
    for page_idx in range(2):
        page = pdf_doc.new_page(width=595, height=842)
        page.insert_text((72, 72), f"Page {page_idx + 1}: Tamil English Studio Export Verification", fontsize=14)
        img = Image.new("RGB", (1000, 1200), (245, 245, 250))
        draw = ImageDraw.Draw(img)
        for i in range(0, 1000, 20):
            draw.rectangle([(i, 0), (i + 20, 1200)], fill=(200 - (i % 150), 210 - (i % 100), 240))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page.insert_image(fitz.Rect(72, 120, 520, 720), stream=buf.getvalue())
    pdf_bytes = pdf_doc.tobytes()
    pdf_doc.close()

    sid = _upload_chunks(headers, "visual_sample.pdf", pdf_bytes)
    job = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers).json()
    done = _poll_job_until_terminal(headers, job["job_id"])
    assert done["status"] == "completed"
    jid = job["job_id"]

    # Edit, save, and reload Tamil + English text
    tamil_edited = "தமிழ் மற்றும் English\nஅத்தியாயம் 1: ஒளிச்சேர்க்கை (Photosynthesis)."
    save_res = client.put(
        f"/api/v1/jobs/{jid}/pages/1",
        json={"edited_text": tamil_edited, "is_autosave": False, "expected_version": 0},
        headers=headers,
    )
    assert save_res.status_code == 200
    reloaded = client.get(f"/api/v1/jobs/{jid}/pages", headers=headers).json()
    assert reloaded["pages"][0]["edited_text"] == tamil_edited

    # Verify compression presets (high, balanced, smallest) on searchable_pdf produce valid PDFs
    preset_sizes = {}
    for preset in ["high", "balanced", "smallest"]:
        exp = client.post(
            f"/api/v1/jobs/{jid}/exports",
            json={"export_format": "searchable_pdf", "quality_preset": preset},
            headers=headers,
        )
        assert exp.status_code in {201, 202}, exp.text
        link = client.get(f"/api/v1/jobs/{jid}/exports/{exp.json()['artifact_id']}/link", headers=headers).json()
        dl = client.get(link["download_url"])
        assert dl.status_code == 200
        opened = fitz.open(stream=dl.content, filetype="pdf")
        assert len(opened) >= 1
        opened.close()
        preset_sizes[preset] = len(dl.content)

    print(f"Searchable PDF preset sizes (bytes): {preset_sizes}")
    assert all(sz > 0 for sz in preset_sizes.values())
    assert preset_sizes["smallest"] <= preset_sizes["high"]
    assert preset_sizes["smallest"] <= preset_sizes["balanced"]

    # Also test image preview compression presets (jpg: high, balanced, smallest)
    img_sizes = {}
    for preset in ["high", "balanced", "smallest"]:
        exp = client.post(
            f"/api/v1/jobs/{jid}/exports",
            json={"export_format": "jpg", "quality_preset": preset},
            headers=headers,
        )
        assert exp.status_code in {201, 202}, exp.text
        link = client.get(f"/api/v1/jobs/{jid}/exports/{exp.json()['artifact_id']}/link", headers=headers).json()
        dl = client.get(link["download_url"])
        assert dl.status_code == 200
        opened_img = Image.open(io.BytesIO(dl.content))
        assert opened_img.size[0] > 0
        img_sizes[preset] = len(dl.content)
    print(f"JPG preview preset sizes (bytes): {img_sizes}")
    assert img_sizes["smallest"] <= img_sizes["balanced"] <= img_sizes["high"]

    # Open & validate all other export formats
    for fmt in ["txt", "md", "docx", "pdf", "html", "json", "xlsx", "csv"]:
        exp = client.post(
            f"/api/v1/jobs/{jid}/exports",
            json={"export_format": fmt, "quality_preset": "high"},
            headers=headers,
        )
        assert exp.status_code in {201, 202}, f"{fmt} failed: {exp.text}"
        link = client.get(f"/api/v1/jobs/{jid}/exports/{exp.json()['artifact_id']}/link", headers=headers).json()
        dl = client.get(link["download_url"])
        assert dl.status_code == 200
        if fmt in {"txt", "md", "html", "csv"}:
            text_out = dl.content.decode("utf-8")
            assert "Photosynthesis" in text_out
            if fmt in {"txt", "md", "html"}:
                assert "தமிழ்" in text_out
        elif fmt == "json":
            parsed = json.loads(dl.content.decode("utf-8"))
            assert len(parsed["pages"]) == 2
        elif fmt == "docx":
            doc = DocxDocument(io.BytesIO(dl.content))
            assert "தமிழ்" in "\n".join(p.text for p in doc.paragraphs)
        elif fmt == "pdf":
            opened = fitz.open(stream=dl.content, filetype="pdf")
            assert len(opened) >= 1
            pdf_text = "\n".join(p.get_text() for p in opened)
            opened.close()
            assert "Photosynthesis" in pdf_text
        elif fmt == "xlsx":
            wb = openpyxl.load_workbook(io.BytesIO(dl.content))
            assert len(wb.sheetnames) >= 1


def test_direct_s3_multipart_upload_and_presigned_downloads():
    headers = _auth_headers()
    img = Image.new("RGB", (64, 64), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    expected_sha = hashlib.sha256(png_bytes).hexdigest()

    from app.routers import exports as exports_router
    from app.routers import uploads as uploads_router

    with (
        patch.object(uploads_router.storage, "is_s3", return_value=True),
        patch.object(uploads_router.storage, "create_multipart_upload", return_value="s3-up-123") as mock_create_mpu,
        patch.object(
            uploads_router.storage,
            "presign_upload_part",
            return_value="https://s3.example.com/bucket/key?partNumber=1&X-Amz-Signature=abc",
        ) as mock_presign_part,
        patch.object(uploads_router.storage, "complete_multipart_upload") as mock_complete_mpu,
        patch.object(uploads_router.storage, "abort_multipart_upload") as mock_abort_mpu,
        patch.object(uploads_router.storage, "head_object_size", return_value=len(png_bytes)),
        patch.object(uploads_router.storage, "read_head_bytes", return_value=png_bytes[:64]),
        patch.object(uploads_router.storage, "compute_sha256_streaming", return_value=(len(png_bytes), expected_sha)),
        patch.object(exports_router.storage, "is_s3", return_value=True),
        patch.object(
            exports_router.storage,
            "presign_download_url",
            return_value="https://s3.example.com/bucket/export.txt?X-Amz-Signature=dl123",
        ) as mock_presign_dl,
        patch.object(exports_router.storage, "save_bytes", return_value=256),
        patch.object(exports_router.storage, "read_bytes", return_value=b"sample-export-bytes"),
    ):
        # 1. Init S3 multipart session
        init_res = client.post(
            "/api/v1/uploads/init",
            json={
                "filename": "direct.png",
                "expected_size_bytes": len(png_bytes),
                "chunk_size_bytes": 65_536,
            },
            headers=headers,
        )
        assert init_res.status_code == 201, init_res.text
        body = init_res.json()
        assert body["upload_mode"] == "direct_s3"
        assert body["chunk_size_bytes"] >= 8_388_608
        sid = body["session_id"]
        mock_create_mpu.assert_called_once()

        # 2. Presign part URL
        part_url_res = client.get(f"/api/v1/uploads/{sid}/part-url/0", headers=headers)
        assert part_url_res.status_code == 200, part_url_res.text
        assert part_url_res.json()["part_number"] == 1
        assert part_url_res.json()["upload_url"].startswith("https://s3.example.com/")
        mock_presign_part.assert_called_once()

        # 3. Register uploaded part ETag
        reg_res = client.post(
            f"/api/v1/uploads/{sid}/parts/0",
            json={"etag": '"etag-part-1"'},
            headers=headers,
        )
        assert reg_res.status_code == 200, reg_res.text
        assert reg_res.json()["received_chunks"] == [0]

        # 4. Status reflects direct_s3
        status_res = client.get(f"/api/v1/uploads/{sid}", headers=headers)
        assert status_res.status_code == 200
        assert status_res.json()["upload_mode"] == "direct_s3"
        assert status_res.json()["received_chunks"] == [0]

        # 5. Complete S3 multipart upload (worker finalization)
        comp_res = client.post(f"/api/v1/uploads/{sid}/complete", headers=headers)
        assert comp_res.status_code in {200, 202}, comp_res.text
        final_status = client.get(f"/api/v1/uploads/{sid}", headers=headers).json()
        assert final_status["status"] == "completed"
        assert final_status["sha256_hash"] == expected_sha
        mock_complete_mpu.assert_called_once()

        # 6. Abort an in-progress S3 session calls abort_multipart_upload
        abort_init = client.post(
            "/api/v1/uploads/init",
            json={
                "filename": "abort_me.png",
                "expected_size_bytes": len(png_bytes),
                "chunk_size_bytes": 65_536,
            },
            headers=headers,
        ).json()
        del_res = client.delete(f"/api/v1/uploads/{abort_init['session_id']}", headers=headers)
        assert del_res.status_code == 204
        mock_abort_mpu.assert_called_once()

        # 7. Reject oversized S3 object (> 500 MB) and invalid magic bytes in worker finalization
        oversize_init = client.post(
            "/api/v1/uploads/init",
            json={
                "filename": "huge.png",
                "expected_size_bytes": len(png_bytes),
                "chunk_size_bytes": 65_536,
            },
            headers=headers,
        ).json()
        osid = oversize_init["session_id"]
        client.post(f"/api/v1/uploads/{osid}/parts/0", json={"etag": '"e1"'}, headers=headers)
        with patch.object(uploads_router.storage, "head_object_size", return_value=get_settings().MAX_UPLOAD_BYTES + 1):
            over_res = client.post(f"/api/v1/uploads/{osid}/complete", headers=headers)
            assert over_res.status_code in {200, 202}
            assert client.get(f"/api/v1/uploads/{osid}", headers=headers).json()["status"] == "aborted"
            assert client.post("/api/v1/jobs", json={"upload_session_id": osid}, headers=headers).status_code == 400

        bad_init = client.post(
            "/api/v1/uploads/init",
            json={
                "filename": "spoof.png",
                "expected_size_bytes": len(png_bytes),
                "chunk_size_bytes": 65_536,
            },
            headers=headers,
        ).json()
        bsid = bad_init["session_id"]
        client.post(f"/api/v1/uploads/{bsid}/parts/0", json={"etag": '"e1"'}, headers=headers)
        with patch.object(uploads_router.storage, "read_head_bytes", return_value=b"NOT_A_VALID_PNG_HEADER"):
            bad_res = client.post(f"/api/v1/uploads/{bsid}/complete", headers=headers)
            assert bad_res.status_code in {200, 202}
            assert client.get(f"/api/v1/uploads/{bsid}", headers=headers).json()["status"] == "aborted"
            assert client.post("/api/v1/jobs", json={"upload_session_id": bsid}, headers=headers).status_code == 400

        # 8. Presigned download links for original, export, and batch zip
        job_res = client.post(
            "/api/v1/jobs",
            json={"upload_session_id": sid, "settings": {"language_mode": "tamil_english"}},
            headers=headers,
        )
        assert job_res.status_code == 201, job_res.text
        jid = job_res.json()["job_id"]

        orig_link = client.get(f"/api/v1/jobs/{jid}/original/link", headers=headers)
        assert orig_link.status_code == 200
        assert orig_link.json()["direct_storage"] is True
        assert orig_link.json()["download_url"].startswith("https://s3.example.com/")

        with SessionLocal() as db:
            from app.models import ExportArtifact
            job_row = db.query(Job).filter(Job.id == jid).first()
            assert job_row is not None
            job_row.status = "completed"
            db.add(
                PageResult(
                    job_id=jid,
                    page_number=1,
                    raw_text="Direct S3 Test",
                    edited_text="Direct S3 Test",
                    language_detected="en",
                    confidence_avg=0.99,
                    extraction_method="direct",
                )
            )
            art = ExportArtifact(
                job_id=jid,
                user_id=job_row.user_id,
                export_format="txt",
                quality_preset="high",
                storage_key=f"jobs/{jid}/exports/test.txt",
                file_size_bytes=19,
            )
            db.add(art)
            db.commit()
            db.refresh(art)
            art_id = art.id

        exp_link = client.get(f"/api/v1/jobs/{jid}/exports/{art_id}/link", headers=headers)
        assert exp_link.status_code == 200
        assert exp_link.json()["direct_storage"] is True
        assert exp_link.json()["download_url"].startswith("https://s3.example.com/")

        batch_res = client.post(
            "/api/v1/jobs/batch/zip",
            json={"job_ids": [jid], "export_format": "txt", "quality_preset": "high"},
            headers=headers,
        )
        assert batch_res.status_code in {200, 202}, batch_res.text
        batch_art_id = batch_res.json()["artifact_id"]
        batch_link = client.get(f"/api/v1/jobs/{jid}/exports/{batch_art_id}/link", headers=headers)
        assert batch_link.status_code == 200
        assert batch_link.json()["direct_storage"] is True
        assert batch_link.json()["download_url"].startswith("https://s3.example.com/")
        assert mock_presign_dl.call_count >= 3
