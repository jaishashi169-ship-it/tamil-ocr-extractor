import hmac
import math
from typing import List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import (
    create_upload_authorization_token,
    get_current_user,
    verify_upload_authorization_token,
)
from app.config import get_settings
from app.database import get_db
from app.dispatch import dispatch_celery_or_fallback, is_broker_reachable
from app.models import StorageReservation, UploadSession, User
from app.security import sanitize_filename
from app.services.retention import (
    StorageQuotaExceededError,
    release_reservations_for_resource,
    reserve_storage_capacity,
)
from app.storage import storage

router = APIRouter(prefix="/uploads", tags=["Uploads"])
settings = get_settings()


class InitUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    expected_size_bytes: int = Field(gt=0)
    chunk_size_bytes: int = Field(default=settings.UPLOAD_CHUNK_BYTES, ge=65_536, le=20_971_520)


class UploadSessionResponse(BaseModel):
    session_id: str
    filename: str
    expected_size_bytes: int
    received_bytes: int = 0
    chunk_size_bytes: int
    total_chunks: int
    received_chunks: List[int]
    status: str
    upload_mode: str = "proxy"
    storage_key: Optional[str] = None
    handle_upload_url: Optional[str] = None
    upload_authorization_token: Optional[str] = None
    format_type: Optional[str] = None
    sha256_hash: Optional[str] = None


class RegisterS3PartRequest(BaseModel):
    etag: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(ge=0, default=0)


class AuthorizeBlobUploadRequest(BaseModel):
    pathname: str = Field(min_length=1, max_length=512)
    upload_authorization_token: str = Field(min_length=1)


def _enforce_upload_readiness_and_smoke_gate(
    current_user: User,
    x_smoke_test_token: Optional[str],
) -> None:
    """
    Prevent public uploads when:
    1. RESTRICTED_SMOKE_TEST_MODE is enabled (unless admin or valid X-Smoke-Test-Token is supplied), OR
    2. REQUIRE_READINESS_FOR_UPLOADS is active and backing services/worker are not ready.
    """
    cfg = get_settings()
    valid_smoke_token = bool(
        cfg.SMOKE_TEST_SECRET
        and x_smoke_test_token
        and hmac.compare_digest(x_smoke_test_token.strip(), cfg.SMOKE_TEST_SECRET.strip())
    )
    is_privileged = current_user.role == "admin" or valid_smoke_token

    if cfg.RESTRICTED_SMOKE_TEST_MODE and not is_privileged:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "RESTRICTED_SMOKE_TEST_MODE",
                "message": "Public uploads are disabled while restricted production smoke testing is active.",
            },
        )

    if cfg.REQUIRE_READINESS_FOR_UPLOADS and not valid_smoke_token:
        from app.routers.admin import evaluate_readiness

        readiness = evaluate_readiness()
        if not readiness.get("ready"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "SYSTEM_NOT_READY_FOR_UPLOADS",
                    "message": "Public uploads are blocked until database, broker, storage, and worker readiness checks pass.",
                    "readiness": readiness,
                },
            )


