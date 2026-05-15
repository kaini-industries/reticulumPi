"""Tests for the shared geodetic utilities."""

from __future__ import annotations

import pytest

from reticulumpi.geo import bearing_deg, bearing_label, haversine_km, haversine_nm


class TestHaversine:
    def test_same_point_zero(self):
        assert haversine_nm(0, 0, 0, 0) == 0.0
        assert haversine_km(0, 0, 0, 0) == 0.0

    def test_equator_90_degrees(self):
        d = haversine_nm(0, 0, 0, 90)
        assert 5390 < d < 5410

    def test_km_vs_nm(self):
        nm = haversine_nm(40.0, -74.0, 51.5, 0.0)
        km = haversine_km(40.0, -74.0, 51.5, 0.0)
        assert abs(km / nm - 1.852) < 0.01

    def test_known_distance(self):
        # NYC to London ~5570 km
        d = haversine_km(40.7128, -74.006, 51.5074, -0.1278)
        assert 5560 < d < 5590


class TestBearing:
    def test_due_north(self):
        b = bearing_deg(0, 0, 1, 0)
        assert abs(b - 0) < 0.1

    def test_due_east(self):
        b = bearing_deg(0, 0, 0, 1)
        assert abs(b - 90) < 0.1

    def test_due_south(self):
        b = bearing_deg(1, 0, 0, 0)
        assert abs(b - 180) < 0.1

    def test_due_west(self):
        b = bearing_deg(0, 1, 0, 0)
        assert abs(b - 270) < 0.1


class TestBearingLabel:
    def test_cardinal_points(self):
        assert bearing_label(0) == "N"
        assert bearing_label(90) == "E"
        assert bearing_label(180) == "S"
        assert bearing_label(270) == "W"

    def test_intercardinal(self):
        assert bearing_label(45) == "NE"
        assert bearing_label(135) == "SE"
        assert bearing_label(225) == "SW"
        assert bearing_label(315) == "NW"

    def test_wrap_around(self):
        assert bearing_label(360) == "N"
        assert bearing_label(350) == "N"
        assert bearing_label(355) == "N"

    def test_fine_points(self):
        assert bearing_label(22.5) == "NNE"
        assert bearing_label(67.5) == "ENE"
