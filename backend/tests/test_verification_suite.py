import hashlib
import io
import json
import time
from docx import Document as DocxDocument
import fitz
from PIL import Image, ImageDraw
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _auth():
    res = client.post("/api/v1/auth/guest")
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _upload_bytes(headers, filename: str, raw: bytes, chunk_size: int = 5_242_880) -> str:
    init = client.post(
        "/api/v1/uploads/init",
        json={"filename": filename, "expected_size_bytes": len(raw), "chunk_size_bytes": max(65_536, chunk_size)},
        headers=headers,
    )
    assert init.status_code == 201, init.text
    sid = init.json()["session_id"]
    total = init.json()["total_chunks"]
    for idx in range(total):
        part = raw[idx * chunk_size : (idx + 1) * chunk_size]
        r = client.put(
            f"/api/v1/uploads/{sid}/chunk/{idx}",
            files={"chunk": (f"c{idx}", part, "application/octet-stream")},
            headers=headers,
        )
        assert r.status_code == 200, r.text
    comp = client.post(f"/api/v1/uploads/{sid}/complete", headers=headers)
    assert comp.status_code in {200, 202}, comp.text
    st_done = client.get(f"/api/v1/uploads/{sid}", headers=headers).json()
    assert st_done["status"] == "completed"
    assert st_done["sha256_hash"] == hashlib.sha256(raw).hexdigest()
    return sid


def _poll_job_until_terminal(headers, job_id: str, timeout_s: float = 15.0, interval_s: float = 0.05) -> dict:
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
        f"Job {job_id} timed out after {timeout_s}s; last_status={last_job.get('status')}, "
        f"stage={last_job.get('stage_label')}, error={last_job.get('error_message')}"
    )


def test_capabilities_reports_ocr_status():
    res = client.get("/api/v1/capabilities")
    assert res.status_code == 200
    data = res.json()
    assert data["max_upload_bytes"] == 500_000_000
    assert "ocr_status" in data
    assert "available" in data["ocr_status"]
    assert "tam_available" in data["ocr_status"]


def test_chunked_upload_idempotency_conflict_and_resume():
    headers = _auth()
    data = b"A" * 150_000
    init = client.post(
        "/api/v1/uploads/init",
        json={"filename": "resume.txt", "expected_size_bytes": len(data), "chunk_size_bytes": 65_536},
        headers=headers,
    )
    assert init.status_code == 201
    sid = init.json()["session_id"]
    assert init.json()["total_chunks"] == 3

    c0 = data[:65_536]
    # Upload chunk 0
    r0 = client.put(f"/api/v1/uploads/{sid}/chunk/0", files={"chunk": ("c0", c0, "application/octet-stream")}, headers=headers)
    assert r0.status_code == 200
    # Idempotent identical retry
    r0_dup = client.put(f"/api/v1/uploads/{sid}/chunk/0", files={"chunk": ("c0", c0, "application/octet-stream")}, headers=headers)
    assert r0_dup.status_code == 200
    # Conflicting chunk 0 must return 409
    r0_bad = client.put(f"/api/v1/uploads/{sid}/chunk/0", files={"chunk": ("c0", b"B" * 65_536, "application/octet-stream")}, headers=headers)
    assert r0_bad.status_code == 409

    # Pause & check status for resume
    client.post(f"/api/v1/uploads/{sid}/pause", headers=headers)
    st = client.get(f"/api/v1/uploads/{sid}", headers=headers)
    assert st.status_code == 200
    assert st.json()["status"] == "paused"
    assert st.json()["received_chunks"] == [0]

    # Resume remaining chunks
    for idx in [1, 2]:
        part = data[idx * 65_536 : (idx + 1) * 65_536]
        r = client.put(f"/api/v1/uploads/{sid}/chunk/{idx}", files={"chunk": (f"c{idx}", part, "application/octet-stream")}, headers=headers)
        assert r.status_code == 200

    comp = client.post(f"/api/v1/uploads/{sid}/complete", headers=headers)
    assert comp.status_code in {200, 202}
    st_done = client.get(f"/api/v1/uploads/{sid}", headers=headers).json()
    assert st_done["status"] == "completed"
    assert st_done["sha256_hash"] == hashlib.sha256(data).hexdigest()


