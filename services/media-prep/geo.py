"""
Georeference extraction — pure functions, no I/O.

This module is the front half of the concern the 2026-08-03 path audit found
nobody owned: turning a pile of drone media into an *earth-framed* capture.
Eido owns it because Eido is the only product that ever holds both the imagery
and its GPS priors; Factlas receives located facts and never derives location
from pixels.

Three inputs produce the same `GeoPrior` shape:

  - GPS EXIF on drone stills (DJI, Autel, Skydio all write standard EXIF GPS)
  - DJI `.SRT` sidecar telemetry that ships alongside video
  - operator-supplied coordinates (handled by the caller, not here)

Everything below is deliberately dependency-light and side-effect free so it can
be unit-tested without COLMAP, ffmpeg, S3, or a GPU.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

# WGS84 ellipsoid.
_WGS84_A = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)


@dataclass(frozen=True)
class GeoPrior:
    """One georeferenced frame: where the camera was when it took the shot."""

    image: str
    lat: float
    lon: float
    #: Metres. Absolute (MSL/ellipsoidal) when known, else relative-to-takeoff.
    alt_m: float | None = None
    #: Where this prior came from — carried into lineage so a downstream
    #: consumer can tell an EXIF-derived anchor from an operator-typed one.
    source: str = "exif"

    def is_valid(self) -> bool:
        return (
            -90.0 <= self.lat <= 90.0
            and -180.0 <= self.lon <= 180.0
            # Exactly (0, 0) is Null Island — overwhelmingly a zeroed GPS chip
            # rather than a capture in the Gulf of Guinea. Drop it: a false
            # anchor is worse than no anchor, because it looks like data.
            and not (abs(self.lat) < 1e-9 and abs(self.lon) < 1e-9)
        )


# ── EXIF ───────────────────────────────────────────────────────────────────────

def _dms_to_degrees(dms: Any, ref: str | None) -> float | None:
    """EXIF stores GPS as ((deg,), (min,), (sec,)) rationals plus a N/S/E/W ref."""
    try:
        deg, minutes, seconds = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    value = deg + minutes / 60.0 + seconds / 3600.0
    if ref and ref.upper() in ("S", "W"):
        value = -value
    return value


def geo_prior_from_exif(image_name: str, gps_ifd: dict[Any, Any]) -> GeoPrior | None:
    """Build a prior from a Pillow GPSInfo IFD (already keyed by tag *name*).

    Returns ``None`` when the image carries no usable fix — the common case for
    handheld photos and for drone footage exported through an editor that
    strips metadata.
    """
    if not gps_ifd:
        return None

    lat = _dms_to_degrees(gps_ifd.get("GPSLatitude"), gps_ifd.get("GPSLatitudeRef"))
    lon = _dms_to_degrees(gps_ifd.get("GPSLongitude"), gps_ifd.get("GPSLongitudeRef"))
    if lat is None or lon is None:
        return None

    alt: float | None = None
    raw_alt = gps_ifd.get("GPSAltitude")
    if raw_alt is not None:
        try:
            alt = float(raw_alt)
            # GPSAltitudeRef 1 means "below sea level".
            if str(gps_ifd.get("GPSAltitudeRef", 0)) in ("1", "b'\\x01'"):
                alt = -alt
        except (TypeError, ValueError):
            alt = None

    prior = GeoPrior(image=image_name, lat=lat, lon=lon, alt_m=alt, source="exif")
    return prior if prior.is_valid() else None


# ── DJI SRT telemetry ──────────────────────────────────────────────────────────

# DJI has shipped at least three SRT dialects across firmware generations. All
# three appear in the wild on footage users will upload, so parse all three:
#   1. [latitude: 19.4326] [longitude: -99.1332] [rel_alt: 45.3 abs_alt: 2285.4]
#   2. [latitude : 19.4326] [longitude : -99.1332] [altitude : 2285.4]
#   3. [GPS(-99.1332,19.4326,20)]        ← note the lon,lat order
_RE_LAT = re.compile(r"\[latitude\s*:\s*([-+]?\d+\.?\d*)\]", re.IGNORECASE)
_RE_LON = re.compile(r"\[long(?:itude)?\s*:\s*([-+]?\d+\.?\d*)\]", re.IGNORECASE)
_RE_ABS_ALT = re.compile(r"abs_alt\s*:\s*([-+]?\d+\.?\d*)", re.IGNORECASE)
_RE_REL_ALT = re.compile(r"rel_alt\s*:\s*([-+]?\d+\.?\d*)", re.IGNORECASE)
_RE_ALT = re.compile(r"\[altitude\s*:\s*([-+]?\d+\.?\d*)\]", re.IGNORECASE)
_RE_GPS_TUPLE = re.compile(
    r"\[GPS\s*\(?\s*([-+]?\d+\.?\d*)\s*,\s*([-+]?\d+\.?\d*)\s*,\s*([-+]?\d+\.?\d*)\s*\)?\s*\]",
    re.IGNORECASE,
)
# Subtitle index lines delimit records in every dialect.
_RE_INDEX = re.compile(r"^\s*(\d+)\s*$")


def parse_dji_srt(text: str) -> list[GeoPrior]:
    """Extract one prior per SRT subtitle record, in file order.

    The returned ``image`` field is a synthetic ``srt:<index>`` marker — SRT
    records are keyed to video timecodes, not frames, so the caller maps them
    onto extracted frames by index. Records without a fix are skipped rather
    than interpolated: inventing positions between real fixes would be exactly
    the "cannot distinguish no-data from no-problem" failure the ecosystem's
    seam rule prohibits.
    """
    priors: list[GeoPrior] = []
    # Split on blank lines — the SRT record separator in all three dialects.
    for block in re.split(r"\n\s*\n", text):
        if not block.strip():
            continue
        lat = lon = alt = None

        m = _RE_GPS_TUPLE.search(block)
        if m:
            # Dialect 3: DJI writes (longitude, latitude, altitude).
            lon, lat, alt = float(m.group(1)), float(m.group(2)), float(m.group(3))
        else:
            m_lat, m_lon = _RE_LAT.search(block), _RE_LON.search(block)
            if m_lat and m_lon:
                lat, lon = float(m_lat.group(1)), float(m_lon.group(1))
                m_alt = _RE_ABS_ALT.search(block) or _RE_ALT.search(block) or _RE_REL_ALT.search(block)
                if m_alt:
                    alt = float(m_alt.group(1))

        if lat is None or lon is None:
            continue

        index = len(priors)
        for line in block.splitlines():
            m_idx = _RE_INDEX.match(line)
            if m_idx:
                index = int(m_idx.group(1))
                break

        prior = GeoPrior(image=f"srt:{index}", lat=lat, lon=lon, alt_m=alt, source="dji_srt")
        if prior.is_valid():
            priors.append(prior)

    return priors


# ── Geometry ───────────────────────────────────────────────────────────────────

def _metres_per_degree(lat: float) -> tuple[float, float]:
    """Local metres-per-degree for latitude and longitude at ``lat``."""
    lat_rad = math.radians(lat)
    m_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * lat_rad) + 1.175 * math.cos(4 * lat_rad)
    m_per_deg_lon = 111412.84 * math.cos(lat_rad) - 93.5 * math.cos(3 * lat_rad)
    return m_per_deg_lat, max(m_per_deg_lon, 1e-6)


def anchor_from_priors(priors: list[GeoPrior]) -> dict[str, Any] | None:
    """Collapse per-frame priors into the capture's single anchor point.

    The anchor is the centroid of the camera positions. For the orbit and grid
    patterns drone operators actually fly over a property, that lands inside the
    subject rather than on its edge, which a first-frame or bounding-box-corner
    anchor would not.
    """
    valid = [p for p in priors if p.is_valid()]
    if not valid:
        return None

    lat = sum(p.lat for p in valid) / len(valid)
    lon = sum(p.lon for p in valid) / len(valid)
    alts = [p.alt_m for p in valid if p.alt_m is not None]

    return {
        "lat": round(lat, 8),
        "lon": round(lon, 8),
        "alt_m": round(sum(alts) / len(alts), 3) if alts else None,
        "prior_count": len(valid),
        "sources": sorted({p.source for p in valid}),
    }


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain hull. Input/output are ``(x, y)`` pairs.

    Callers pass ``(lon, lat)`` degrees. Treating those as planar is accurate to
    well under a metre across a single property — the scale this is used at —
    and avoids pulling a projection library into an image that only needs it for
    a hull.
    """
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def square_buffer(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    """Axis-aligned square of half-width ``radius_m`` centred on ``lat``/``lon``."""
    m_lat, m_lon = _metres_per_degree(lat)
    d_lat, d_lon = radius_m / m_lat, radius_m / m_lon
    return [
        (lon - d_lon, lat - d_lat),
        (lon + d_lon, lat - d_lat),
        (lon + d_lon, lat + d_lat),
        (lon - d_lon, lat + d_lat),
    ]


def footprint_polygon(
    priors: list[GeoPrior], *, min_radius_m: float = 25.0
) -> dict[str, Any] | None:
    """GeoJSON Polygon covering the ground area the capture observed.

    This is the **capture coverage envelope** — the convex hull of the camera
    positions — not a cadastral parcel boundary and not a building outline. A
    property is an extent rather than a point, and this is the extent Eido can
    honestly assert from its own telemetry. Naming it anything more authoritative
    would be the kind of claim the truthfulness ratchet exists to prevent.

    Degenerate captures (a hover, or a single geotagged still) collapse to fewer
    than three distinct points; those get a ``min_radius_m`` square around the
    anchor so downstream consumers always receive a valid, non-zero-area
    Polygon rather than a hull that silently isn't one.
    """
    valid = [p for p in priors if p.is_valid()]
    if not valid:
        return None

    hull = convex_hull([(p.lon, p.lat) for p in valid])

    if len(hull) < 3:
        anchor = anchor_from_priors(valid)
        assert anchor is not None  # non-empty `valid` guarantees an anchor
        hull = square_buffer(anchor["lat"], anchor["lon"], min_radius_m)
        degenerate = True
    else:
        degenerate = False

    ring = [[round(x, 8), round(y, 8)] for x, y in hull]
    ring.append(ring[0])  # GeoJSON requires an explicitly closed ring

    return {
        "type": "Polygon",
        "coordinates": [ring],
        # Not part of the GeoJSON spec — stripped before emission, carried here
        # so the caller can flag a buffered (rather than observed) envelope.
        "_degenerate": degenerate,
    }


def hull_area_m2(ring: list[list[float]]) -> float:
    """Approximate area of a closed lon/lat ring, via the local metre scale."""
    if len(ring) < 4:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    m_lat, m_lon = _metres_per_degree(lat0)
    xs = [p[0] * m_lon for p in ring]
    ys = [p[1] * m_lat for p in ring]
    # Shoelace over the closed ring.
    area = 0.0
    for i in range(len(ring) - 1):
        area += xs[i] * ys[i + 1] - xs[i + 1] * ys[i]
    return abs(area) / 2.0
