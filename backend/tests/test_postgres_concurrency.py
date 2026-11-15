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