def test_real_english_pdf_tamil_docx_corrupt_and_encrypted_pdf():
    headers = _auth()

    # 1. Real English text PDF via PyMuPDF
    pdf_doc = fitz.open()
    page = pdf_doc.new_page()
    page.insert_text((72, 72), "Tamil English Document Studio Real PDF Verification", fontsize=14)
    page.insert_text((72, 110), "Photosynthesis converts light energy into chemical energy.", fontsize=11)
    pdf_bytes = pdf_doc.tobytes()
    pdf_doc.close()

    sid_pdf = _upload_bytes(headers, "english_real.pdf", pdf_bytes)
    created_pdf = client.post("/api/v1/jobs", json={"upload_session_id": sid_pdf}, headers=headers).json()
    job_pdf = _poll_job_until_terminal(headers, created_pdf["job_id"])
    assert job_pdf["status"] == "completed"
    pages_pdf = client.get(f"/api/v1/jobs/{job_pdf['job_id']}/pages", headers=headers).json()
    assert "Photosynthesis" in pages_pdf["pages"][0]["raw_text"]

    # 2. Real Tamil + English DOCX via python-docx
    docx_obj = DocxDocument()
    docx_obj.add_heading("தமிழ் ஆங்கில ஆவணம்", level=1)
    docx_obj.add_paragraph("தமிழ் என்பது செம்மொழி. Tamil is a classical language.")
    buf = io.BytesIO()
    docx_obj.save(buf)
    sid_docx = _upload_bytes(headers, "tamil_real.docx", buf.getvalue())
    created_docx = client.post("/api/v1/jobs", json={"upload_session_id": sid_docx}, headers=headers).json()
    job_docx = _poll_job_until_terminal(headers, created_docx["job_id"])
    assert job_docx["status"] == "completed"

    # Export DOCX and PDF and parse outputs to verify Tamil Unicode preservation
    exp_docx = client.post(
        f"/api/v1/jobs/{job_docx['job_id']}/exports",
        json={"export_format": "docx", "quality_preset": "high"},
        headers=headers,
    )
    assert exp_docx.status_code in {201, 202}
    link_docx = client.get(f"/api/v1/jobs/{job_docx['job_id']}/exports/{exp_docx.json()['artifact_id']}/link", headers=headers)
    dl_docx = client.get(link_docx.json()["download_url"])
    parsed_docx = DocxDocument(io.BytesIO(dl_docx.content))
    all_docx_text = "\n".join(p.text for p in parsed_docx.paragraphs)
    assert "தமிழ்" in all_docx_text

    # 3. Corrupt PDF detection
    sid_corrupt = _upload_bytes(headers, "corrupt.pdf", b"%PDF-1.4\nINVALID_CORRUPT_CONTENT")
    created_corrupt = client.post("/api/v1/jobs", json={"upload_session_id": sid_corrupt}, headers=headers).json()
    job_corrupt = _poll_job_until_terminal(headers, created_corrupt["job_id"])
    assert job_corrupt["status"] == "failed"

    # 4. Encrypted PDF detection & password unlock retry
    enc_doc = fitz.open()
    ep = enc_doc.new_page()
    ep.insert_text((72, 72), "Secret encrypted PDF content for verification.", fontsize=12)
    enc_bytes = enc_doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner123", user_pw="secret123")
    enc_doc.close()

    sid_enc = _upload_bytes(headers, "encrypted.pdf", enc_bytes)
    created_enc = client.post("/api/v1/jobs", json={"upload_session_id": sid_enc}, headers=headers).json()
    job_enc = _poll_job_until_terminal(headers, created_enc["job_id"])
    assert job_enc["status"] == "failed"
    assert job_enc["needs_password"] is True

    client.post(f"/api/v1/jobs/{job_enc['job_id']}/retry", json={"password": "secret123"}, headers=headers)
    retried = _poll_job_until_terminal(headers, job_enc["job_id"])
    assert retried["status"] == "completed"


def test_optimistic_concurrency_and_grammar_review():
    headers = _auth()
    raw = "teh equasion is  \"teh\" and தமிழ்  மொழி.".encode("utf-8")
    sid = _upload_bytes(headers, "grammar.txt", raw)
    job = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers).json()
    jid = job["job_id"]

    # First edit expects version 0 -> succeeds and becomes version 1
    r1 = client.put(
        f"/api/v1/jobs/{jid}/pages/1",
        json={"edited_text": "Updated v1", "is_autosave": True, "expected_version": 0},
        headers=headers,
    )
    assert r1.status_code == 200
    assert r1.json()["draft_version"] == 1

    # Stale edit expecting version 0 -> must fail with 409 Conflict
    r_stale = client.put(
        f"/api/v1/jobs/{jid}/pages/1",
        json={"edited_text": "Stale overwrite", "is_autosave": True, "expected_version": 0},
        headers=headers,
    )
    assert r_stale.status_code == 409

    # Grammar review does not overwrite saved text
    gr = client.post(f"/api/v1/jobs/{jid}/pages/1/grammar-review", headers=headers)
    assert gr.status_code == 200
    pages = client.get(f"/api/v1/jobs/{jid}/pages", headers=headers).json()
    assert pages["pages"][0]["edited_text"] == "Updated v1"


def test_ocr_unavailable_fails_cleanly_when_tesseract_missing():
    headers = _auth()
    cap = client.get("/api/v1/capabilities").json()
    img = Image.new("RGB", (400, 120), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 40), "OCR Test Image", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    sid = _upload_bytes(headers, "scan.png", buf.getvalue())
    created = client.post("/api/v1/jobs", json={"upload_session_id": sid}, headers=headers).json()
    job = _poll_job_until_terminal(headers, created["job_id"])

    if not cap["ocr_status"]["available"]:
        assert job["status"] == "failed"
        assert "unavailable" in (job["stage_label"] or "").lower() or "not found" in (job["error_message"] or "").lower()
    else:
        assert job["status"] == "completed"
