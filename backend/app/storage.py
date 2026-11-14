import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO, Optional
import boto3
from botocore.client import Config

from app.config import get_settings
from app.security import safe_join

settings = get_settings()


class PrivateStorage:
    """Private object storage supporting local filesystem and S3/MinIO."""

    def __init__(self) -> None:
        self.backend = settings.STORAGE_BACKEND.lower()
        self.local_root = Path(settings.LOCAL_STORAGE_ROOT).resolve()
        self.local_root.mkdir(parents=True, exist_ok=True)
        self._s3 = None
        if self.backend == "s3":
            self._s3 = boto3.client(
                "s3",
                endpoint_url=settings.S3_ENDPOINT_URL,
                aws_access_key_id=settings.S3_ACCESS_KEY,
                aws_secret_access_key=settings.S3_SECRET_KEY,
                region_name=settings.S3_REGION,
                config=Config(signature_version="s3v4"),
            )
            try:
                self._s3.head_bucket(Bucket=settings.S3_BUCKET_NAME)
            except Exception:
                self._s3.create_bucket(Bucket=settings.S3_BUCKET_NAME)

    def local_path_for(self, key: str) -> Path:
        path = safe_join(self.local_root, *key.split("/"))
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
        chunk_dir = self.local_path_for(f"chunks/{session_id}")
        shutil.rmtree(chunk_dir, ignore_errors=True)

    def save_bytes(self, key: str, data: bytes) -> str:
        path = self.local_path_for(key)
        path.write_bytes(data)
        if self.backend == "s3" and self._s3 is not None:
            self._s3.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=data)
        return key

    def save_file(self, key: str, src_path: Path) -> str:
        dest = self.local_path_for(key)
        if src_path.resolve() != dest.resolve():
            shutil.copy2(src_path, dest)
        if self.backend == "s3" and self._s3 is not None:
            self._s3.upload_file(str(dest), settings.S3_BUCKET_NAME, key)
        return key

    def get_local_file(self, key: str) -> Path:
        path = self.local_path_for(key)
        if not path.exists() and self.backend == "s3" and self._s3 is not None:
            self._s3.download_file(settings.S3_BUCKET_NAME, key, str(path))
        if not path.exists():
            raise FileNotFoundError(f"Storage object not found: {key}")
        return path

    def read_bytes(self, key: str) -> bytes:
        return self.get_local_file(key).read_bytes()

    def delete_key(self, key: Optional[str]) -> None:
        if not key:
            return
        try:
            path = self.local_path_for(key)
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
        try:
            target_dir = self.local_path_for(prefix)
            if target_dir.exists() and target_dir.is_dir():
                shutil.rmtree(target_dir, ignore_errors=True)
        except Exception:
            pass

    def get_total_usage_bytes(self) -> int:
        total = 0
        for p in self.local_root.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        return total


storage = PrivateStorage()
