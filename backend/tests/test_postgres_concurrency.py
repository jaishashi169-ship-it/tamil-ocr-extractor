"""
Real PostgreSQL concurrency tests.

SQLite and mocks cannot prove `SELECT ... FOR UPDATE` / advisory-lock behavior, so these tests
run ONLY when TEST_POSTGRES_URL points at a real PostgreSQL server, e.g.
    TEST_POSTGRES_URL=postgresql://user:pass@127.0.0.1:5432/studio_test
They apply Alembic migrations through head (0004_storage_reservations) on that database first.
"""
import hashlib
import os
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

PG_URL = os.environ.get("TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_POSTGRES_URL not set; real PostgreSQL required")

MB = 1_000_000


@pytest.fixture(scope="module")
def pg_sessionmaker():
    from alembic import command
    from alembic.config import Config

    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", PG_URL)
    from app.config import get_settings

    app_cfg = get_settings()
    prev_sync = app_cfg.SYNC_DATABASE_URL
    app_cfg.SYNC_DATABASE_URL = PG_URL  # alembic/env.py reads the cached settings object
    try:
        command.upgrade(cfg, "head")
    finally:
        app_cfg.SYNC_DATABASE_URL = prev_sync

    engine = create_engine(PG_URL, pool_size=30, max_overflow=10, future=True)
    with engine.connect() as conn:
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert head == "0004_storage_reservations", head
    yield sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    engine.dispose()


def _reset(SessionPG):
    from app.models import ExportArtifact, Job, StorageReservation, UploadSession, User

    with SessionPG() as db:
        db.query(StorageReservation).delete()
        db.query(ExportArtifact).delete()
        db.query(Job).delete()
        db.query(UploadSession).delete()
        db.query(User).filter(User.email.like("pgconc-%")).delete(synchronize_session=False)
        db.commit()


def test_concurrent_reservations_never_exceed_total_budget(pg_sessionmaker, monkeypatch):
    from app.config import get_settings
    from app.models import StorageReservation, User
    from app.services.retention import StorageQuotaExceededError, reserve_storage_capacity

    cfg = get_settings()
    monkeypatch.setattr(cfg, "TOTAL_STORAGE_BUDGET_BYTES", 850 * MB)
    monkeypatch.setattr(cfg, "USER_STORAGE_QUOTA_BYTES", 750 * MB)
    _reset(pg_sessionmaker)

    workers = 20
    user_ids = []
    with pg_sessionmaker() as db:
        for i in range(workers):
            u = User(
                email=f"pgconc-{i}-{uuid.uuid4().hex[:6]}@studio.local",
                hashed_password="x",
                role="user",
                storage_quota_bytes=750 * MB,
                retention_days=7,
            )
            db.add(u)
            db.flush()
            user_ids.append(u.id)
        db.commit()

    barrier = threading.Barrier(workers)
    results: list[str] = []
    lock = threading.Lock()

    def _worker(idx: int) -> None:
        db = pg_sessionmaker()
        try:
            user = db.query(User).filter(User.id == user_ids[idx]).one()
            barrier.wait()
            reserve_storage_capacity(
                db,
                user=user,
                resource_type="upload",
                resource_id=f"pgconc-sess-{idx}",
                reserved_bytes=100 * MB,
                storage_key=f"uploads/{user.id}/pgconc-sess-{idx}/f.pdf",
            )
            outcome = "ok"
        except StorageQuotaExceededError:
            db.rollback()
            outcome = "rejected"
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            db.rollback()
            outcome = f"error:{type(exc).__name__}:{exc}"
        finally:
            db.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    errors = [r for r in results if r.startswith("error")]
    assert not errors, errors
    with pg_sessionmaker() as db:
        total_reserved = sum(
            r.reserved_bytes
            for r in db.query(StorageReservation).filter(StorageReservation.status == "reserved").all()
        )
    ok = results.count("ok")
    print(f"PG concurrency: ok={ok} rejected={results.count('rejected')} total_reserved={total_reserved}")
    # 8 x 100 MB = 800 MB fits in 850 MB; a 9th would be 900 MB.
    assert ok == 8, results
    assert total_reserved == 800 * MB
    assert total_reserved <= 850 * MB
    _reset(pg_sessionmaker)


