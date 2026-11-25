"""Phase 1 pipeline outputs (transcripts, places, mentions, cards) and Phase 3 trips.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import SPARSEVEC, VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DENSE_DIM = 1024
SPARSE_DIM = 250002
JSONB = postgresql.JSONB()


def _ts(name: str, nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["users.id"], name=f"fk_{table}_user_id_users", ondelete="CASCADE"
    )


def upgrade() -> None:
    # --- videos: intermediate pipeline results, so stages can be skipped on resume
    op.add_column("videos", sa.Column("language", sa.String(16), nullable=True))
    op.add_column("videos", sa.Column("transcript", JSONB, nullable=True))
    op.add_column("videos", sa.Column("ocr", JSONB, nullable=True))
    op.add_column("videos", _ts("extracted_at", nullable=True))

    # --- transcript chunks
    op.create_table(
        "transcript_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=False),  # unaccented text + ocr
        sa.Column("dense", VECTOR(DENSE_DIM), nullable=True),
        sa.Column("sparse", SPARSEVEC(SPARSE_DIM), nullable=True),
        _user_fk("transcript_chunks"),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name="fk_transcript_chunks_video_id_videos",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("video_id", "seq", name="uq_transcript_chunks_video_id_seq"),
    )
    op.create_index("ix_transcript_chunks_user_id", "transcript_chunks", ["user_id"])

    # --- places
    op.create_table(
        "places",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("name_norm", sa.String(256), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("area", sa.String(256), nullable=True),
        sa.Column("address", sa.String(512), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("osm_type", sa.String(16), nullable=True),
        sa.Column("osm_id", sa.BigInteger(), nullable=True),
        sa.Column("osm_opening_hours", sa.String(512), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _user_fk("places"),
    )
    op.create_index("ix_places_user_id", "places", ["user_id"])
    op.create_index(
        "uq_places_user_osm",
        "places",
        ["user_id", "osm_type", "osm_id"],
        unique=True,
        postgresql_where=sa.text("osm_id IS NOT NULL"),
    )
    op.create_table(
        "place_aliases",
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("alias", sa.String(256), nullable=False),
        sa.Column("alias_norm", sa.String(256), nullable=False),
        sa.PrimaryKeyConstraint("place_id", "alias_norm", name="pk_place_aliases"),
        sa.ForeignKeyConstraint(
            ["place_id"],
            ["places.id"],
            name="fk_place_aliases_place_id_places",
            ondelete="CASCADE",
        ),
    )

    # --- mentions (one per place x video) and review candidates
    op.create_table(
        "place_mentions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("source_start_ms", sa.Integer(), nullable=False),
        sa.Column("source_end_ms", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),  # pending | review | resolved
        _ts("created_at"),
        _user_fk("place_mentions"),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name="fk_place_mentions_video_id_videos",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["place_id"],
            ["places.id"],
            name="fk_place_mentions_place_id_places",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'review', 'resolved')", name="ck_place_mentions_status"
        ),
    )
    op.create_index("ix_place_mentions_video_id", "place_mentions", ["video_id"])
    op.create_index("ix_place_mentions_place_id", "place_mentions", ["place_id"])
    op.create_table(
        "place_match_candidates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("mention_id", sa.Uuid(), nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=True),  # an existing place of the user ...
        sa.Column("osm", JSONB, nullable=True),  # ... or an OSM POI not on the map yet
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),  # pending | accepted | rejected
        _ts("created_at"),
        _user_fk("place_match_candidates"),
        sa.ForeignKeyConstraint(
            ["mention_id"],
            ["place_mentions.id"],
            name="fk_place_match_candidates_mention_id_place_mentions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["place_id"],
            ["places.id"],
            name="fk_place_match_candidates_place_id_places",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(place_id IS NULL) <> (osm IS NULL)", name="ck_place_match_candidates_target"
        ),
    )
    op.create_index(
        "ix_place_match_candidates_mention_id", "place_match_candidates", ["mention_id"]
    )

    # --- place cards (aggregated view + search index)
    op.create_table(
        "place_cards",
        sa.Column("place_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("aggregated", JSONB, nullable=False),
        sa.Column("conflicts", JSONB, nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("summary_stale", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("indexed_version", sa.Integer(), nullable=True),
        sa.Column("card_text", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("dense", VECTOR(DENSE_DIM), nullable=True),
        sa.Column("sparse", SPARSEVEC(SPARSE_DIM), nullable=True),
        _ts("updated_at"),
        _user_fk("place_cards"),
        sa.ForeignKeyConstraint(
            ["place_id"],
            ["places.id"],
            name="fk_place_cards_place_id_places",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_place_cards_user_id", "place_cards", ["user_id"])

    # --- search indexes
    for table in ("transcript_chunks", "place_cards"):
        op.execute(f"CREATE INDEX ix_{table}_dense ON {table} USING hnsw (dense vector_cosine_ops)")
        op.execute(
            f"CREATE INDEX ix_{table}_sparse ON {table} USING hnsw (sparse sparsevec_ip_ops)"
        )
        op.execute(
            f"CREATE INDEX ix_{table}_search_trgm ON {table} USING gin (search_text gin_trgm_ops)"
        )
    op.execute("CREATE INDEX ix_places_name_trgm ON places USING gin (name_norm gin_trgm_ops)")
    op.execute(
        "CREATE INDEX ix_place_aliases_trgm ON place_aliases USING gin (alias_norm gin_trgm_ops)"
    )

    # --- Phase 3: trips
    op.create_table(
        "trips",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("area", sa.String(256), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("preferences", JSONB, nullable=False),
        sa.Column("routing_source", sa.String(16), nullable=False),
        sa.Column("unscheduled", JSONB, nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _user_fk("trips"),
        sa.CheckConstraint("end_date >= start_date", name="ck_trips_dates"),
    )
    op.create_index("ix_trips_user_id", "trips", ["user_id"])
    op.create_table(
        "trip_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("trip_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),  # 0-based day index
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("start_min", sa.Integer(), nullable=False),  # minutes since 00:00 that day
        sa.Column("end_min", sa.Integer(), nullable=False),
        sa.Column("travel_from_prev", JSONB, nullable=True),
        sa.Column("cost_estimate", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trip_id"], ["trips.id"], name="fk_trip_items_trip_id_trips", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["place_id"], ["places.id"], name="fk_trip_items_place_id_places", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_trip_items_trip_id", "trip_items", ["trip_id"])


def downgrade() -> None:
    op.drop_table("trip_items")
    op.drop_table("trips")
    op.drop_table("place_cards")
    op.drop_table("place_match_candidates")
    op.drop_table("place_mentions")
    op.drop_table("place_aliases")
    op.drop_table("places")
    op.drop_table("transcript_chunks")
    for column in ("extracted_at", "ocr", "transcript", "language"):
        op.drop_column("videos", column)
