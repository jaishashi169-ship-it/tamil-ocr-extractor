import contextvars
import hashlib
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

import boto3
from botocore.client import Config
from vercel._internal.core.session import get_active_sync_session
from vercel.blob import sync as vercel_blob_sync
from vercel.blob.errors import BlobNotFoundError

from app.config import get_settings
from app.security import safe_join

settings = get_settings()

STREAM_CHUNK_BYTES = 262_144  # 256 KiB bounded streaming chunk size

_write_hook_var: contextvars.ContextVar[Optional[Callable[[str, int], None]]] = contextvars.ContextVar(
    "storage_write_hook", default=None
)
_delete_hook_var: contextvars.ContextVar[Optional[Callable[[str], None]]] = contextvars.ContextVar(
    "storage_delete_hook", default=None
)


@contextmanager
def bind_storage_hooks(
    on_write: Optional[Callable[[str, int], None]] = None,
    on_delete: Optional[Callable[[str], None]] = None,
) -> Iterator[None]:
    """Bind per-execution callbacks to enforce atomic storage reservations on every object write/delete."""
    t_w = _write_hook_var.set(on_write)
    t_d = _delete_hook_var.set(on_delete)
    try:
        yield
    finally:
        _write_hook_var.reset(t_w)
        _delete_hook_var.reset(t_d)


def parse_store_id_from_rw_token(token: str) -> str:
    """Extract the storeId segment from vercel_blob_rw_<storeId>_<secret>."""
    parts = (token or "").strip().split("_")
    if len(parts) >= 5 and parts[:3] == ["vercel", "blob", "rw"] and parts[3]:
        return parts[3]
    return ""


def parse_byte_range(range_header: Optional[str], total_size: int) -> Optional[tuple[int, int]]:
    """
    Parse an HTTP Range header ('bytes=start-end', 'bytes=start-', or 'bytes=-suffix')
    against total_size. Returns (start, end) inclusive, or None if no Range header was given.
    Raises ValueError if the range is unsatisfiable (416).
    """
    if not range_header:
        return None
    m = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
    if not m or total_size <= 0:
        raise ValueError("Invalid or unsatisfiable Range header")
    start_s, end_s = m.group(1), m.group(2)
    if not start_s and not end_s:
        raise ValueError("Empty byte range")
    if not start_s:
        suffix_len = int(end_s)
        if suffix_len <= 0:
            raise ValueError("Invalid suffix length")
        start = max(0, total_size - suffix_len)
        end = total_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else total_size - 1
    if start < 0 or start >= total_size or end < start:
        raise ValueError("Byte range out of bounds")
    return start, min(end, total_size - 1)