def test_concurrent_finalize_accepts_original_exactly_once(pg_sessionmaker, monkeypatch):
    import app.tasks as tasks_mod
    from app.models import UploadSession, User
    from app.storage import storage

    _reset(pg_sessionmaker)
    monkeypatch.setattr(tasks_mod, "SessionLocal", pg_sessionmaker)

    payload = b"%PDF-1.4\n% pg concurrency finalize\n%%EOF\n"
    with pg_sessionmaker() as db:
        user = User(
            email=f"pgconc-final-{uuid.uuid4().hex[:6]}@studio.local",
            hashed_password="x",
            role="user",
            storage_quota_bytes=750 * MB,
            retention_days=7,
        )
        db.add(user)
        db.flush()
        sess = UploadSession(
            user_id=user.id,
            filename="final.pdf",
            expected_size_bytes=len(payload),
            chunk_size_bytes=65_536,
            total_chunks=1,
            received_chunks=[0],
            s3_parts_json=[],
            status="processing",
        )
        db.add(sess)
        db.commit()
        sid, uid = sess.id, user.id

    storage.write_chunk(sid, 0, payload)
    dest_key = f"uploads/{uid}/{sid}/final.pdf"

    barrier = threading.Barrier(5)
    errors: list[str] = []

    def _finalize() -> None:
        try:
            barrier.wait()
            tasks_mod.finalize_upload_sync(sid)
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=_finalize) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    with pg_sessionmaker() as db:
        final = db.query(UploadSession).filter(UploadSession.id == sid).one()
        assert final.status == "completed"
        assert final.sha256_hash == hashlib.sha256(payload).hexdigest()
    assert storage.read_bytes(dest_key) == payload
    storage.delete_key(dest_key)
    _reset(pg_sessionmaker)


def test_retention_cleanup_lock_prevents_overlap(pg_sessionmaker):
    from app.services.retention import _try_retention_distributed_lock

    db1 = pg_sessionmaker()
    db2 = pg_sessionmaker()
    try:
        with _try_retention_distributed_lock(db1) as first:
            assert first is True
            # Second trigger in another thread/session must be skipped, not run concurrently.
            outcome: list[bool] = []

            def _second() -> None:
                with _try_retention_distributed_lock(db2) as second:
                    outcome.append(second)

            t = threading.Thread(target=_second)
            t.start()
            t.join(timeout=30)
            assert outcome == [False]
        db1.commit()
        # Advisory xact lock released at commit; a later trigger may proceed.
        with _try_retention_distributed_lock(db2) as third:
            assert third is True
        db2.commit()
    finally:
        db1.close()
        db2.close()


