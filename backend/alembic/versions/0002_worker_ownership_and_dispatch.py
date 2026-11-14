"""Add worker ownership, lease, heartbeat, and dispatch tracking to jobs

Revision ID: 0002_worker_ownership
Revises: 0001_initial
Create Date: 2026-11-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_worker_ownership"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("execution_token", sa.String(64), nullable=True))
    op.add_column("jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("dispatch_status", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "jobs",
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("jobs", "dispatch_attempts")
    op.drop_column("jobs", "dispatch_status")
    op.drop_column("jobs", "heartbeat_at")
    op.drop_column("jobs", "lease_expires_at")
    op.drop_column("jobs", "execution_token")
