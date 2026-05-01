"""
Capture models — single-table PostgreSQL schema for Eido portfolio data.

Maps to the DynamoDB design in the implementation plan using PostgreSQL
for the MVP (DynamoDB migration can happen at scale).

Tables:
  - users           (UserProfile)
  - captures        (Asset + Metadata)
  - social_edges    (follows, likes — polymorphic)
  - annotations     (3D spatial pins on captures)
  - ecosystem_handoffs (BH / Yantra4D / Factlas / CEQ dispatch log)
"""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from eido_api.db.session import Base


class CaptureStatus(str, enum.Enum):
    UPLOADING = "uploading"
    QUEUED = "queued"
    PROCESSING_SFM = "processing_sfm"
    PROCESSING_3DGS = "processing_3dgs"
    PROCESSING_MESH = "processing_mesh"
    READY = "ready"
    FAILED = "failed"


class CaptureMode(str, enum.Enum):
    PHOTOGRAMMETRY = "photogrammetry"
    LIDAR = "lidar"
    DRONE = "drone"
    GAUSSIAN_SPLATTING = "3dgs"


class HandoffTarget(str, enum.Enum):
    BLUEPRINT_HARVESTER = "blueprint-harvester"
    YANTRA4D = "yantra4d"
    FACTLAS = "factlas"
    CEQ = "ceq"


class User(Base):
    __tablename__ = "users"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    janua_id = Column(String(255), unique=True, nullable=False)  # Identity from Janua
    username = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(255))
    avatar_url = Column(Text)
    bio = Column(Text)
    tier = Column(String(50), nullable=False, server_default="free")  # free | pro | studio
    follower_count = Column(Integer, nullable=False, server_default="0")
    capture_count = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    captures = relationship("Capture", back_populates="author")


class Capture(Base):
    """Core asset entity — a processed 3D reality capture."""
    __tablename__ = "captures"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    author_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    title = Column(String(255), nullable=False)
    description = Column(Text)
    mode = Column(Enum(CaptureMode), nullable=False, server_default=CaptureMode.GAUSSIAN_SPLATTING)
    status = Column(Enum(CaptureStatus), nullable=False, server_default=CaptureStatus.UPLOADING)

    # Output files (CDN URLs)
    splat_url = Column(Text)         # .spz compressed splat
    mesh_url = Column(Text)          # .glb polygon mesh
    thumbnail_url = Column(Text)
    point_cloud_url = Column(Text)   # raw .ply

    # Geometry metadata
    vertex_count = Column(Integer)
    face_count = Column(Integer)
    gaussian_count = Column(Integer)
    scale_metric = Column(String(50), server_default="millimeters")

    # Geospatial (for drone captures → Factlas)
    latitude = Column(Float)
    longitude = Column(Float)
    altitude_m = Column(Float)
    is_georeferenced = Column(Boolean, server_default="false")

    # Publishing
    is_public = Column(Boolean, nullable=False, server_default="false")
    license = Column(String(100), server_default="CC-BY-4.0")
    tags = Column(ARRAY(Text), server_default="{}")

    # Processing pipeline job tracking
    gpu_job_id = Column(String(255))
    processing_time_s = Column(Float)
    error_message = Column(Text)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    author = relationship("User", back_populates="captures")
    annotations = relationship("SpatialAnnotation", back_populates="capture")
    handoffs = relationship("EcosystemHandoff", back_populates="capture")


class SocialEdge(Base):
    """Polymorphic social graph — follows and likes."""
    __tablename__ = "social_edges"
    __table_args__ = (
        UniqueConstraint("actor_id", "target_id", "edge_type", name="uq_social_edge"),
    )

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    actor_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    target_id = Column(PGUUID(as_uuid=True), nullable=False)  # user_id or capture_id
    edge_type = Column(String(50), nullable=False)  # "follow" | "like"
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SpatialAnnotation(Base):
    """3D coordinate pin on a capture — the social commentary layer."""
    __tablename__ = "annotations"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    capture_id = Column(PGUUID(as_uuid=True), ForeignKey("captures.id"), nullable=False)
    author_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # 3D coordinates in the capture's local space
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    z = Column(Float, nullable=False)

    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    capture = relationship("Capture", back_populates="annotations")


class EcosystemHandoff(Base):
    """Audit log for every cross-platform handoff dispatched from Eido."""
    __tablename__ = "ecosystem_handoffs"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    capture_id = Column(PGUUID(as_uuid=True), ForeignKey("captures.id"), nullable=False)
    target = Column(Enum(HandoffTarget), nullable=False)
    status = Column(String(50), nullable=False, server_default="dispatched")  # dispatched | accepted | failed
    upstream_job_id = Column(String(255))
    payload = Column(JSONB, server_default="{}")
    dispatched_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    responded_at = Column(DateTime(timezone=True))

    capture = relationship("Capture", back_populates="handoffs")
