"""Initial schema migration — all Eido tables.

Revision ID: 001
Revises:
Create Date: 2026-04-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── users ──────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("janua_id", sa.String(255), nullable=False),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("bio", sa.Text()),
        sa.Column("tier", sa.String(50), nullable=False, server_default="free"),
        sa.Column("follower_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("capture_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("janua_id"),
        sa.UniqueConstraint("username"),
    )

    # ── captures ───────────────────────────────────────────────────────────────
    op.create_table(
        "captures",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("mode", sa.Enum("photogrammetry", "lidar", "drone", "3dgs", name="capturemode"), nullable=False, server_default="3dgs"),
        sa.Column("status", sa.Enum("uploading", "queued", "processing_sfm", "processing_3dgs", "processing_mesh", "ready", "failed", name="capturestatus"), nullable=False, server_default="uploading"),
        sa.Column("splat_url", sa.Text()),
        sa.Column("mesh_url", sa.Text()),
        sa.Column("thumbnail_url", sa.Text()),
        sa.Column("point_cloud_url", sa.Text()),
        sa.Column("vertex_count", sa.Integer()),
        sa.Column("face_count", sa.Integer()),
        sa.Column("gaussian_count", sa.Integer()),
        sa.Column("scale_metric", sa.String(50), server_default="millimeters"),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("altitude_m", sa.Float()),
        sa.Column("is_georeferenced", sa.Boolean(), server_default="false"),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("license", sa.String(100), server_default="CC-BY-4.0"),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}"),
        sa.Column("gpu_job_id", sa.String(255)),
        sa.Column("processing_time_s", sa.Float()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_captures_author_id", "captures", ["author_id"])
    op.create_index("ix_captures_is_public_status", "captures", ["is_public", "status"])

    # ── social_edges ───────────────────────────────────────────────────────────
    op.create_table(
        "social_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("edge_type", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "target_id", "edge_type", name="uq_social_edge"),
    )

    # ── annotations ────────────────────────────────────────────────────────────
    op.create_table(
        "annotations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("z", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["capture_id"], ["captures.id"]),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── ecosystem_handoffs ─────────────────────────────────────────────────────
    op.create_table(
        "ecosystem_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target", sa.Enum("blueprint-harvester", "yantra4d", "factlas", "ceq", name="handofftarget"), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="dispatched"),
        sa.Column("upstream_job_id", sa.String(255)),
        sa.Column("payload", postgresql.JSONB(), server_default="{}"),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["capture_id"], ["captures.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── collections ────────────────────────────────────────────────────────────
    op.create_table(
        "collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("cover_capture_id", postgresql.UUID(as_uuid=True)),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "collection_captures",
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("position", sa.String(50), server_default="0"),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"]),
        sa.ForeignKeyConstraint(["capture_id"], ["captures.id"]),
        sa.PrimaryKeyConstraint("collection_id", "capture_id"),
    )

    # ── api_tokens ─────────────────────────────────────────────────────────────
    op.create_table(
        "api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(12), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("api_tokens")
    op.drop_table("collection_captures")
    op.drop_table("collections")
    op.drop_table("ecosystem_handoffs")
    op.drop_table("annotations")
    op.drop_table("social_edges")
    op.drop_index("ix_captures_is_public_status", "captures")
    op.drop_index("ix_captures_author_id", "captures")
    op.drop_table("captures")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS capturemode")
    op.execute("DROP TYPE IF EXISTS capturestatus")
    op.execute("DROP TYPE IF EXISTS handofftarget")
