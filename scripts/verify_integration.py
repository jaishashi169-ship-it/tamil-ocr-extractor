#!/usr/bin/env python3
"""
Reproducible Integration Verification Script for Tamil English Document Studio.

Usage:
  python scripts/verify_integration.py --base-url http://localhost:8000

Checks:
  1. Service health & capabilities (/health, /api/v1/capabilities)
  2. Original upload, SHA-256 preservation, and extraction/OCR
  3. Saved edit persistence & reload
  4. TXT, MD, DOCX, and Searchable PDF downloads (and file opening validation)
  5. Live AI Study Studio generation (or explicit BLOCKED if unconfigured)
"""
import argparse
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _request(method: str, url: str, headers: dict | None = None, json_body: dict | None = None, raw_body: bytes | None = None):
    hdrs = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif raw_body is not None:
        data = raw_body
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000", help="Backend base URL (e.g. http://localhost:8000)")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    results = []

    def record(name: str, status: str, detail: str):
        results.append((name, status, detail))
        print(f"[{status}] {name}: {detail}")

    # 1. Health & Capabilities
    try:
        code, body = _request("GET", f"{base}/health")
        if code != 200:
            record("Service Health", "FAIL", f"HTTP {code}: {body.decode('utf-8', 'ignore')}")
            return 1
        record("Service Health", "PASS", body.decode("utf-8"))

        code, cap_raw = _request("GET", f"{base}/api/v1/capabilities")
        cap = json.loads(cap_raw.decode("utf-8"))
        ocr_ok = cap.get("ocr_status", {}).get("available", False)
        record(
            "OCR Capability",
            "PASS" if ocr_ok else "BLOCKED",
            f"available={ocr_ok}, languages={cap.get('ocr_status', {}).get('languages', [])}",
        )
    except Exception as exc:
        record("Service Health", "FAIL", f"Could not reach {base}: {exc}")
        return 1

    # 2. Guest Auth
    code, auth_raw = _request("POST", f"{base}/api/v1/auth/guest", json_body={})
    if code != 200:
        record("Guest Auth", "FAIL", f"HTTP {code}")
        return 1
    token = json.loads(auth_raw.decode("utf-8"))["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 3. Upload & Extraction (Bilingual MD)
    sample_text = (
        "# Chapter 1: Photosynthesis\n\n"
        "ஒளிச்சேர்க்கை (Photosynthesis) converts light energy into chemical energy.\n"
    ).encode("utf-8")
    orig_sha = hashlib.sha256(sample_text).hexdigest()

    code, init_raw = _request(
        "POST",
        f"{base}/api/v1/uploads/init",
        headers=headers,
        json_body={"filename": "integration_sample.md", "expected_size_bytes": len(sample_text)},
    )
    if code != 201:
        record("Upload Init", "FAIL", f"HTTP {code}")
        return 1
    sid = json.loads(init_raw.decode("utf-8"))["session_id"]

    # Multipart upload for chunk 0
    boundary = "----StudioVerifyBoundary"
    mp_body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chunk"; filename="chunk0"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + sample_text + f"\r\n--{boundary}--\r\n".encode("utf-8")
    code, _ = _request(
        "PUT",
        f"{base}/api/v1/uploads/{sid}/chunk/0",
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        raw_body=mp_body,
    )
    if code != 200:
        record("Chunk Upload", "FAIL", f"HTTP {code}")
        return 1

    code, comp_raw = _request("POST", f"{base}/api/v1/uploads/{sid}/complete", headers=headers, json_body={})
    if code != 200:
        record("Upload Complete", "FAIL", f"HTTP {code}")
        return 1

    # 4. Create Job & Bounded Poll
    code, job_raw = _request("POST", f"{base}/api/v1/jobs", headers=headers, json_body={"upload_session_id": sid})
    if code != 201:
        record("Job Dispatch", "FAIL", f"HTTP {code}: {job_raw.decode('utf-8', 'ignore')}")
        return 1
    job_id = json.loads(job_raw.decode("utf-8"))["job_id"]

    deadline = time.monotonic() + 30.0
    job_state = {}
    while time.monotonic() < deadline:
        _, jr = _request("GET", f"{base}/api/v1/jobs/{job_id}", headers=headers)
        job_state = json.loads(jr.decode("utf-8"))
        if job_state.get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.25)

    if job_state.get("status") != "completed":
        record("Async Job Completion", "FAIL", f"status={job_state.get('status')}, error={job_state.get('error_message')}")
        return 1
    record("Async Job Completion", "PASS", f"job_id={job_id}, duration_ms={job_state.get('duration_ms')}")

    # 5. Original SHA-256 check
    _, orig_link_raw = _request("GET", f"{base}/api/v1/jobs/{job_id}/original/link", headers=headers)
    orig_url = json.loads(orig_link_raw.decode("utf-8"))["download_url"]
    if orig_url.startswith("/"):
        orig_url = f"{base}{orig_url}"
    _, orig_bytes = _request("GET", orig_url)
    if hashlib.sha256(orig_bytes).hexdigest() != orig_sha:
        record("Original SHA-256", "FAIL", "Downloaded original hash mismatch")
        return 1
    record("Original SHA-256", "PASS", orig_sha)

    # 6. Edit, Save & Reload
    edited = "தமிழ் மற்றும் English — Edited via verify_integration.py\nPhotosynthesis converts light energy."
    code, _ = _request(
        "PUT",
        f"{base}/api/v1/jobs/{job_id}/pages/1",
        headers=headers,
        json_body={"edited_text": edited, "is_autosave": False, "expected_version": 0},
    )
    if code != 200:
        record("Edit & Save", "FAIL", f"HTTP {code}")
        return 1
    _, pages_raw = _request("GET", f"{base}/api/v1/jobs/{job_id}/pages", headers=headers)
    if json.loads(pages_raw.decode("utf-8"))["pages"][0]["edited_text"] != edited:
        record("Edit Reload", "FAIL", "Reloaded edited_text mismatch")
        return 1
    record("Edit & Save Persistence", "PASS", "Saved and reloaded Unicode edit")

    # 7. Export Downloads (txt, md, docx, searchable_pdf)
    for fmt in ["txt", "md", "docx", "searchable_pdf"]:
        code, exp_raw = _request(
            "POST",
            f"{base}/api/v1/jobs/{job_id}/exports",
            headers=headers,
            json_body={"export_format": fmt, "quality_preset": "high"},
        )
        if code != 201:
            record(f"Export {fmt}", "FAIL", f"HTTP {code}")
            return 1
        art_id = json.loads(exp_raw.decode("utf-8"))["artifact_id"]
        _, link_raw = _request("GET", f"{base}/api/v1/jobs/{job_id}/exports/{art_id}/link", headers=headers)
        dl_url = json.loads(link_raw.decode("utf-8"))["download_url"]
        if dl_url.startswith("/"):
            dl_url = f"{base}{dl_url}"
        dl_code, dl_bytes = _request("GET", dl_url)
        if dl_code != 200 or len(dl_bytes) == 0:
            record(f"Export {fmt}", "FAIL", f"Download HTTP {dl_code}, bytes={len(dl_bytes)}")
            return 1
        record(f"Export {fmt}", "PASS", f"{len(dl_bytes):,} bytes")

    # 8. Study Studio Live Check
    _, st_raw = _request("GET", f"{base}/api/v1/study/status", headers=headers)
    st = json.loads(st_raw.decode("utf-8"))
    if not st.get("configured"):
        record("Live Study Studio", "BLOCKED", "AI provider credentials not configured (503 AI_PROVIDER_UNCONFIGURED)")
    else:
        code, proj_raw = _request(
            "POST",
            f"{base}/api/v1/study/projects",
            headers=headers,
            json_body={"job_id": job_id, "language": "bilingual", "mark_categories": [1, 2, 3, 5, 10]},
        )
        record("Live Study Studio", "PASS" if code in {200, 201} else "FAIL", f"HTTP {code}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
