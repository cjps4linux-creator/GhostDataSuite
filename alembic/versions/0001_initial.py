"""Initial schema for Ghost-Data Suite.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "captures",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.Text(), nullable=False),
        sa.Column("encrypted", sa.BLOB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
    )
    op.create_table(
        "meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
    )
    op.create_table(
        "capture_media",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("capture_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_captures_url_hash", "captures", ["url_hash"])
    op.create_index("ix_capture_media_capture_id", "capture_media", ["capture_id"])


def downgrade() -> None:
    op.drop_index("ix_capture_media_capture_id", table_name="capture_media")
    op.drop_index("ix_captures_url_hash", table_name="captures")
    op.drop_table("capture_media")
    op.drop_table("meta")
    op.drop_table("captures")