class PrivateStorage:
    """
    Private object storage supporting:
    - Vercel Blob private storage (`STORAGE_BACKEND=vercel_blob`, `access='private'`) via official `vercel.blob.sync` SDK
    - S3-compatible private object storage (`STORAGE_BACKEND=s3`) via `boto3`
    - Local filesystem for non-production development/test (`STORAGE_BACKEND=local`)
    """

    def __init__(self) -> None:
        self.backend = settings.STORAGE_BACKEND.lower()
        self.local_root = Path(settings.LOCAL_STORAGE_ROOT).resolve()
        # /tmp is permitted strictly for disposable processing scratch files, never durable storage
        self.scratch_root = (Path(tempfile.gettempdir()) / "doc_studio_disposable").resolve()
        self.local_writable = False
        if self.backend == "local" and settings.APP_ENV != "production" and not os.environ.get("VERCEL"):
            try:
                self.local_root.mkdir(parents=True, exist_ok=True)
                self.local_writable = os.access(self.local_root, os.W_OK)
            except OSError:
                self.local_writable = False
        self._s3 = None
        self._s3_public = None
        self._last_blob_env: tuple[str, str] = ("", "")
        if self.backend == "s3":
            self._init_s3()

    def _init_s3(self) -> None:
        self._s3 = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
            config=Config(signature_version="s3v4"),
        )
        pub_endpoint = settings.S3_PUBLIC_ENDPOINT_URL or settings.S3_ENDPOINT_URL
        self._s3_public = (
            self._s3
            if pub_endpoint == settings.S3_ENDPOINT_URL
            else boto3.client(
                "s3",
                endpoint_url=pub_endpoint,
                aws_access_key_id=settings.S3_ACCESS_KEY,
                aws_secret_access_key=settings.S3_SECRET_KEY,
                region_name=settings.S3_REGION,
                config=Config(signature_version="s3v4"),
            )
        )
        try:
            self._s3.head_bucket(Bucket=settings.S3_BUCKET_NAME)
        except Exception:
            try:
                self._s3.create_bucket(Bucket=settings.S3_BUCKET_NAME)
            except Exception:
                pass

    def _get_blob_token(self) -> str:
        return (os.environ.get("BLOB_READ_WRITE_TOKEN") or settings.BLOB_READ_WRITE_TOKEN or "").strip()

    def _get_blob_store_id(self) -> str:
        return parse_store_id_from_rw_token(self._get_blob_token())

    def _get_blob_api_url(self) -> str:
        return (
            os.environ.get("VERCEL_BLOB_API_URL")
            or settings.VERCEL_BLOB_API_URL
            or "https://vercel.com/api/blob"
        ).rstrip("/")

    def _sync_blob_sdk_env(self) -> None:
        """Ensure official `vercel.blob.sync` SDK sees current server-side token and API URL."""
        token = self._get_blob_token()
        api_url = self._get_blob_api_url()
        if not token:
            raise RuntimeError("BLOB_READ_WRITE_TOKEN is not configured for Vercel Blob storage.")
        os.environ["BLOB_READ_WRITE_TOKEN"] = token
        os.environ["VERCEL_BLOB_API_URL"] = api_url
        current_pair = (token, api_url)
        if self._last_blob_env != current_pair:
            self._last_blob_env = current_pair
            try:
                session = get_active_sync_session()
                if hasattr(session, "_services"):
                    session._services.clear()
            except Exception:
                pass

    def is_s3(self) -> bool:
        return self.backend == "s3" and self._s3 is not None

    def is_vercel_blob(self) -> bool:
        return self.backend == "vercel_blob" and bool(self._get_blob_store_id())

    def is_direct_multipart(self) -> bool:
        return self.is_s3() or self.is_vercel_blob()

    def direct_upload_mode(self) -> str:
        if self.is_vercel_blob():
            return "vercel_blob"
        if self.is_s3():
            return "direct_s3"
        return "proxy"

    def check_readiness(self) -> Dict[str, Any]:
        if self.backend == "vercel_blob":
            store_id = self._get_blob_store_id()
            if not store_id:
                return {
                    "ready": False,
                    "backend": "vercel_blob",
                    "reason": "BLOB_READ_WRITE_TOKEN is missing or not in vercel_blob_rw_<storeId>_<secret> format.",
                }
            try:
                self._sync_blob_sdk_env()
                # Use official vercel.blob.sync.query_blobs(page_size=1) to probe store readiness
                it = vercel_blob_sync.query_blobs(page_size=1)
                next(it, None)
                return {
                    "ready": True,
                    "backend": "vercel_blob",
                    "store_id": store_id,
                    "access": "private",
                    "sdk": "vercel-blob",
                }
            except Exception as exc:
                return {
                    "ready": False,
                    "backend": "vercel_blob",
                    "store_id": store_id,
                    "reason": f"Vercel Blob store unreachable via official SDK: {exc}",
                }

        if self.backend == "s3":
            if not self._s3:
                return {"ready": False, "backend": "s3", "reason": "S3 client not initialized"}
            if settings.APP_ENV == "production" and settings.S3_ACCESS_KEY == "minioadmin":
                return {"ready": False, "backend": "s3", "reason": "Placeholder S3 credentials in production"}
            try:
                self._s3.head_bucket(Bucket=settings.S3_BUCKET_NAME)
                return {"ready": True, "backend": "s3", "bucket": settings.S3_BUCKET_NAME}
            except Exception as exc:
                return {"ready": False, "backend": "s3", "reason": f"S3 bucket unreachable: {exc}"}

        if settings.APP_ENV == "production" or os.environ.get("VERCEL"):
            return {
                "ready": False,
                "backend": self.backend,
                "reason": "Durable private object storage (STORAGE_BACKEND=vercel_blob or s3) is required on Vercel/production; /tmp or local storage is not permitted for durable files.",
            }
        if not self.local_writable:
            return {"ready": False, "backend": "local", "reason": "Local storage root is not writable"}
        return {"ready": True, "backend": "local"}

    def create_multipart_upload(self, key: str) -> str:
        clean_key = key.lstrip("/")
        if self.is_vercel_blob():
            # Browser uploads use official @vercel/blob/client (`upload` + `handleUpload`) directly;
            # server-side uploads use official `vercel.blob.sync.multipart_upload_blob` context manager.
            return "vercel-blob-sdk-managed"
        assert self._s3 is not None
        res = self._s3.create_multipart_upload(Bucket=settings.S3_BUCKET_NAME, Key=clean_key)
        return res["UploadId"]

    def presign_upload_part(self, key: str, upload_id: str, part_number: int, expires_in: int = 900) -> str:
        client = self._s3_public or self._s3
        assert client is not None
        return client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": settings.S3_BUCKET_NAME,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=expires_in,
        )

    def get_direct_part_upload_target(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        max_size_bytes: int = settings.MAX_UPLOAD_BYTES,
        expires_in: int = 900,
    ) -> Dict[str, Any]:
        clean_key = key.lstrip("/")
        if self.is_vercel_blob():
            raise RuntimeError(
                "Vercel Blob uploads use the official @vercel/blob/client handleUpload flow (/api/blob/upload), not manual part presigning."
            )
        url = self.presign_upload_part(clean_key, upload_id, part_number, expires_in=expires_in)
        return {
            "upload_url": url,
            "method": "PUT",
            "headers": {},
            "part_number": part_number,
            "expires_in_seconds": expires_in,
            "upload_mode": "direct_s3",
        }

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None:
        clean_key = key.lstrip("/")
        if self.is_vercel_blob():
            # Official @vercel/blob/client `upload(..., { multipart: true })` completes the multipart upload directly.
            return
        assert self._s3 is not None
        sorted_parts = sorted(
            parts,
            key=lambda p: int(p.get("PartNumber") if "PartNumber" in p else p.get("partNumber", 0)),
        )
        s3_parts = [
            {
                "PartNumber": int(p.get("PartNumber") if "PartNumber" in p else p.get("partNumber", 0)),
                "ETag": str(p.get("ETag") if "ETag" in p else p.get("etag", "")),
            }
            for p in sorted_parts
        ]
        self._s3.complete_multipart_upload(
            Bucket=settings.S3_BUCKET_NAME,
            Key=clean_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": s3_parts},
        )

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        if self.is_vercel_blob():
            try:
                self.delete_key(key)
            except Exception:
                pass
            return
        if not self._s3 or not upload_id:
            return
        try:
            self._s3.abort_multipart_upload(
                Bucket=settings.S3_BUCKET_NAME,
                Key=key,
                UploadId=upload_id,
            )
        except Exception:
            pass

    def head_object_size(self, key: str) -> int:
        clean_key = key.lstrip("/")
        if self.is_vercel_blob():
            self._sync_blob_sdk_env()
            try:
                meta = vercel_blob_sync.head_blob(clean_key)
                return int(meta.size)
            except BlobNotFoundError as exc:
                raise FileNotFoundError(f"Vercel Blob object not found: {clean_key}") from exc

        assert self._s3 is not None
        res = self._s3.head_object(Bucket=settings.S3_BUCKET_NAME, Key=clean_key)
        return int(res["ContentLength"])

    def read_head_bytes(self, key: str, num_bytes: int = 8192) -> bytes:
        clean_key = key.lstrip("/")
        if self.is_vercel_blob():
            self._sync_blob_sdk_env()
            try:
                chunks: list[bytes] = []
                remaining = num_bytes
                with vercel_blob_sync.stream_blob(clean_key, access="private") as download:
                    for chunk in download:
                        if not chunk:
                            continue
                        take = chunk[:remaining]
                        chunks.append(take)
                        remaining -= len(take)
                        if remaining <= 0:
                            break
                return b"".join(chunks)
            except BlobNotFoundError as exc:
                raise FileNotFoundError(f"Vercel Blob object not found: {clean_key}") from exc

        assert self._s3 is not None
        res = self._s3.get_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=clean_key,
            Range=f"bytes=0-{max(0, num_bytes - 1)}",
        )
        return res["Body"].read()

    def compute_sha256_streaming(self, key: str) -> tuple[int, str]:
        clean_key = key.lstrip("/")
        if self.is_vercel_blob():
            self._sync_blob_sdk_env()
            sha256 = hashlib.sha256()
            total = 0
            try:
                with vercel_blob_sync.stream_blob(clean_key, access="private") as download:
                    for chunk in download:
                        if chunk:
                            sha256.update(chunk)
                            total += len(chunk)
                return total, sha256.hexdigest()
            except BlobNotFoundError as exc:
                raise FileNotFoundError(f"Vercel Blob object not found: {clean_key}") from exc

        assert self._s3 is not None
        res = self._s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=clean_key)
        sha256 = hashlib.sha256()
        total = 0
        for chunk in res["Body"].iter_chunks(chunk_size=STREAM_CHUNK_BYTES):
            if chunk:
                sha256.update(chunk)
                total += len(chunk)
        return total, sha256.hexdigest()

    def open_download_stream(
        self,
        key: str,
        filename: Optional[str] = None,
        media_type: str = "application/octet-stream",
        range_header: Optional[str] = None,
        disposition: str = "attachment",
        chunk_size: int = STREAM_CHUNK_BYTES,
    ) -> Dict[str, Any]:
        """
        Open an authenticated, bounded-memory byte stream for private downloads using official SDKs.
        Supports HTTP Range / resume requests across Vercel Blob, S3, and local dev storage.
        Never buffers the full object into memory or /tmp.
        """
        clean_key = key.lstrip("/")
        base_headers: Dict[str, str] = {"Accept-Ranges": "bytes"}
        if filename:
            safe_disp_name = filename.replace('"', "")
            base_headers["Content-Disposition"] = f'{disposition}; filename="{safe_disp_name}"'

        if self.is_vercel_blob():
            self._sync_blob_sdk_env()
            try:
                head_meta = vercel_blob_sync.head_blob(clean_key)
            except BlobNotFoundError as exc:
                raise FileNotFoundError(f"Storage object not found: {clean_key}") from exc

            total_size = int(head_meta.size)
            byte_range = parse_byte_range(range_header, total_size)
            out_headers = dict(base_headers)
            if head_meta.etag:
                out_headers["ETag"] = head_meta.etag
            if head_meta.cache_control:
                out_headers["Cache-Control"] = head_meta.cache_control
            resolved_media_type = head_meta.content_type or media_type

            if byte_range is not None:
                start, end = byte_range
                status_code = 206
                length = end - start + 1
                out_headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
                out_headers["Content-Length"] = str(length)
            else:
                start, end = 0, max(0, total_size - 1)
                status_code = 200
                length = total_size
                out_headers["Content-Length"] = str(total_size)

            def _iter_blob_sdk() -> Iterator[bytes]:
                if length <= 0:
                    return
                with vercel_blob_sync.stream_blob(clean_key, access="private") as download:
                    offset = 0
                    for chunk in download:
                        if not chunk:
                            continue
                        chunk_start = offset
                        chunk_end = offset + len(chunk) - 1
                        offset += len(chunk)
                        if chunk_end < start:
                            continue
                        if chunk_start > end:
                            break
                        slice_start = max(0, start - chunk_start)
                        slice_end = min(len(chunk), end - chunk_start + 1)
                        if slice_end > slice_start:
                            yield chunk[slice_start:slice_end]
                        if chunk_end >= end:
                            break

            return {
                "status_code": status_code,
                "media_type": resolved_media_type,
                "headers": out_headers,
                "iterator": _iter_blob_sdk(),
            }

        if self.is_s3():
            assert self._s3 is not None
            total_size = self.head_object_size(clean_key)
            byte_range = parse_byte_range(range_header, total_size)
            s3_params: Dict[str, Any] = {"Bucket": settings.S3_BUCKET_NAME, "Key": clean_key}
            status_code = 200
            out_headers = dict(base_headers)
            if byte_range is not None:
                start, end = byte_range
                s3_params["Range"] = f"bytes={start}-{end}"
                status_code = 206
                out_headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
                out_headers["Content-Length"] = str(end - start + 1)
            else:
                out_headers["Content-Length"] = str(total_size)
            res = self._s3.get_object(**s3_params)
            body = res["Body"]

            def _iter_s3() -> Iterator[bytes]:
                try:
                    for chunk in body.iter_chunks(chunk_size=chunk_size):
                        if chunk:
                            yield chunk
                finally:
                    try:
                        body.close()
                    except Exception:
                        pass

            return {
                "status_code": status_code,
                "media_type": res.get("ContentType") or media_type,
                "headers": out_headers,
                "iterator": _iter_s3(),
            }

        path = self.get_local_file(clean_key)
        total_size = path.stat().st_size
        byte_range = parse_byte_range(range_header, total_size)
        out_headers = dict(base_headers)
        if byte_range is not None:
            start, end = byte_range
            status_code = 206
            length = end - start + 1
            out_headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
            out_headers["Content-Length"] = str(length)
        else:
            start, end = 0, max(0, total_size - 1)
            status_code = 200
            length = total_size
            out_headers["Content-Length"] = str(total_size)

        def _iter_local() -> Iterator[bytes]:
            remaining = length
            with open(path, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    buf = f.read(min(chunk_size, remaining))
                    if not buf:
                        break
                    remaining -= len(buf)
                    yield buf

        return {
            "status_code": status_code,
            "media_type": media_type,
            "headers": out_headers,
            "iterator": _iter_local(),
        }

    def presign_download_url(self, key: str, filename: str, expires_in: int = 900) -> str:
        client = self._s3_public or self._s3
        assert client is not None
        return client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.S3_BUCKET_NAME,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=expires_in,
        )

    def local_path_for(self, key: str) -> Path:
        if settings.APP_ENV == "production" or os.environ.get("VERCEL") or not self.local_writable:
            raise RuntimeError(
                "Durable private object storage (STORAGE_BACKEND=vercel_blob or s3) is required; local or /tmp storage is not permitted for uploaded originals or durable exports."
            )
        path = safe_join(self.local_root, *key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def scratch_path_for(self, key: str) -> Path:
        """Disposable processing path in /tmp used strictly while a worker processes an object."""
        path = safe_join(self.scratch_root, *key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_scratch_key(self, key: Optional[str]) -> None:
        if not key:
            return
        try:
            path = safe_join(self.scratch_root, *key.split("/"))
            if path.exists():
                path.unlink(missing_ok=True)
        except Exception:
            pass

    def write_chunk(self, session_id: str, chunk_index: int, data: bytes) -> None:
        chunk_path = self.local_path_for(f"chunks/{session_id}/part_{chunk_index:06d}")
        chunk_path.write_bytes(data)

    def write_chunk_idempotent(self, session_id: str, chunk_index: int, data: bytes) -> tuple[str, int]:
        chunk_path = self.local_path_for(f"chunks/{session_id}/part_{chunk_index:06d}")
        new_hash = hashlib.sha256(data).hexdigest()
        if chunk_path.exists():
            existing_hash = hashlib.sha256(chunk_path.read_bytes()).hexdigest()
            if existing_hash == new_hash:
                return "duplicate", len(data)
            raise ValueError(f"Conflicting chunk data for chunk {chunk_index}.")
        tmp_path = chunk_path.with_suffix(".tmp")
        tmp_path.write_bytes(data)
        tmp_path.replace(chunk_path)
        return "written", len(data)

    def get_session_chunks_total_bytes(self, session_id: str) -> int:
        chunk_dir = self.local_path_for(f"chunks/{session_id}")
        if not chunk_dir.exists():
            return 0
        return sum(p.stat().st_size for p in chunk_dir.glob("part_*") if p.is_file())

    def assemble_chunks(self, session_id: str, total_chunks: int, dest_key: str) -> tuple[int, str]:
        dest_path = self.local_path_for(dest_key)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".assembling")
        sha256 = hashlib.sha256()
        total_bytes = 0
        try:
            with open(tmp_path, "wb") as out_f:
                for idx in range(total_chunks):
                    part_path = self.local_path_for(f"chunks/{session_id}/part_{idx:06d}")
                    if not part_path.exists():
                        raise FileNotFoundError(f"Missing chunk {idx}")
                    with open(part_path, "rb") as part_f:
                        while True:
                            buf = part_f.read(1024 * 1024)
                            if not buf:
                                break
                            sha256.update(buf)
                            out_f.write(buf)
                            total_bytes += len(buf)
            tmp_path.replace(dest_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
        chunk_dir = self.local_path_for(f"chunks/{session_id}")
        shutil.rmtree(chunk_dir, ignore_errors=True)

        if self.backend == "s3" and self._s3 is not None:
            self._s3.upload_file(str(dest_path), settings.S3_BUCKET_NAME, dest_key)
        elif self.is_vercel_blob():
            self.save_file(dest_key, dest_path)

        return total_bytes, sha256.hexdigest()

    def abort_chunks(self, session_id: str) -> None:
        if self.backend == "local" and self.local_writable:
            chunk_dir = self.local_path_for(f"chunks/{session_id}")
            shutil.rmtree(chunk_dir, ignore_errors=True)

    def save_bytes(self, key: str, data: bytes) -> str:
        clean_key = key.lstrip("/")
        write_hook = _write_hook_var.get()
        if write_hook is not None:
            write_hook(clean_key, len(data))
        if self.is_vercel_blob():
            self._sync_blob_sdk_env()
            vercel_blob_sync.put_blob(
                clean_key,
                data,
                access="private",
                content_type="application/octet-stream",
                add_random_suffix=False,
                allow_overwrite=True,
            )
            return clean_key

        if self.backend == "s3" and self._s3 is not None:
            self._s3.put_object(Bucket=settings.S3_BUCKET_NAME, Key=clean_key, Body=data)
            return clean_key

        path = self.local_path_for(clean_key)
        path.write_bytes(data)
        return clean_key

    def save_file(self, key: str, src_path: Path) -> str:
        clean_key = key.lstrip("/")
        file_size = src_path.stat().st_size
        if self.is_vercel_blob():
            self._sync_blob_sdk_env()
            part_size = max(5 * 1024 * 1024, settings.S3_MULTIPART_PART_BYTES)
            if file_size > part_size:
                write_hook = _write_hook_var.get()
                if write_hook is not None:
                    write_hook(clean_key, file_size)
                with vercel_blob_sync.multipart_upload_blob(
                    clean_key,
                    access="private",
                    content_type="application/octet-stream",
                    add_random_suffix=False,
                    allow_overwrite=True,
                ) as mpu:
                    with open(src_path, "rb") as f:
                        while True:
                            chunk = f.read(part_size)
                            if not chunk:
                                break
                            mpu.upload_part(chunk)
                return clean_key
            return self.save_bytes(clean_key, src_path.read_bytes())

        write_hook = _write_hook_var.get()
        if write_hook is not None:
            write_hook(clean_key, file_size)
        if self.backend == "s3" and self._s3 is not None:
            self._s3.upload_file(str(src_path), settings.S3_BUCKET_NAME, clean_key)
            return clean_key

        dest = self.local_path_for(clean_key)
        if src_path.resolve() != dest.resolve():
            shutil.copy2(src_path, dest)
        return clean_key

    def get_local_file(self, key: str) -> Path:
        clean_key = key.lstrip("/")
        if self.is_vercel_blob():
            self._sync_blob_sdk_env()
            scratch_path = self.scratch_path_for(clean_key)
            if not scratch_path.exists():
                tmp_path = scratch_path.with_suffix(scratch_path.suffix + ".downloading")
                try:
                    with vercel_blob_sync.stream_blob(clean_key, access="private") as download:
                        with open(tmp_path, "wb") as out_f:
                            for chunk in download:
                                if chunk:
                                    out_f.write(chunk)
                    tmp_path.replace(scratch_path)
                except BlobNotFoundError as exc:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                    raise FileNotFoundError(f"Storage object not found: {clean_key}") from exc
                except Exception:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                    raise
            return scratch_path

        if self.backend == "s3" and self._s3 is not None:
            scratch_path = self.scratch_path_for(clean_key)
            if not scratch_path.exists():
                self._s3.download_file(settings.S3_BUCKET_NAME, clean_key, str(scratch_path))
            if not scratch_path.exists():
                raise FileNotFoundError(f"Storage object not found: {clean_key}")
            return scratch_path

        path = self.local_path_for(clean_key)
        if not path.exists():
            raise FileNotFoundError(f"Storage object not found: {clean_key}")
        return path

    def read_bytes(self, key: str) -> bytes:
        clean_key = key.lstrip("/")
        if self.is_vercel_blob():
            self._sync_blob_sdk_env()
            try:
                res = vercel_blob_sync.get_blob(clean_key, access="private")
                return bytes(res.body)
            except BlobNotFoundError as exc:
                raise FileNotFoundError(f"Storage object not found: {clean_key}") from exc
        if self.backend == "s3" and self._s3 is not None:
            res = self._s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=clean_key)
            return res["Body"].read()
        return self.get_local_file(clean_key).read_bytes()

    def delete_key(self, key: Optional[str]) -> None:
        if not key:
            return
        clean_key = key.lstrip("/")
        delete_hook = _delete_hook_var.get()
        if delete_hook is not None:
            try:
                delete_hook(clean_key)
            except Exception:
                pass
        for root in (self.local_root, self.scratch_root):
            try:
                path = safe_join(root, *clean_key.split("/"))
                if path.exists():
                    path.unlink()
            except Exception:
                pass
        if self.is_vercel_blob():
            try:
                self._sync_blob_sdk_env()
                vercel_blob_sync.delete_blob(clean_key)
            except Exception:
                pass
        elif self.backend == "s3" and self._s3 is not None:
            try:
                self._s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=clean_key)
            except Exception:
                pass

    def delete_prefix(self, prefix: str) -> None:
        clean_prefix = prefix.lstrip("/")
        for root in (self.local_root, self.scratch_root):
            try:
                target_dir = safe_join(root, *clean_prefix.split("/"))
                if target_dir.exists() and target_dir.is_dir():
                    shutil.rmtree(target_dir, ignore_errors=True)
            except Exception:
                pass
        if self.is_vercel_blob():
            try:
                self._sync_blob_sdk_env()
                batch: list[str] = []
                for item in vercel_blob_sync.query_blobs(prefix=clean_prefix, page_size=500):
                    batch.append(item.url)
                    if len(batch) >= 500:
                        vercel_blob_sync.delete_blob(batch)
                        batch.clear()
                if batch:
                    vercel_blob_sync.delete_blob(batch)
            except Exception:
                pass
        elif self.backend == "s3" and self._s3 is not None:
            try:
                paginator = self._s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=settings.S3_BUCKET_NAME, Prefix=clean_prefix):
                    objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                    if objs:
                        self._s3.delete_objects(Bucket=settings.S3_BUCKET_NAME, Delete={"Objects": objs})
            except Exception:
                pass

    def get_total_usage_bytes(self) -> int:
        if self.is_vercel_blob():
            try:
                self._sync_blob_sdk_env()
                return sum(int(item.size) for item in vercel_blob_sync.query_blobs(page_size=1000))
            except Exception:
                return 0

        if self.backend == "s3" and self._s3 is not None:
            try:
                total = 0
                paginator = self._s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=settings.S3_BUCKET_NAME):
                    for obj in page.get("Contents", []):
                        total += int(obj.get("Size", 0))
                return total
            except Exception:
                return 0

        if not self.local_root.exists():
            return 0
        total = 0
        for p in self.local_root.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        return total


storage = PrivateStorage()
