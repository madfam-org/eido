"""Unit tests for the georeference extraction primitives.

These are the functions that decide whether a capture is georeferenced at all,
so they are tested against the real shapes drone media actually carries: the
three DJI SRT dialects, EXIF rationals with hemisphere refs, and the degenerate
single-fix case.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geo import (  # noqa: E402
    GeoPrior,
    anchor_from_priors,
    convex_hull,
    footprint_polygon,
    geo_prior_from_exif,
    hull_area_m2,
    parse_dji_srt,
    square_buffer,
)

# Zócalo, Mexico City — the ecosystem's standing example coordinate.
ZOCALO_LAT, ZOCALO_LON = 19.4326, -99.1332


# ── GeoPrior validity ──────────────────────────────────────────────────────────

def test_null_island_is_rejected():
    """A zeroed GPS chip reports (0, 0); a false anchor is worse than none."""
    assert not GeoPrior(image="a.jpg", lat=0.0, lon=0.0).is_valid()


def test_out_of_range_coordinates_are_rejected():
    assert not GeoPrior(image="a.jpg", lat=91.0, lon=0.5).is_valid()
    assert not GeoPrior(image="a.jpg", lat=10.0, lon=181.0).is_valid()


def test_ordinary_fix_is_valid():
    assert GeoPrior(image="a.jpg", lat=ZOCALO_LAT, lon=ZOCALO_LON).is_valid()


# ── EXIF ───────────────────────────────────────────────────────────────────────

def test_exif_dms_to_degrees_with_hemisphere_refs():
    """19°25'57.36"N, 99°7'59.52"W → the Zócalo, with W negated."""
    prior = geo_prior_from_exif(
        "DJI_0001.JPG",
        {
            "GPSLatitude": (19, 25, 57.36),
            "GPSLatitudeRef": "N",
            "GPSLongitude": (99, 7, 59.52),
            "GPSLongitudeRef": "W",
            "GPSAltitude": 2285.4,
        },
    )
    assert prior is not None
    assert prior.lat == pytest.approx(ZOCALO_LAT, abs=1e-4)
    assert prior.lon == pytest.approx(ZOCALO_LON, abs=1e-4)
    assert prior.alt_m == pytest.approx(2285.4)
    assert prior.source == "exif"


def test_exif_southern_and_eastern_hemispheres():
    prior = geo_prior_from_exif(
        "x.jpg",
        {
            "GPSLatitude": (33, 51, 30.0),
            "GPSLatitudeRef": "S",
            "GPSLongitude": (151, 12, 36.0),
            "GPSLongitudeRef": "E",
        },
    )
    assert prior is not None
    assert prior.lat < 0 and prior.lon > 0


def test_exif_absent_or_partial_gps_returns_none():
    assert geo_prior_from_exif("x.jpg", {}) is None
    # Latitude without longitude is unusable, not half-usable.
    assert geo_prior_from_exif("x.jpg", {"GPSLatitude": (19, 25, 57.36), "GPSLatitudeRef": "N"}) is None


def test_exif_malformed_rationals_do_not_raise():
    assert geo_prior_from_exif("x.jpg", {"GPSLatitude": "garbage", "GPSLongitude": (1, 2, 3)}) is None


# ── DJI SRT dialects ───────────────────────────────────────────────────────────

SRT_MODERN = """1
00:00:00,000 --> 00:00:00,033
<font size="28">SrtCnt : 1, DiffTime : 33ms
2026-05-01 10:00:00,000,000
[iso : 100] [shutter : 1/1000] [fnum : 280] [latitude: 19.432600] [longitude: -99.133200] [rel_alt: 45.300 abs_alt: 2285.400] </font>

2
00:00:00,033 --> 00:00:00,066
<font size="28">SrtCnt : 2, DiffTime : 33ms
2026-05-01 10:00:00,033,000
[iso : 100] [shutter : 1/1000] [fnum : 280] [latitude: 19.432700] [longitude: -99.133300] [rel_alt: 45.500 abs_alt: 2285.600] </font>
"""

SRT_SPACED = """1
00:00:00,000 --> 00:00:01,000
[latitude : 19.4326] [longitude : -99.1332] [altitude : 2285.4]

2
00:00:01,000 --> 00:00:02,000
[latitude : 19.4327] [longitude : -99.1333] [altitude : 2286.0]
"""

SRT_GPS_TUPLE = """1
00:00:00,000 --> 00:00:01,000
F/2.8, SS 1000, ISO 100, EV 0, [GPS(-99.1332,19.4326,20)], D 12.5m, H 45.3m
"""


def test_srt_modern_dialect_prefers_absolute_altitude():
    priors = parse_dji_srt(SRT_MODERN)
    assert len(priors) == 2
    assert priors[0].lat == pytest.approx(19.4326)
    assert priors[0].lon == pytest.approx(-99.1332)
    # abs_alt wins over rel_alt when both are present.
    assert priors[0].alt_m == pytest.approx(2285.4)
    assert priors[0].source == "dji_srt"


