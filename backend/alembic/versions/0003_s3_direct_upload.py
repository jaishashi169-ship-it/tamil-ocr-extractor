"""Add S3 multipart upload tracking columns to upload_sessions

Revision ID: 0003_s3_direct_upload
Revises: 0002_worker_ownership
Create Date: 2026-11-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_s3_direct_upload"
down_revision = "0002_worker_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("upload_sessions", sa.Column("s3_upload_id", sa.String(255), nullable=True))
    op.add_column(
        "upload_sessions",
        sa.Column("s3_parts_json", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("upload_sessions", "s3_parts_json")
    op.drop_column("upload_sessions", "s3_upload_id")