@router.post("/init", response_model=UploadSessionResponse, status_code=status.HTTP_201_CREATED)
def init_upload(
    payload: InitUploadRequest,
    x_smoke_test_token: Optional[str] = Header(default=None, alias="X-Smoke-Test-Token"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _enforce_upload_readiness_and_smoke_gate(current_user, x_smoke_test_token)

    if payload.expected_size_bytes > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size ({payload.expected_size_bytes:,} bytes) exceeds the maximum allowed limit of {settings.MAX_UPLOAD_BYTES:,} bytes (400 MB).",
        )

    safe_name = sanitize_filename(payload.filename)
    is_direct = storage.is_direct_multipart()
    effective_chunk_size = (
        max(settings.S3_MULTIPART_PART_BYTES, payload.chunk_size_bytes)
        if is_direct
        else payload.chunk_size_bytes
    )
    total_chunks = max(1, math.ceil(payload.expected_size_bytes / effective_chunk_size))

    session = UploadSession(
        user_id=current_user.id,
        filename=safe_name,
        expected_size_bytes=payload.expected_size_bytes,
        chunk_size_bytes=effective_chunk_size,
        total_chunks=total_chunks,
        received_chunks=[],
        s3_parts_json=[],
        status="uploading",
    )
    db.add(session)
    db.flush()

    dest_key = f"uploads/{current_user.id}/{session.id}/{safe_name}"
    session.storage_key = dest_key

    # Atomically reserve storage capacity under row lock covering concurrent uploads and outputs
    try:
        reserve_storage_capacity(
            db,
            user=current_user,
            resource_type="upload",
            resource_id=session.id,
            reserved_bytes=payload.expected_size_bytes,
            storage_key=dest_key,
            ttl_seconds=6 * 3600,
            commit=False,
        )
    except StorageQuotaExceededError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc

    upload_mode = "proxy"
    handle_upload_url: Optional[str] = None
    upload_auth_token: Optional[str] = None
    if is_direct:
        session.s3_upload_id = storage.create_multipart_upload(dest_key)
        upload_mode = storage.direct_upload_mode()
        if upload_mode == "vercel_blob":
            handle_upload_url = "/api/blob/upload"
            upload_auth_token = create_upload_authorization_token(
                user_id=current_user.id,
                session_id=session.id,
                storage_key=dest_key,
                expected_size_bytes=payload.expected_size_bytes,
            )

    db.commit()
    db.refresh(session)

    return UploadSessionResponse(
        session_id=session.id,
        filename=session.filename,
        expected_size_bytes=session.expected_size_bytes,
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks,
        status=session.status,
        upload_mode=upload_mode,
        storage_key=session.storage_key,
        handle_upload_url=handle_upload_url,
        upload_authorization_token=upload_auth_token,
    )


@router.post("/{session_id}/authorize-blob")
def authorize_blob_upload(
    session_id: str,
    payload: AuthorizeBlobUploadRequest,
    db: Session = Depends(get_db),
):
    """
    Verify an upload session and its atomic StorageReservation for the Next.js
    `@vercel/blob/client` `handleUpload` server route (`onBeforeGenerateToken`).
    Enforces single-use token exchange and `allow_overwrite=False` so a finalized
    or uploaded original can never be overwritten via token replay.
    """
    claims = verify_upload_authorization_token(payload.upload_authorization_token)
    if claims.get("sid") != session_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Session ID mismatch")

    q = db.query(UploadSession).filter(
        UploadSession.id == session_id,
        UploadSession.user_id == claims.get("sub"),
    )
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        q = q.with_for_update()
    session = q.first()

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active upload session not found")
    if session.status in {"processing", "completed", "aborted", "failed"} or session.sha256_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload session is already finalized or inactive; token replay is rejected.",
        )

    clean_pathname = payload.pathname.lstrip("/")
    expected_key = (session.storage_key or "").lstrip("/")
    if not expected_key or clean_pathname != expected_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload pathname does not match reserved storage key")

    active_res = (
        db.query(StorageReservation)
        .filter(
            StorageReservation.resource_type == "upload",
            StorageReservation.resource_id == session.id,
            StorageReservation.status == "reserved",
        )
        .first()
    )
    if not active_res:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Storage reservation is no longer active for this upload session",
        )

    # Track token issuance for safe recovery without stranding unfinished uploads,
    # while allow_overwrite=False and the finalization guard strictly prevent mutation of finalized originals.
    existing_markers = list(session.s3_parts_json or [])
    token_issuance_count = sum(1 for p in existing_markers if isinstance(p, dict) and p.get("blob_token_issued"))
    existing_markers.append({
        "PartNumber": token_issuance_count + 1,
        "ETag": "vercel-blob-token-issued",
        "blob_token_issued": True,
    })
    session.s3_parts_json = existing_markers
    session.received_chunks = sorted(set(list(session.received_chunks or []) + [0]))
    db.commit()

    return {
        "upload_id": session.id,
        "user_id": session.user_id,
        "storage_key": expected_key,
        "expected_size_bytes": int(session.expected_size_bytes),
        "allow_overwrite": False,
        "reauthorized": token_issuance_count > 0,
    }


@router.get("/{session_id}/part-url/{chunk_index}")
def get_direct_part_upload_url(
    session_id: str,
    chunk_index: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id, UploadSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if session.status in {"processing", "completed", "aborted", "failed"} or session.sha256_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Upload session is already {session.status} and cannot be modified",
        )
    if chunk_index < 0 or chunk_index >= session.total_chunks:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chunk index")
    if not session.s3_upload_id or not session.storage_key or not storage.is_s3():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Direct S3 multipart upload is not enabled for this session",
        )

    part_number = chunk_index + 1
    return storage.get_direct_part_upload_target(
        key=session.storage_key,
        upload_id=session.s3_upload_id,
        part_number=part_number,
        max_size_bytes=min(int(session.expected_size_bytes), int(settings.MAX_UPLOAD_BYTES)),
        expires_in=900,
    )


