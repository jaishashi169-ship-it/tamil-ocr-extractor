"""Add storage_budget_state and storage_reservations tables for atomic storage capacity enforcement

Revision ID: 0004_storage_reservations
Revises: 0003_s3_direct_upload
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_storage_reservations"
down_revision = "0003_s3_direct_upload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_budget_state",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("total_budget_bytes", sa.BigInteger(), nullable=False, server_default="850000000"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "storage_reservations",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("actual_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(24), nullable=False, server_default="reserved"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_storage_reservations_user_id", "storage_reservations", ["user_id"])
    op.create_index("ix_storage_reservations_resource_type", "storage_reservations", ["resource_type"])
    op.create_index("ix_storage_reservations_resource_id", "storage_reservations", ["resource_id"])
    op.create_index("ix_storage_reservations_status", "storage_reservations", ["status"])
    op.create_index("ix_storage_reservations_expires_at", "storage_reservations", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_storage_reservations_expires_at", table_name="storage_reservations")
    op.drop_index("ix_storage_reservations_status", table_name="storage_reservations")
    op.drop_index("ix_storage_reservations_resource_id", table_name="storage_reservations")
    op.drop_index("ix_storage_reservations_resource_type", table_name="storage_reservations")
    op.drop_index("ix_storage_reservations_user_id", table_name="storage_reservations")
    op.drop_table("storage_reservations")
    op.drop_table("storage_budget_state")
