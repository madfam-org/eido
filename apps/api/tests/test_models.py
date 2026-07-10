"""Model-layer guards.

The Postgres enum types are created (in the migrations) with the lowercase
member *values* — 'ready', '3dgs', 'ceq'. SQLAlchemy's default ``Enum`` binds
the member *name* ('READY'), which the database rejects
(``invalid input value for enum capturestatus: "READY"``) and 500s every
status/mode query — including the gallery's ``status == READY`` filter.
``_pg_enum`` sets ``values_callable`` so binding uses the values. These tests
lock the column types to the DB labels so the mismatch can't regress.
"""
from eido_api.models import (
    Capture,
    CaptureMode,
    CaptureStatus,
    EcosystemHandoff,
    HandoffTarget,
)


def test_enum_columns_use_lowercase_db_values():
    status_enums = set(Capture.__table__.c.status.type.enums)
    assert "ready" in status_enums
    assert "processing_compress" in status_enums
    assert "READY" not in status_enums  # the NAME must never reach the DB

    mode_enums = set(Capture.__table__.c.mode.type.enums)
    assert "3dgs" in mode_enums
    assert "GAUSSIAN_SPLATTING" not in mode_enums

    target_enums = set(EcosystemHandoff.__table__.c.target.type.enums)
    assert "blueprint-harvester" in target_enums
    assert "BLUEPRINT_HARVESTER" not in target_enums


def test_enum_labels_match_python_values():
    # Every column label set must equal the enum's .value set (not .name set).
    assert set(Capture.__table__.c.status.type.enums) == {s.value for s in CaptureStatus}
    assert set(Capture.__table__.c.mode.type.enums) == {m.value for m in CaptureMode}
    assert set(EcosystemHandoff.__table__.c.target.type.enums) == {t.value for t in HandoffTarget}