def test_cleanup_outlasting_redis_ttl_prevents_overlap_via_pg_lock(pg_sessionmaker, monkeypatch):
    """
    Test that even if a cleanup run outlasts a Redis lock TTL (or if Redis key expires mid-cleanup),
    the PostgreSQL transaction-scoped advisory lock remains held across all helper operations
    and external storage calls, strictly preventing a second concurrent cleanup run from
    performing any overlapping destructive work.
    """
    import time
    from datetime import datetime, timedelta, timezone
    from app.models import ExportArtifact, Job, User
    from app.services.retention import cleanup_expired_resources, _try_retention_distributed_lock
    import app.storage as storage_mod

    _reset(pg_sessionmaker)

    # Seed an expired export artifact with a storage key
    now_utc = datetime.now(timezone.utc)
    expired_time = now_utc - timedelta(days=2)
    with pg_sessionmaker() as db:
        user = User(
            email=f"pgconc-ret-{uuid.uuid4().hex[:6]}@studio.local",
            hashed_password="x",
            role="user",
            storage_quota_bytes=750 * MB,
            retention_days=7,
        )
        db.add(user)
        db.flush()
        job = Job(
            user_id=user.id,
            original_filename="doc.pdf",
            format_type="application/pdf",
            file_size_bytes=5000,
            original_storage_key="uploads/test.pdf",
            status="completed",
        )
        db.add(job)
        db.flush()
        art = ExportArtifact(
            user_id=user.id,
            job_id=job.id,
            export_format="pdf",
            status="completed",
            storage_key="exports/test-expired.pdf",
            file_size_bytes=1000,
            expires_at=expired_time,
        )
        db.add(art)
        db.commit()
        art_id = art.id

    thread1_in_storage = threading.Event()
    thread1_allow_finish = threading.Event()
    thread2_finished = threading.Event()

    original_delete_key = storage_mod.storage.delete_key
    storage_deletions: list[str] = []

    def mocked_delete_key(key: str) -> None:
        storage_deletions.append(key)
        # Signal that Thread 1 is inside external storage deletion while holding the PG xact lock
        thread1_in_storage.set()
        # Wait until Thread 2 has attempted its overlapping run
        thread1_allow_finish.wait(timeout=10)
        original_delete_key(key)

    monkeypatch.setattr(storage_mod.storage, "delete_key", mocked_delete_key)

    thread1_result: list[dict] = []
    thread2_result: list[dict] = []
    commits_during_storage: list[str] = []

    def _run_thread1():
        db1 = pg_sessionmaker()
        try:
            # Run cleanup with a simulated 1-second TTL to test operations lasting longer than lock TTL
            res = cleanup_expired_resources(db1, stale_upload_hours=1)
            thread1_result.append(res)
        finally:
            db1.close()

    def _run_thread2():
        # Wait for Thread 1 to enter external storage deletion
        assert thread1_in_storage.wait(timeout=10)
        # Sleep to simulate Redis lock expiry (e.g. outlasting Redis TTL)
        time.sleep(0.2)
        db2 = pg_sessionmaker()
        try:
            # Thread 2 attempts to run cleanup concurrently
            res = cleanup_expired_resources(db2, stale_upload_hours=1)
            thread2_result.append(res)
        finally:
            db2.close()
            thread2_finished.set()
            thread1_allow_finish.set()

    t1 = threading.Thread(target=_run_thread1)
    t2 = threading.Thread(target=_run_thread2)

    t1.start()
    t2.start()

    t2.join(timeout=15)
    t1.join(timeout=15)

    # Thread 2 MUST have been skipped because PostgreSQL transaction advisory lock was held by Thread 1
    assert len(thread2_result) == 1, thread2_result
    assert thread2_result[0]["skipped_locked"] is True
    assert thread2_result[0]["deleted_exports"] == 0
    assert thread2_result[0]["deleted_jobs"] == 0

    # Thread 1 MUST have successfully cleaned up the artifact and committed
    assert len(thread1_result) == 1, thread1_result
    assert thread1_result[0]["skipped_locked"] is False
    assert thread1_result[0]["deleted_exports"] == 1

    # External storage was called exactly once (by Thread 1, never by Thread 2)
    assert storage_deletions == ["exports/test-expired.pdf"]

    # Verify the database state
    with pg_sessionmaker() as db:
        remaining = db.query(ExportArtifact).filter(ExportArtifact.id == art_id).first()
        assert remaining is None

    # Now verify a subsequent run (Thread 3) can acquire the lock cleanly since Thread 1 committed
    with pg_sessionmaker() as db3:
        t3_res = cleanup_expired_resources(db3, stale_upload_hours=1)
        assert t3_res["skipped_locked"] is False
        assert t3_res["deleted_exports"] == 0

    _reset(pg_sessionmaker)

