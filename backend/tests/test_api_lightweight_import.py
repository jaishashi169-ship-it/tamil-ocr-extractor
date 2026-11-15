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

        cap = await client.get("/api/v1/capabilities")
        assert cap.status_code == 200, cap.text
        data = cap.json()
        assert data["ocr_status"]["execution_mode"] == "worker_daemon"
        assert data["ocr_status"]["verified_locally"] is False
        assert data["installed_ocr_languages"] == []
        assert "tam" in data["configured_ocr_languages"]

asyncio.run(_verify())
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(backend_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
