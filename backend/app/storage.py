import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional
import boto3
from botocore.client import Config

from app.config import get_settings
from app.security import safe_join

settings = get_settings()


class PrivateStorage:
    """Private object storage supporting S3-compatible durable storage and local dev filesystem."""

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
        if self.backend == "s3":
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

    def is_s3(self) -> bool:
        return self.backend == "s3" and self._s3 is not None

    def check_readiness(self) -> Dict[str, Any]:
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
                "reason": "Durable private object storage (STORAGE_BACKEND=s3) is required on Vercel/production; /tmp or local storage is not permitted for durable files.",
            }
        if not self.local_writable:
            return {"ready": False, "backend": "local", "reason": "Local storage root is not writable"}
        return {"ready": True, "backend": "local"}

    def create_multipart_upload(self, key: str) -> str:
        assert self._s3 is not None
        res = self._s3.create_multipart_upload(Bucket=settings.S3_BUCKET_NAME, Key=key)
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

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None:
        assert self._s3 is not None
        sorted_parts = sorted(parts, key=lambda p: int(p["PartNumber"]))
        self._s3.complete_multipart_upload(
            Bucket=settings.S3_BUCKET_NAME,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": sorted_parts},
        )

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
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
        assert self._s3 is not None
        res = self._s3.head_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        return int(res["ContentLength"])

    def read_head_bytes(self, key: str, num_bytes: int = 8192) -> bytes:
        assert self._s3 is not None
        res = self._s3.get_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=key,
            Range=f"bytes=0-{max(0, num_bytes - 1)}",
        )
        return res["Body"].read()

    def compute_sha256_streaming(self, key: str) -> tuple[int, str]:
        assert self._s3 is not None
        res = self._s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        sha256 = hashlib.sha256()
        total = 0
        for chunk in res["Body"].iter_chunks(chunk_size=1024 * 1024):
            if chunk:
                sha256.update(chunk)
                total += len(chunk)
        return total, sha256.hexdigest()

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
                "Durable private object storage (STORAGE_BACKEND=s3) is required; local or /tmp storage is not permitted for uploaded originals or durable exports."
            )
        path = safe_join(self.local_root, *key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def scratch_path_for(self, key: str) -> Path:
        """Disposable processing path in /tmp used strictly while a worker processes an S3 object."""
        path = safe_join(self.scratch_root, *key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_chunk(self, session_id: str, chunk_index: int, data: bytes) -> None:
        chunk_path = self.local_path_for(f"chunks/{session_id}/part_{chunk_index:06d}")
        chunk_path.write_bytes(data)

    def write_chunk_idempotent(self, session_id: str, chunk_index: int, data: bytes) -> tuple[str, int]:
        """
        Write a chunk idempotently:
        - If chunk doesn't exist, write atomically and return ('written', len(data)).
        - If chunk exists with identical SHA-256, return ('duplicate', len(data)).
        - If chunk exists with different content, raise ValueError (conflict).
        """
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
        # Clean up chunk directory
        chunk_dir = self.local_path_for(f"chunks/{session_id}")
        shutil.rmtree(chunk_dir, ignore_errors=True)

        if self.backend == "s3" and self._s3 is not None:
            self._s3.upload_file(str(dest_path), settings.S3_BUCKET_NAME, dest_key)

        return total_bytes, sha256.hexdigest()

    def abort_chunks(self, session_id: str) -> None:
        if self.backend == "local" and self.local_writable:
            chunk_dir = self.local_path_for(f"chunks/{session_id}")
            shutil.rmtree(chunk_dir, ignore_errors=True)

    def save_bytes(self, key: str, data: bytes) -> str:
        if self.backend == "s3" and self._s3 is not None:
            self._s3.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=data)
            return key
        path = self.local_path_for(key)
        path.write_bytes(data)
        return key

    def save_file(self, key: str, src_path: Path) -> str:
        if self.backend == "s3" and self._s3 is not None:
            self._s3.upload_file(str(src_path), settings.S3_BUCKET_NAME, key)
            return key
        dest = self.local_path_for(key)
        if src_path.resolve() != dest.resolve():
            shutil.copy2(src_path, dest)
        return key

    def get_local_file(self, key: str) -> Path:
        if self.backend == "s3" and self._s3 is not None:
            scratch_path = self.scratch_path_for(key)
            if not scratch_path.exists():
                self._s3.download_file(settings.S3_BUCKET_NAME, key, str(scratch_path))
            if not scratch_path.exists():
                raise FileNotFoundError(f"Storage object not found: {key}")
            return scratch_path
        path = self.local_path_for(key)
        if not path.exists():
            raise FileNotFoundError(f"Storage object not found: {key}")
        return path

    def read_bytes(self, key: str) -> bytes:
        if self.backend == "s3" and self._s3 is not None:
            res = self._s3.get_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
            return res["Body"].read()
        return self.get_local_file(key).read_bytes()

    def delete_key(self, key: Optional[str]) -> None:
        if not key:
            return
        for root in (self.local_root, self.scratch_root):
            try:
                path = safe_join(root, *key.split("/"))
                if path.exists():
                    path.unlink()
            except Exception:
                pass
        if self.backend == "s3" and self._s3 is not None:
            try:
                self._s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
            except Exception:
                pass

    def delete_prefix(self, prefix: str) -> None:
        for root in (self.local_root, self.scratch_root):
            try:
                target_dir = safe_join(root, *prefix.split("/"))
                if target_dir.exists() and target_dir.is_dir():
                    shutil.rmtree(target_dir, ignore_errors=True)
            except Exception:
                pass
        if self.backend == "s3" and self._s3 is not None:
            try:
                paginator = self._s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=settings.S3_BUCKET_NAME, Prefix=prefix):
                    objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                    if objs:
                        self._s3.delete_objects(Bucket=settings.S3_BUCKET_NAME, Delete={"Objects": objs})
            except Exception:
                pass

    def get_total_usage_bytes(self) -> int:
        if not self.local_root.exists():
            return 0
        total = 0
        for p in self.local_root.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        return total


storage = PrivateStorage()