@router.post("/{session_id}/parts/{chunk_index}", response_model=UploadSessionResponse)
def register_direct_s3_part(
    session_id: str,
    chunk_index: int,
    payload: RegisterS3PartRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id, UploadSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if session.status in {"processing", "completed", "aborted", "failed"} or session.sha256_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Upload session is already {session.status} and cannot be modified",
        )
    if chunk_index < 0 or chunk_index >= session.total_chunks:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chunk index")

    part_number = chunk_index + 1
    parts = [p for p in (session.s3_parts_json or []) if int(p.get("PartNumber", -1)) != part_number]
    parts.append({"PartNumber": part_number, "ETag": payload.etag})
    session.s3_parts_json = parts
    session.received_chunks = sorted(set(list(session.received_chunks or []) + [chunk_index]))
    session.status = "uploading"
    db.commit()
    db.refresh(session)

    return UploadSessionResponse(
        session_id=session.id,
        filename=session.filename,
        expected_size_bytes=session.expected_size_bytes,
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks,
        status=session.status,
        upload_mode=storage.direct_upload_mode() if session.s3_upload_id else "proxy",
        storage_key=session.storage_key,
    )


@router.put("/{session_id}/chunk/{chunk_index}", response_model=UploadSessionResponse)
async def upload_chunk(
    session_id: str,
    chunk_index: int,
    chunk: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id, UploadSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if session.status in {"processing", "completed", "aborted", "failed"} or session.sha256_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Upload session is already {session.status} and cannot be modified",
        )
    if chunk_index < 0 or chunk_index >= session.total_chunks:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chunk index")

    data = await chunk.read()
    if len(data) > session.chunk_size_bytes + 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Chunk exceeds declared chunk size")

    current_bytes = storage.get_session_chunks_total_bytes(session.id)
    if chunk_index not in (session.received_chunks or []) and current_bytes + len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Streamed bytes ({current_bytes + len(data):,}) exceed maximum allowed 400 MB ({settings.MAX_UPLOAD_BYTES:,} bytes).",
        )

    try:
        storage.write_chunk_idempotent(session.id, chunk_index, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    received = sorted(set(list(session.received_chunks or []) + [chunk_index]))
    session.received_chunks = received
    session.status = "uploading"
    db.commit()
    db.refresh(session)
    total_received_bytes = storage.get_session_chunks_total_bytes(session.id)

    return UploadSessionResponse(
        session_id=session.id,
        filename=session.filename,
        expected_size_bytes=session.expected_size_bytes,
        received_bytes=total_received_bytes,
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks,
        status=session.status,
        storage_key=session.storage_key,
    )


@router.get("/{session_id}", response_model=UploadSessionResponse)
def get_upload_status(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id, UploadSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")

    # In local development without a running Celery worker daemon, auto-finalize on polling
    if (
        session.status == "processing"
        and not session.sha256_hash
        and settings.APP_ENV != "production"
        and settings.ALLOW_BACKGROUND_TASK_FALLBACK
        and not is_broker_reachable(settings.CELERY_BROKER_URL, 0.05, 0.1)
    ):
        try:
            from app.tasks import finalize_upload_sync
            finalize_upload_sync(session.id)
            db.refresh(session)
        except Exception:
            pass

    mode = storage.direct_upload_mode() if session.s3_upload_id else "proxy"
    received_bytes = (
        session.expected_size_bytes
        if session.status == "completed"
        else (
            min(session.expected_size_bytes, len(session.received_chunks or []) * session.chunk_size_bytes)
            if session.s3_upload_id
            else storage.get_session_chunks_total_bytes(session.id)
        )
    )
    return UploadSessionResponse(
        session_id=session.id,
        filename=session.filename,
        expected_size_bytes=session.expected_size_bytes,
        received_bytes=received_bytes,
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks or [],
        status=session.status,
        format_type=session.format_type,
        sha256_hash=session.sha256_hash,
        upload_mode=mode,
        storage_key=session.storage_key,
        handle_upload_url="/api/blob/upload" if mode == "vercel_blob" else None,
        upload_authorization_token=(
            create_upload_authorization_token(
                user_id=current_user.id,
                session_id=session.id,
                storage_key=session.storage_key or "",
                expected_size_bytes=session.expected_size_bytes,
            )
            if (
                mode == "vercel_blob"
                and session.status in {"uploading", "paused"}
                and not session.sha256_hash
                and not any(isinstance(p, dict) and p.get("blob_token_issued") for p in (session.s3_parts_json or []))
            )
            else None
        ),
    )


@router.post("/{session_id}/pause", response_model=UploadSessionResponse)
def pause_upload(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id, UploadSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if session.status in {"processing", "completed", "aborted", "failed"} or session.sha256_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Upload session is already {session.status} and cannot be paused or modified",
        )
    if session.status == "uploading":
        session.status = "paused"
        db.commit()
        db.refresh(session)
    return UploadSessionResponse(
        session_id=session.id,
        filename=session.filename,
        expected_size_bytes=session.expected_size_bytes,
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks or [],
        status=session.status,
        upload_mode=storage.direct_upload_mode() if session.s3_upload_id else "proxy",
        storage_key=session.storage_key,
    )


@router.post("/{session_id}/complete", response_model=UploadSessionResponse, status_code=status.HTTP_202_ACCEPTED)
def complete_upload(
    session_id: str,
    background_tasks: BackgroundTasks,
    x_smoke_test_token: Optional[str] = Header(default=None, alias="X-Smoke-Test-Token"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _enforce_upload_readiness_and_smoke_gate(current_user, x_smoke_test_token)

    q = db.query(UploadSession).filter(
        UploadSession.id == session_id,
        UploadSession.user_id == current_user.id,
    )
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        q = q.with_for_update()
    session = q.first()

    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")

    if session.status in {"processing", "completed"}:
        is_blob = session.s3_upload_id == "vercel-blob-sdk-managed"
        recv = (
            session.expected_size_bytes
            if session.status == "completed"
            else (
                session.expected_size_bytes
                if is_blob
                else (
                    min(session.expected_size_bytes, len(session.received_chunks or []) * session.chunk_size_bytes)
                    if session.s3_upload_id
                    else storage.get_session_chunks_total_bytes(session.id)
                )
            )
        )
        return UploadSessionResponse(
            session_id=session.id,
            filename=session.filename,
            expected_size_bytes=session.expected_size_bytes,
            received_bytes=recv,
            chunk_size_bytes=session.chunk_size_bytes,
            total_chunks=session.total_chunks,
            received_chunks=session.received_chunks or [],
            status=session.status,
            format_type=session.format_type,
            sha256_hash=session.sha256_hash,
            upload_mode=storage.direct_upload_mode() if session.s3_upload_id else "proxy",
            storage_key=session.storage_key,
        )

    if session.status in {"aborted", "failed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Upload session is {session.status} and cannot be finalized.",
        )

    is_blob_sdk_upload = session.s3_upload_id == "vercel-blob-sdk-managed"
    if not is_blob_sdk_upload:
        received = set(session.received_chunks or [])
        missing = [i for i in range(session.total_chunks) if i not in received]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Upload incomplete. Missing {len(missing)} chunk(s), starting at {missing[0]}.",
            )

    session.status = "processing"
    db.commit()

    def _on_failed() -> None:
        session.status = "failed"
        release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
        db.commit()

    dispatch_celery_or_fallback(
        background_tasks=background_tasks,
        task_name="app.tasks.finalize_upload",
        args=[session.id],
        sync_fn_name="finalize_upload_sync",
        on_failed=_on_failed,
        resource_id=session.id,
    )
    db.refresh(session)

    received_bytes = (
        session.expected_size_bytes
        if session.status == "completed"
        else (
            session.expected_size_bytes
            if is_blob_sdk_upload
            else (
                min(session.expected_size_bytes, len(session.received_chunks or []) * session.chunk_size_bytes)
                if session.s3_upload_id
                else storage.get_session_chunks_total_bytes(session.id)
            )
        )
    )
    return UploadSessionResponse(
        session_id=session.id,
        filename=session.filename,
        expected_size_bytes=session.expected_size_bytes,
        received_bytes=received_bytes,
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks or [],
        status=session.status,
        format_type=session.format_type,
        sha256_hash=session.sha256_hash,
        upload_mode=storage.direct_upload_mode() if session.s3_upload_id else "proxy",
        storage_key=session.storage_key,
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def abort_upload(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = (
        db.query(UploadSession)
        .filter(UploadSession.id == session_id, UploadSession.user_id == current_user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if session.status in {"processing", "completed"} or session.sha256_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finalized or processing upload session cannot be aborted or replaced.",
        )
    storage.abort_chunks(session.id)
    if session.s3_upload_id and session.storage_key:
        storage.abort_multipart_upload(session.storage_key, session.s3_upload_id)
    if session.storage_key:
        storage.delete_key(session.storage_key)
    release_reservations_for_resource(db, resource_type="upload", resource_id=session.id, commit=False)
    session.status = "aborted"
    db.commit()
