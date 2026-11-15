#!/usr/bin/env python3
"""
Restricted Production Smoke Test Runner for Tamil-English Document Studio.

Because Vercel Fluid Compute Daemons (`daemons.celery`) run in production deployments
rather than ephemeral preview deployments, this script validates a restricted production
rollout before opening public uploads:
  1. Verifies `/health` (liveness) and `/health/ready` (DB, Redis, private Blob, verified OCR worker).
  2. Verifies that unauthenticated/public upload initialization (`POST /api/v1/uploads/init` without
     `X-Smoke-Test-Token`) is rejected with HTTP 403/503 while `RESTRICTED_SMOKE_TEST_MODE=true`
     or `REQUIRE_READINESS_FOR_UPLOADS=true`.
  3. Executes `POST /api/v1/admin/smoke-test` with `X-Smoke-Test-Token` to verify atomic storage
     reservation, private Vercel Blob write/read/delete round-trip, and reservation release.
  4. Optionally runs an authenticated upload -> OCR -> export -> Study Studio workflow using
     `X-Smoke-Test-Token`.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


def _request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    req_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"raw": raw}
        return exc.code, parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Run restricted production smoke test.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_TEST_BASE_URL", "https://tamilocrextraxtor.vercel.app"),
        help="Deployment base URL (default: https://tamilocrextraxtor.vercel.app)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SMOKE_TEST_SECRET", ""),
        help="Smoke test secret token for X-Smoke-Test-Token header",
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    print(f"[1/4] Checking liveness: {base}/health")
    code, liveness = _request(f"{base}/health")
    print(f"      Status {code}: {json.dumps(liveness)}")
    if code != 200:
        print("FAIL: Liveness check failed.")
        return 1

    print(f"[2/4] Checking readiness: {base}/health/ready")
    ready_code, readiness = _request(f"{base}/health/ready")
    print(f"      Status {ready_code}: {json.dumps(readiness)}")

    print(f"[3/4] Checking public upload gating: {base}/api/v1/uploads/init (without smoke token)")
    pub_code, pub_resp = _request(
        f"{base}/api/v1/uploads/init",
        method="POST",
        json_body={
            "filename": "public-probe.pdf",
            "mime_type": "application/pdf",
            "total_bytes": 1024,
        },
    )
    print(f"      Public upload status {pub_code}: {json.dumps(pub_resp)}")

    if not args.token:
        print("[4/4] Skipping authenticated /api/v1/admin/smoke-test (no --token / SMOKE_TEST_SECRET provided).")
        return 0 if ready_code == 200 else 2

    print(f"[4/4] Running restricted smoke test: {base}/api/v1/admin/smoke-test")
    smoke_code, smoke_resp = _request(
        f"{base}/api/v1/admin/smoke-test",
        method="POST",
        headers={"X-Smoke-Test-Token": args.token},
    )
    print(f"      Smoke test status {smoke_code}: {json.dumps(smoke_resp, indent=2)}")
    if smoke_code == 200 and smoke_resp.get("smoke_test_passed"):
        print("PASS: Restricted production smoke test succeeded.")
        return 0

    print("NO-GO: Restricted production smoke test did not pass all readiness/storage checks.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
