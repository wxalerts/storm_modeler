"""Tests for the CONUS WSR-88D site table and resolver."""

from __future__ import annotations

from shapely.geometry import Polygon

from storm_modeler.data.sites import SITE_COORDS, SiteResolver, all_sites, get_site


def test_full_network_present_and_exportable():
    # The operational WSR-88D network is ~160 sites.
    assert len(SITE_COORDS) >= 150
    sites = all_sites()
    assert len(sites) == len(SITE_COORDS)
    # Exportable as plain data: every entry is well-formed.
    for s in sites:
        assert -180 <= s.lon <= 180
        assert -90 <= s.lat <= 90
        assert s.icao and s.name


def test_known_sites_have_expected_coords():
    kfws = get_site("KFWS")
    assert abs(kfws.lat - 32.57) < 0.1 and abs(kfws.lon + 97.30) < 0.1
    khgx = get_site("KHGX")
    assert abs(khgx.lat - 29.47) < 0.1 and abs(khgx.lon + 95.08) < 0.1


def test_resolver_picks_nearest_covering_site():
    resolver = SiteResolver()
    # A polygon over the DFW metroplex resolves to KFWS.
    dfw = Polygon([(-97.4, 32.5), (-97.0, 32.5), (-97.0, 32.9), (-97.4, 32.9)])
    assert resolver.for_polygon(dfw).icao == "KFWS"
    # A polygon over Houston resolves to KHGX.
    hou = Polygon([(-95.4, 29.5), (-95.0, 29.5), (-95.0, 29.9), (-95.4, 29.9)])
    assert resolver.for_polygon(hou).icao == "KHGX"
