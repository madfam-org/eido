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

    def _emit_producer_payload(self, monkeypatch) -> dict:
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

            async def __aenter__(self) -> "_FakeClient":
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

        # A georeferenced drone capture — only attributes _dispatch_factlas reads.
        capture = SimpleNamespace(
            id="3f9c2b7e-8a41-4d2e-9b0c-1f2e3d4c5b6a",
            title="Zócalo aerial",
            mesh_url="https://cdn.eido.cam/3f9c/mesh.spz",
            altitude_m=120,
            latitude=19.4326,
            longitude=-99.1332,
            is_georeferenced=True,
        )

        asyncio.run(handoff._dispatch_factlas(capture))

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
