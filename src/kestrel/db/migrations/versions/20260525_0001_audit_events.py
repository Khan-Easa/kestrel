"""Create audit_events table.

Revision ID: 20260525_0001
Revises:
Create Date: 2026-05-25
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260525_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("api_key_id", sa.String(64), nullable=True),
        sa.Column("route", sa.String(128), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("status", sa.Integer, nullable=False),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("execution_id", sa.String(64), nullable=True),
        sa.Column("code_length", sa.Integer, nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("timed_out", sa.Boolean, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("error_kind", sa.String(64), nullable=True),
    )
    op.create_index("ix_audit_events_ts", "audit_events", ["ts"])
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_api_key_id", "audit_events", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_api_key_id", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_audit_events_ts", table_name="audit_events")
    op.drop_table("audit_events")