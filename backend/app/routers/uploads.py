import math
from typing import List
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import Job, UploadSession, User
from app.security import detect_format_by_signature, sanitize_filename
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
    format_type: str | None = None
    sha256_hash: str | None = None


@router.post("/init", response_model=UploadSessionResponse, status_code=status.HTTP_201_CREATED)
def init_upload(
    payload: InitUploadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.expected_size_bytes > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size ({payload.expected_size_bytes:,} bytes) exceeds the maximum allowed limit of {settings.MAX_UPLOAD_BYTES:,} bytes (500 MB).",
        )

    # Check user storage quota
    used_bytes = (
        db.query(func.coalesce(func.sum(Job.file_size_bytes), 0))
        .filter(Job.user_id == current_user.id)
        .scalar()
        or 0
    )
    if used_bytes + payload.expected_size_bytes > current_user.storage_quota_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="User storage quota exceeded. Please delete older jobs before uploading.",
        )

    safe_name = sanitize_filename(payload.filename)
    total_chunks = max(1, math.ceil(payload.expected_size_bytes / payload.chunk_size_bytes))

    session = UploadSession(
        user_id=current_user.id,
        filename=safe_name,
        expected_size_bytes=payload.expected_size_bytes,
        chunk_size_bytes=payload.chunk_size_bytes,
        total_chunks=total_chunks,
        received_chunks=[],
        status="uploading",
    )
    db.add(session)
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
    if session.status in {"completed", "aborted"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Upload session is already {session.status}")
    if chunk_index < 0 or chunk_index >= session.total_chunks:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chunk index")

    data = await chunk.read()
    if len(data) > session.chunk_size_bytes + 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Chunk exceeds declared chunk size")

    current_bytes = storage.get_session_chunks_total_bytes(session.id)
    if chunk_index not in (session.received_chunks or []) and current_bytes + len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Streamed bytes ({current_bytes + len(data):,}) exceed maximum allowed 500 MB ({settings.MAX_UPLOAD_BYTES:,} bytes).",
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
    return UploadSessionResponse(
        session_id=session.id,
        filename=session.filename,
        expected_size_bytes=session.expected_size_bytes,
        received_bytes=(
            session.expected_size_bytes
            if session.status == "completed"
            else storage.get_session_chunks_total_bytes(session.id)
        ),
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks or [],
        status=session.status,
        format_type=session.format_type,
        sha256_hash=session.sha256_hash,
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
    )


@router.post("/{session_id}/complete", response_model=UploadSessionResponse)
def complete_upload(
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

    received = set(session.received_chunks or [])
    missing = [i for i in range(session.total_chunks) if i not in received]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload incomplete. Missing {len(missing)} chunk(s), starting at {missing[0]}.",
        )

    dest_key = f"uploads/{current_user.id}/{session.id}/{session.filename}"
    actual_bytes, sha256_hex = storage.assemble_chunks(session.id, session.total_chunks, dest_key)

    if actual_bytes > settings.MAX_UPLOAD_BYTES:
        storage.delete_key(dest_key)
        session.status = "aborted"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Assembled file ({actual_bytes:,} bytes) exceeds 500 MB limit ({settings.MAX_UPLOAD_BYTES:,} bytes).",
        )

    # Validate magic-byte content signature as well as extension
    assembled_path = storage.get_local_file(dest_key)
    with open(assembled_path, "rb") as f:
        header = f.read(4096)
    fmt, err = detect_format_by_signature(header, session.filename)
    if err or not fmt:
        storage.delete_key(dest_key)
        session.status = "aborted"
        db.commit()
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=err or "Unsupported file format")

    session.storage_key = dest_key
    session.sha256_hash = sha256_hex
    session.format_type = fmt
    session.expected_size_bytes = actual_bytes
    session.status = "completed"
    db.commit()
    db.refresh(session)

    return UploadSessionResponse(
        session_id=session.id,
        filename=session.filename,
        expected_size_bytes=session.expected_size_bytes,
        received_bytes=(
            session.expected_size_bytes
            if session.status == "completed"
            else storage.get_session_chunks_total_bytes(session.id)
        ),
        chunk_size_bytes=session.chunk_size_bytes,
        total_chunks=session.total_chunks,
        received_chunks=session.received_chunks or [],
        status=session.status,
        format_type=session.format_type,
        sha256_hash=session.sha256_hash,
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
    storage.abort_chunks(session.id)
    if session.storage_key:
        storage.delete_key(session.storage_key)
    session.status = "aborted"
    db.commit()
