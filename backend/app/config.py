from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "Tamil English Document Studio"
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-insecure-secret-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    DOWNLOAD_TOKEN_EXPIRE_SECONDS: int = 900

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./tamil_doc_studio.db"
    SYNC_DATABASE_URL: str = "sqlite:///./tamil_doc_studio.db"

    # Redis & Celery
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS: float = 0.5
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 2.0
    ALLOW_BACKGROUND_TASK_FALLBACK: bool = True

    # Private Storage
    STORAGE_BACKEND: str = "local"  # "local" or "s3"
    LOCAL_STORAGE_ROOT: str = "./private_storage"
    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_BUCKET_NAME: str = "tamil-doc-studio-private"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_REGION: str = "us-east-1"

    # Strict 500 MB (500,000,000 bytes) limit & processing quotas
    MAX_UPLOAD_BYTES: int = 500_000_000
    UPLOAD_CHUNK_BYTES: int = 5_242_880  # 5 MB chunks
    MAX_BATCH_FILES: int = 25
    MAX_PDF_PAGES: int = 500
    MAX_IMAGE_PIXELS: int = 100_000_000
    JOB_TIMEOUT_SECONDS: int = 1800
    JOB_LEASE_SECONDS: int = 300
    WORKER_MAX_MEMORY_MB: int = 2048
    DEFAULT_RETENTION_DAYS: int = 7
    USER_STORAGE_QUOTA_BYTES: int = 5_000_000_000  # 5 GB default per user

    # Binaries
    TESSERACT_CMD: str = "tesseract"
    LIBREOFFICE_CMD: str = "soffice"
    ENABLE_ISOLATED_LIBREOFFICE: bool = True

    # Optional Study Studio LLM
    OPENAI_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    STUDY_MAX_COST_USD: float = 2.00

    def validate_production_safety(self) -> None:
        """Reject insecure placeholder secrets when running in production."""
        if self.APP_ENV == "production":
            if (
                not self.SECRET_KEY
                or self.SECRET_KEY == "dev-insecure-secret-change-in-production"
                or len(self.SECRET_KEY) < 32
            ):
                raise ValueError(
                    "APP_ENV=production requires a strong SECRET_KEY (>= 32 characters, not the dev placeholder)."
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()
