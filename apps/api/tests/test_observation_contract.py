"""Contract test: eido's Factlas handoff payload conforms to observation.v1.json.

Eido is the PRODUCER on the Eido→Factlas seam (internal-devops#197, task C):
``handoff._dispatch_factlas`` builds the body POSTed to Factlas at
``POST /api/v1/observations``, which Factlas validates as ``ObservationCreate``.
This test exercises the *real* dispatch code path, captures the exact payload it
emits, and validates it against the vendored contract's JSON Schema — so any
producer drift (a forbidden top-level field, a dropped ``type``, an out-of-range
coordinate) fails here rather than silently at the Factlas boundary.

Mirrors the payment-method-vocabulary vendoring pattern (karafiel's
``TestDhanamFormaPagoContract``): a byte-identical vendored copy of the canonical
contract, enforced by a per-repo contract test. Canonical copy lives at
``madfam-org/internal-devops/contracts/observation.v1.json``; the vendored copy
in this repo is ``apps/api/contracts/observation.v1.json`` and must stay
shape-identical with it.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

# Vendored contract (byte-identical with the internal-devops canonical copy).
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "observation.v1.json"
_CONTRACT = json.loads(_CONTRACT_PATH.read_text())
_SCHEMA = _CONTRACT["schema"]
_VALIDATOR = Draft202012Validator(_SCHEMA)


class TestFactlasObservationContract:
    """The payload eido emits to Factlas must satisfy the observation envelope."""

    #: Coverage envelope of a ~100 m box around the Zócalo, as media-prep emits it.
    _ENVELOPE = {
        "type": "Polygon",
        "coordinates": [
            [
                [-99.13368, 19.43215],
                [-99.13272, 19.43215],
                [-99.13272, 19.43305],
                [-99.13368, 19.43305],
                [-99.13368, 19.43215],
            ]
        ],
    }

    def _capture(self, **overrides) -> SimpleNamespace:
        """A georeferenced drone capture — only the attributes the dispatcher reads.

        One factory rather than inline literals, so a new Capture column the
        dispatcher starts reading fails loudly here (AttributeError) instead of
        in production.
        """
        fields = {
            "id": "3f9c2b7e-8a41-4d2e-9b0c-1f2e3d4c5b6a",
            "title": "Zócalo aerial",
            "mesh_url": "https://cdn.eido.cam/3f9c/mesh.spz",
            "altitude_m": 120,
            "latitude": 19.4326,
            "longitude": -99.1332,
            "is_georeferenced": True,
            "footprint": self._ENVELOPE,
            "footprint_area_m2": 11740.5,
            "geo_source": "dji_srt",
            "geo_prior_count": 184,
            "is_georegistered": True,
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def _emit_producer_payload(self, monkeypatch, capture=None) -> dict:
        """Run the real ``_dispatch_factlas`` and return the payload it POSTs.

        Intercepts the outbound HTTP POST and the audit-log write so no network
        or database is touched — only the producer's payload construction runs.
        """
        from eido_api.services import handoff

        captured: dict = {}

        class _FakeResp:
            def raise_for_status(self) -> None:
                return None

        class _FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self) -> _FakeClient:
                return self

            async def __aexit__(self, *args) -> bool:
                return False

            async def post(self, url, json=None, headers=None):  # noqa: A002
                captured["url"] = url
                captured["payload"] = json
                return _FakeResp()

        async def _noop_log(*args, **kwargs) -> None:
            return None

        # No real HTTP (factlas is unreachable in CI) and no DB audit write.
        monkeypatch.setattr(handoff.httpx, "AsyncClient", _FakeClient)
        monkeypatch.setattr(handoff, "_log_handoff", _noop_log)

        asyncio.run(handoff._dispatch_factlas(capture if capture is not None else self._capture()))

        assert "payload" in captured, "_dispatch_factlas did not POST an observation payload"
        return captured["payload"]

    def test_producer_payload_conforms_to_contract(self, monkeypatch):
        """The live producer payload validates against the vendored schema."""
        payload = self._emit_producer_payload(monkeypatch)

        # Raises ValidationError with a readable message if the producer drifts.
        _VALIDATOR.validate(payload)

        # Producer-specific expectations the schema alone does not pin down.
        assert payload["provider"] == "eido", "eido must self-identify as provider='eido'"
        assert payload["type"], "observation type must be non-empty"
        assert isinstance(payload["properties"], dict), (
            "producer-specific data (eido_id, mesh_url, title, altitude_m) must be "
            "nested under `properties`"
        )
        # Factlas derives h3 server-side; producers must NOT send it (factlas#16).
        assert "h3" not in payload, "eido must not send h3 — Factlas derives it"

    # --- Extent + provenance (contract revision 2026-08-04) --------------------

    def test_georeferenced_capture_emits_its_coverage_envelope(self, monkeypatch):
        """A property is an extent, so the handoff carries the polygon too."""
        payload = self._emit_producer_payload(monkeypatch)
        _VALIDATOR.validate(payload)

        assert payload["geometry"] == self._ENVELOPE
        ring = payload["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1], "GeoJSON rings must be explicitly closed"
        # The representative point must stay inside the envelope it describes.
        lons = [c[0] for c in ring]
        lats = [c[1] for c in ring]
        assert min(lons) <= payload["lon"] <= max(lons)
        assert min(lats) <= payload["lat"] <= max(lats)

    def test_geo_anchor_carries_provenance_and_a_read_proof(self, monkeypatch):
        payload = self._emit_producer_payload(monkeypatch)
        anchor = payload["geo_anchor"]

        assert anchor["source"] == "dji_srt"
        # prior_count is the read-proof: it distinguishes "no frames carried
        # GPS" from "this producer does not report that".
        assert anchor["prior_count"] == 184
        assert anchor["is_georegistered"] is True
        assert anchor["footprint_kind"] == "capture_envelope"
        assert anchor["crs"] == "EPSG:4326"

    def test_capture_without_an_envelope_still_emits_a_valid_observation(self, monkeypatch):
        """Operator-anchored captures have coordinates but no polygon."""
        capture = self._capture(
            footprint=None,
            footprint_area_m2=None,
            geo_source="operator",
            geo_prior_count=None,
            is_georegistered=False,
        )
        payload = self._emit_producer_payload(monkeypatch, capture)
        _VALIDATOR.validate(payload)

        assert "geometry" not in payload, "no envelope means no geometry key, not a null"
        anchor = payload["geo_anchor"]
        assert anchor["source"] == "operator"
        assert anchor["is_georegistered"] is False
        # Unset provenance is omitted rather than sent as null — a consumer
        # reading `prior_count: null` cannot tell absence from unreported.
        assert "prior_count" not in anchor
        assert "footprint_kind" not in anchor

    def test_flagged_georeferenced_without_coordinates_does_not_dispatch(self, monkeypatch):
        """Guard against emitting a payload that fails the contract's `required`.

        A null lat/lon 422s at the Factlas boundary, which is far harder to
        trace back than refusing to send it.
        """
        from eido_api.services import handoff

        posted: list = []

        class _FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self) -> _FakeClient:
                return self

            async def __aexit__(self, *args) -> bool:
                return False

            async def post(self, *args, **kwargs):  # pragma: no cover - must not run
                posted.append(kwargs)
                raise AssertionError("dispatched an observation with no coordinates")

        async def _noop_log(*args, **kwargs) -> None:
            return None

        monkeypatch.setattr(handoff.httpx, "AsyncClient", _FakeClient)
        monkeypatch.setattr(handoff, "_log_handoff", _noop_log)

        capture = self._capture(latitude=None, longitude=None, is_georeferenced=True)
        asyncio.run(handoff._dispatch_factlas(capture))
        assert posted == []

    # --- Drift guards: the schema must REJECT malformed payloads ----------------

    def test_lat_out_of_range_fails(self):
        bad = {"lat": 91.0, "lon": -99.1332, "type": "drone_capture"}
        with pytest.raises(ValidationError):
            _VALIDATOR.validate(bad)

    def test_missing_required_type_fails(self):
        bad = {"lat": 19.4326, "lon": -99.1332}
        with pytest.raises(ValidationError):
            _VALIDATOR.validate(bad)

    def test_forbidden_top_level_h3_fails(self):
        """additionalProperties:false — a top-level h3 (or any extra field) is rejected."""
        bad = {"lat": 19.4326, "lon": -99.1332, "type": "drone_capture", "h3": "8928308280fffff"}
        with pytest.raises(ValidationError):
            _VALIDATOR.validate(bad)

    def test_confidence_out_of_range_fails(self):
        bad = {"lat": 19.4326, "lon": -99.1332, "type": "drone_capture", "confidence": 1.5}
        with pytest.raises(ValidationError):
            _VALIDATOR.validate(bad)
