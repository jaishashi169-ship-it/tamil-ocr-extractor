import hashlib
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def _get_auth_headers():
    res = client.post("/api/v1/auth/guest")
    assert res.status_code == 200
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_500mb_limit_enforcement():
    headers = _get_auth_headers()
    # Exactly 500,000,000 bytes should be accepted at init
    ok_res = client.post(
        "/api/v1/uploads/init",
        json={"filename": "large.pdf", "expected_size_bytes": 500_000_000},
        headers=headers,
    )
    assert ok_res.status_code == 201
    client.delete(f"/api/v1/uploads/{ok_res.json()['session_id']}", headers=headers)

    # 500,000,001 bytes must be rejected
    over_res = client.post(
        "/api/v1/uploads/init",
        json={"filename": "oversized.pdf", "expected_size_bytes": 500_000_001},
        headers=headers,
    )
    assert over_res.status_code == 413


def test_end_to_end_upload_extract_edit_export_download_delete():
    headers = _get_auth_headers()
    sample_text = "அத்தியாயம் 1: தமிழ் இலக்கியம்\n\nTamil literature has a rich history.\n"
    raw_bytes = sample_text.encode("utf-8")
    orig_sha = hashlib.sha256(raw_bytes).hexdigest()

    # 1. Init chunked upload
    init_res = client.post(
        "/api/v1/uploads/init",
        json={"filename": "tamil_notes.txt", "expected_size_bytes": len(raw_bytes)},
        headers=headers,
    )
    assert init_res.status_code == 201
    session_id = init_res.json()["session_id"]

    # 2. Upload chunk 0
    chunk_res = client.put(
        f"/api/v1/uploads/{session_id}/chunk/0",
        files={"chunk": ("chunk0", raw_bytes, "application/octet-stream")},
        headers=headers,
    )
    assert chunk_res.status_code == 200

    # 3. Complete upload
    comp_res = client.post(f"/api/v1/uploads/{session_id}/complete", headers=headers)
    assert comp_res.status_code == 200
    assert comp_res.json()["sha256_hash"] == orig_sha

    # 4. Start job
    job_res = client.post(
        "/api/v1/jobs",
        json={"upload_session_id": session_id, "settings": {"language": "tam+eng"}},
        headers=headers,
    )
    assert job_res.status_code == 201
    job_id = job_res.json()["job_id"]

    # 5. Read job & pages (FastAPI BackgroundTasks runs before TestClient returns)
    status_res = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert status_res.status_code == 200
    assert status_res.json()["status"] == "completed"

    pages_res = client.get(f"/api/v1/jobs/{job_id}/pages", headers=headers)
    assert pages_res.status_code == 200
    assert "தமிழ்" in pages_res.json()["pages"][0]["raw_text"]

    # 6. Edit & autosave page 1
    edit_res = client.put(
        f"/api/v1/jobs/{job_id}/pages/1",
        json={"edited_text": sample_text + "\nReviewed note.", "is_autosave": True},
        headers=headers,
    )
    assert edit_res.status_code == 200
    assert edit_res.json()["saved"] is True

    # 7. Export DOCX, TXT, MD, PDF, JSON
    for fmt in ["txt", "md", "docx", "pdf", "json"]:
        exp_res = client.post(
            f"/api/v1/jobs/{job_id}/exports",
            json={"export_format": fmt, "quality_preset": "high"},
            headers=headers,
        )
        assert exp_res.status_code == 201
        art_id = exp_res.json()["artifact_id"]
        assert exp_res.json()["file_size_bytes"] > 0

        link_res = client.get(f"/api/v1/jobs/{job_id}/exports/{art_id}/link", headers=headers)
        assert link_res.status_code == 200
        dl_res = client.get(link_res.json()["download_url"])
        assert dl_res.status_code == 200
        assert len(dl_res.content) == exp_res.json()["file_size_bytes"]

    # 8. Download unchanged original & verify hash
    orig_link = client.get(f"/api/v1/jobs/{job_id}/original/link", headers=headers)
    orig_dl = client.get(orig_link.json()["download_url"])
    assert hashlib.sha256(orig_dl.content).hexdigest() == orig_sha

    # 9. Cross-user unauthorized access check
    other_headers = _get_auth_headers()
    unauth = client.get(f"/api/v1/jobs/{job_id}", headers=other_headers)
    assert unauth.status_code == 404

    # 10. Delete-now cleanup
    del_res = client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
    assert del_res.status_code == 204
