import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    BigInteger,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=new_uuid)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False, default="user")  # user | admin
    storage_quota_bytes = Column(BigInteger, nullable=False, default=750_000_000)
    retention_days = Column(Integer, nullable=False, default=7)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id = Column(String(36), primary_key=True, default=new_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(512), nullable=False)
    expected_size_bytes = Column(BigInteger, nullable=False)
    chunk_size_bytes = Column(Integer, nullable=False)
    total_chunks = Column(Integer, nullable=False)
    received_chunks = Column(JSON, nullable=False, default=list)
    sha256_hash = Column(String(64), nullable=True)
    detected_mime = Column(String(128), nullable=True)
    format_type = Column(String(32), nullable=True)
    storage_key = Column(String(512), nullable=True)
    s3_upload_id = Column(String(255), nullable=True)
    s3_parts_json = Column(JSON, nullable=False, default=list)
    status = Column(String(32), nullable=False, default="uploading")  # uploading | paused | completed | aborted
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=new_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id = Column(String(36), nullable=True, index=True)
    upload_session_id = Column(String(36), ForeignKey("upload_sessions.id", ondelete="SET NULL"), nullable=True)
    original_filename = Column(String(512), nullable=False)
    format_type = Column(String(32), nullable=False)
    file_size_bytes = Column(BigInteger, nullable=False)
    file_sha256 = Column(String(64), nullable=True)
    original_storage_key = Column(String(512), nullable=False)
    status = Column(String(32), nullable=False, default="queued", index=True)  # queued | running | completed | failed | cancelled
    progress_percent = Column(Float, nullable=False, default=0.0)
    stage_label = Column(String(128), nullable=False, default="Queued")
    page_count = Column(Integer, nullable=False, default=0)
    processed_pages = Column(Integer, nullable=False, default=0)
    settings_json = Column(JSON, nullable=False, default=dict)
    metadata_json = Column(JSON, nullable=False, default=dict)
    error_message = Column(Text, nullable=True)
    needs_password = Column(Boolean, nullable=False, default=False)
    duration_ms = Column(Integer, nullable=True)
    idempotency_key = Column(String(128), nullable=True, index=True)
    execution_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    dispatch_status = Column(String(32), nullable=False, default="pending")
    dispatch_attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="jobs")
    pages = relationship("PageResult", back_populates="job", cascade="all, delete-orphan", order_by="PageResult.page_number")
    exports = relationship("ExportArtifact", back_populates="job", cascade="all, delete-orphan")


class PageResult(Base):
    __tablename__ = "page_results"

    id = Column(String(36), primary_key=True, default=new_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number = Column(Integer, nullable=False)
    source_type = Column(String(64), nullable=False, default="unknown")
    extraction_method = Column(String(64), nullable=False, default="direct")  # direct | ocr | hybrid
    language_detected = Column(String(32), nullable=False, default="unknown")
    confidence_avg = Column(Float, nullable=True)
    raw_text = Column(Text, nullable=False, default="")
    edited_text = Column(Text, nullable=True)
    model_json = Column(JSON, nullable=False, default=dict)
    thumbnail_key = Column(String(512), nullable=True)
    preview_key = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    job = relationship("Job", back_populates="pages")


class Draft(Base):
    __tablename__ = "drafts"

    id = Column(String(36), primary_key=True, default=new_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    page_number = Column(Integer, nullable=False)
    content_text = Column(Text, nullable=False, default="")
    blocks_json = Column(JSON, nullable=False, default=list)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ExportArtifact(Base):
    __tablename__ = "export_artifacts"

    id = Column(String(36), primary_key=True, default=new_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    export_format = Column(String(32), nullable=False)  # txt | md | docx | pdf | searchable_pdf | html | json | csv | xlsx | png | jpg | webp | zip
    quality_preset = Column(String(32), nullable=False, default="high")  # original | high | balanced | smallest | custom
    compression_options_json = Column(JSON, nullable=False, default=dict)
    storage_key = Column(String(512), nullable=False)
    file_size_bytes = Column(BigInteger, nullable=False, default=0)
    uncompressed_size_bytes = Column(BigInteger, nullable=True)
    savings_percent = Column(Float, nullable=True)
    warnings_json = Column(JSON, nullable=False, default=list)
    status = Column(String(32), nullable=False, default="completed")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    job = relationship("Job", back_populates="exports")


class StudyProject(Base):
    __tablename__ = "study_projects"

    id = Column(String(36), primary_key=True, default=new_uuid)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(512), nullable=False)
    settings_json = Column(JSON, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default="queued")
    current_stage = Column(String(64), nullable=False, default="init")
    coverage_report_json = Column(JSON, nullable=False, default=dict)
    grammar_diffs_json = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    items = relationship("StudyItem", back_populates="project", cascade="all, delete-orphan", order_by="StudyItem.order_index")


class StudyItem(Base):
    __tablename__ = "study_items"

    id = Column(String(36), primary_key=True, default=new_uuid)
    project_id = Column(String(36), ForeignKey("study_projects.id", ondelete="CASCADE"), nullable=False, index=True)
    item_type = Column(String(32), nullable=False)  # guide_topic | question
    chapter = Column(String(255), nullable=False, default="General")
    topic = Column(String(255), nullable=False, default="Overview")
    marks = Column(Integer, nullable=True)  # 1, 2, 3, 5, 10
    difficulty = Column(String(32), nullable=False, default="intermediate")
    language = Column(String(32), nullable=False, default="bilingual")
    question_type = Column(String(64), nullable=True)
    prompt_text = Column(Text, nullable=False)
    answer_text = Column(Text, nullable=False, default="")
    explanation_text = Column(Text, nullable=False, default="")
    hint_text = Column(Text, nullable=False, default="")
    scoring_points_json = Column(JSON, nullable=False, default=list)
    options_json = Column(JSON, nullable=False, default=list)
    study_support_json = Column(JSON, nullable=False, default=dict)
    source_page = Column(Integer, nullable=True)
    source_ref = Column(String(512), nullable=True)
    verification_status = Column(String(32), nullable=False, default="ai_draft")  # ai_draft | auto_checked | user_approved
    is_locked = Column(Boolean, nullable=False, default=False)
    issue_note = Column(Text, nullable=True)
    order_index = Column(Integer, nullable=False, default=0)

    project = relationship("StudyProject", back_populates="items")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(128), primary_key=True)
    value_json = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
