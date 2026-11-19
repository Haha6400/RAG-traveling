"""Initial schema: extensions, users, jobs, procrastinate queue.

Revision ID: 0001
Revises:
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from procrastinate.schema import SchemaManager
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATUSES = ("pending", "processing", "needs_review", "done", "failed")

# procrastinate ships no "drop schema" script, so remove everything it created by name.
DROP_PROCRASTINATE = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables
           WHERE schemaname = current_schema() AND tablename LIKE 'procrastinate%' LOOP
    EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
           JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = current_schema() AND p.proname LIKE 'procrastinate%' LOOP
    EXECUTE 'DROP FUNCTION IF EXISTS ' || r.sig || ' CASCADE';
  END LOOP;
  FOR r IN SELECT t.typname FROM pg_type t
           JOIN pg_namespace n ON n.oid = t.typnamespace
           WHERE n.nspname = current_schema() AND t.typname LIKE 'procrastinate%'
             AND t.typtype IN ('e', 'c', 'd') LOOP
    EXECUTE 'DROP TYPE IF EXISTS ' || quote_ident(r.typname) || ' CASCADE';
  END LOOP;
END $$;
"""


def _exec_raw(sql: str) -> None:
    # Multi-statement SQL must bypass bind-parameter parsing and the extended protocol.
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})


def upgrade() -> None:
    # vector / pg_trgm / unaccent are used from Phase 1 on; enabling them now fails fast if
    # the Postgres image lacks them.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=True),  # FK to videos added in Phase 1
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(16), nullable=True),
        sa.Column("progress", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_jobs_user_id_users", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in JOB_STATUSES) + ")",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_jobs_progress"),
    )
    op.create_index("ix_jobs_user_id_created_at", "jobs", ["user_id", sa.text("created_at DESC")])

    _exec_raw(SchemaManager.get_schema())


def downgrade() -> None:
    _exec_raw(DROP_PROCRASTINATE)
    op.drop_index("ix_jobs_user_id_created_at", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("users")
