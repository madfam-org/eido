"""Capture georeference: coverage envelope, provenance, registration state.

Adds the columns that let a capture be *anchored* rather than merely labelled
with a coordinate:

  - ``footprint`` / ``footprint_area_m2`` — the ground extent the capture
    observed. A property is an extent; a lat/lon alone is a pin near one.
  - ``geo_source`` / ``geo_prior_count`` — provenance and a read-proof, so a
    consumer can tell operator-typed coordinates from telemetry-derived ones,
    and "no frames carried GPS" from "nothing ever looked".
  - ``is_georegistered`` — whether the sparse model was actually aligned to the
    priors, which is distinct from having coordinates at all.

Every column is nullable with a safe default: existing captures keep working
and simply report no georeference, which is the truth about them.

Revision ID: 002_capture_georeference
Revises: 001_initial_schema
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_capture_georeference"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column("captures", sa.Column("footprint", postgresql.JSONB(), nullable=True))
    op.add_column("captures", sa.Column("footprint_area_m2", sa.Float(), nullable=True))
    op.add_column("captures", sa.Column("geo_source", sa.String(length=50), nullable=True))
    op.add_column("captures", sa.Column("geo_prior_count", sa.Integer(), nullable=True))
    op.add_column(
        "captures",
        sa.Column("is_georegistered", sa.Boolean(), server_default="false", nullable=True),
    )
    # The Factlas handoff selects georeferenced captures; without this it is a
    # sequential scan over the whole table on every publish.
    op.create_index(
        "ix_captures_georeferenced",
        "captures",
        ["is_georeferenced"],
        postgresql_where=sa.text("is_georeferenced"),
    )


def downgrade() -> None:
    op.drop_index("ix_captures_georeferenced", table_name="captures")
    op.drop_column("captures", "is_georegistered")
    op.drop_column("captures", "geo_prior_count")
    op.drop_column("captures", "geo_source")
    op.drop_column("captures", "footprint_area_m2")
    op.drop_column("captures", "footprint")