def test_srt_spaced_dialect():
    priors = parse_dji_srt(SRT_SPACED)
    assert len(priors) == 2
    assert priors[1].lat == pytest.approx(19.4327)
    assert priors[1].alt_m == pytest.approx(2286.0)


def test_srt_gps_tuple_dialect_is_lon_lat_ordered():
    """DJI writes GPS(longitude, latitude, altitude) — swapping them lands the
    capture in the Indian Ocean, which is exactly the silent failure to guard."""
    priors = parse_dji_srt(SRT_GPS_TUPLE)
    assert len(priors) == 1
    assert priors[0].lat == pytest.approx(19.4326)
    assert priors[0].lon == pytest.approx(-99.1332)


def test_srt_records_without_a_fix_are_skipped_not_interpolated():
    text = SRT_MODERN + "\n3\n00:00:00,066 --> 00:00:00,099\n[iso : 100] no fix here\n"
    assert len(parse_dji_srt(text)) == 2


def test_srt_empty_input():
    assert parse_dji_srt("") == []


# ── Anchor ─────────────────────────────────────────────────────────────────────

def test_anchor_is_the_centroid_of_valid_priors():
    priors = [
        GeoPrior("a.jpg", 19.0, -99.0, 100.0),
        GeoPrior("b.jpg", 20.0, -100.0, 200.0),
    ]
    anchor = anchor_from_priors(priors)
    assert anchor is not None
    assert anchor["lat"] == pytest.approx(19.5)
    assert anchor["lon"] == pytest.approx(-99.5)
    assert anchor["alt_m"] == pytest.approx(150.0)
    assert anchor["prior_count"] == 2


def test_anchor_ignores_invalid_priors():
    priors = [GeoPrior("a.jpg", ZOCALO_LAT, ZOCALO_LON), GeoPrior("b.jpg", 0.0, 0.0)]
    anchor = anchor_from_priors(priors)
    assert anchor is not None
    assert anchor["prior_count"] == 1
    assert anchor["lat"] == pytest.approx(ZOCALO_LAT)


def test_anchor_of_nothing_is_none():
    assert anchor_from_priors([]) is None
    assert anchor_from_priors([GeoPrior("a.jpg", 0.0, 0.0)]) is None


def test_anchor_without_altitudes():
    assert anchor_from_priors([GeoPrior("a.jpg", 19.0, -99.0)])["alt_m"] is None


# ── Hull + footprint ───────────────────────────────────────────────────────────

def test_convex_hull_of_a_square_drops_the_interior_point():
    pts = [(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)]
    hull = convex_hull(pts)
    assert len(hull) == 4
    assert (0.5, 0.5) not in hull


def test_convex_hull_of_collinear_points_is_not_a_polygon():
    assert len(convex_hull([(0, 0), (1, 1), (2, 2)])) < 3


def test_footprint_is_a_closed_ring():
    priors = [
        GeoPrior("a.jpg", 19.4320, -99.1340),
        GeoPrior("b.jpg", 19.4330, -99.1340),
        GeoPrior("c.jpg", 19.4330, -99.1320),
        GeoPrior("d.jpg", 19.4320, -99.1320),
    ]
    fp = footprint_polygon(priors)
    assert fp is not None
    assert fp["type"] == "Polygon"
    ring = fp["coordinates"][0]
    assert ring[0] == ring[-1], "GeoJSON rings must be explicitly closed"
    assert len(ring) == 5
    assert fp["_degenerate"] is False


def test_single_fix_gets_a_buffered_square_not_a_broken_polygon():
    fp = footprint_polygon([GeoPrior("a.jpg", ZOCALO_LAT, ZOCALO_LON)], min_radius_m=25.0)
    assert fp is not None
    assert fp["_degenerate"] is True
    ring = fp["coordinates"][0]
    assert len(ring) == 5 and ring[0] == ring[-1]
    # ~50m square → ~2500 m², within the tolerance of the local metre scale.
    assert hull_area_m2(ring) == pytest.approx(2500, rel=0.05)


def test_footprint_of_no_priors_is_none():
    assert footprint_polygon([]) is None


def test_square_buffer_is_roughly_the_requested_size():
    ring = [list(p) for p in square_buffer(ZOCALO_LAT, ZOCALO_LON, 100.0)]
    ring.append(ring[0])
    assert hull_area_m2(ring) == pytest.approx(40000, rel=0.05)


def test_hull_area_of_a_degenerate_ring_is_zero():
    assert hull_area_m2([[0, 0], [1, 1]]) == 0.0


def test_footprint_area_matches_a_known_orbit():
    """A ~100m-square flight box should report ~10,000 m², not a wild number."""
    lat, lon = ZOCALO_LAT, ZOCALO_LON
    corners = [list(p) for p in square_buffer(lat, lon, 50.0)]
    priors = [GeoPrior(f"{i}.jpg", c[1], c[0]) for i, c in enumerate(corners)]
    fp = footprint_polygon(priors)
    assert hull_area_m2(fp["coordinates"][0]) == pytest.approx(10000, rel=0.05)
