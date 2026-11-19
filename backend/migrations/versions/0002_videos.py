"""Videos table; jobs.video_id references it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "videos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("original_name", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("has_audio", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_videos"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_videos_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("user_id", "sha256", name="uq_videos_user_id_sha256"),
        sa.CheckConstraint("source IN ('local', 'tiktok')", name="ck_videos_source"),
    )
    op.create_index(
        "ix_videos_user_id_created_at", "videos", ["user_id", sa.text("created_at DESC")]
    )

    # Jobs created before this revision have no video; drop any dangling ids defensively.
    op.execute("UPDATE jobs SET video_id = NULL WHERE video_id IS NOT NULL")
    op.create_foreign_key(
        "fk_jobs_video_id_videos", "jobs", "videos", ["video_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_jobs_video_id", "jobs", ["video_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_video_id", table_name="jobs")
    op.drop_constraint("fk_jobs_video_id_videos", "jobs", type_="foreignkey")
    op.drop_index("ix_videos_user_id_created_at", table_name="videos")
    op.drop_table("videos")
