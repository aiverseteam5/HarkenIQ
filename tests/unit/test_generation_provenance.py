"""`harkeniq.generation_provenance` (A30.29): the marker's vocabulary and algebra.

One module, two services. The Site Manager writes the marker, Central
Command reads it, and this is the rule both ask. Everything it does not
recognise is UNKNOWN, and unknown covers nobody narrower than the tenant.
"""

from __future__ import annotations

import pytest

from harkeniq import generation_provenance as GP


class TestVocabulary:
    def test_site_and_tenant_are_the_whole_vocabulary(self):
        assert GP.SCOPES == frozenset({"site", "tenant"})
        assert GP.KEY == "generation_visibility"
        assert GP.PROJECTION_VERSION == 1

    def test_the_marker_shape_admits_no_evidence(self):
        """A boundary, never evidence: exactly three keys, and none of
        them can hold a list, a count or a metric."""
        assert GP.MARKER_KEYS == frozenset({"scope", "site_id", "projection_version"})
        site = GP.site_visibility("site-a").to_dict()
        tenant = GP.tenant_visibility().to_dict()
        assert site == {"scope": "site", "site_id": "site-a", "projection_version": 1}
        assert tenant == {"scope": "tenant", "site_id": None, "projection_version": 1}
        for marker in (site, tenant):
            assert set(marker) == GP.MARKER_KEYS
            for value in marker.values():
                assert not isinstance(value, (list, dict, float))

    def test_a_site_visibility_needs_a_site(self):
        with pytest.raises(ValueError):
            GP.site_visibility("")


class TestParseFailsClosed:
    @pytest.mark.parametrize("value", [
        None, "", "site", 1, [], ["site"],
        {},                                                   # nothing
        {"scope": "site"},                                    # site without site id
        {"scope": "site", "site_id": ""},                     # empty site id
        {"scope": "site", "site_id": None},
        {"scope": "site", "site_id": 3, "projection_version": 1},
        {"scope": "tenant", "site_id": "site-a", "projection_version": 1},  # tenant WITH a site
        {"scope": "device", "site_id": "d1", "projection_version": 1},      # unknown scope
        {"scope": "SITE", "site_id": "site-a", "projection_version": 1},
        {"scope": "site", "site_id": "site-a"},                             # no version
        {"scope": "site", "site_id": "site-a", "projection_version": "1"},
        {"scope": "site", "site_id": "site-a", "projection_version": True},
        {"scope": "site", "site_id": "site-a", "projection_version": 0},
        {"scope": "site", "site_id": "site-a", "projection_version": 2},    # newer than this reader
        # An extra key -- a site list, a count, anything -- refuses the
        # whole marker: the boundary may not grow evidence.
        {"scope": "site", "site_id": "site-a", "projection_version": 1, "sites": ["site-c"]},
        {"scope": "site", "site_id": "site-a", "projection_version": 1, "total": 40},
    ])
    def test_unrecognised_is_unknown(self, value):
        assert GP.parse(value) is None
        assert GP.covers(value, frozenset({"site-a"})) is False
        assert GP.covers(value, frozenset()) is False

    def test_well_formed_round_trips(self):
        for marker in (GP.site_visibility("site-a"), GP.tenant_visibility()):
            assert GP.parse(marker.to_dict()) == marker


class TestCoveredBy:
    def test_tenant_wide_reader_is_covered_by_anything_including_nothing(self):
        assert GP.covers(None, None) is True
        assert GP.covers({"garbage": 1}, None) is True
        assert GP.covers(GP.tenant_visibility().to_dict(), None) is True
        assert GP.covers(GP.site_visibility("site-c").to_dict(), None) is True

    def test_scoped_reader_needs_the_marked_site_now(self):
        marker = GP.site_visibility("site-a").to_dict()
        assert GP.covers(marker, frozenset({"site-a"})) is True
        assert GP.covers(marker, frozenset({"site-a", "site-b"})) is True
        assert GP.covers(marker, frozenset({"site-b"})) is False
        assert GP.covers(marker, frozenset()) is False

    def test_tenant_marker_covers_no_scoped_reader(self):
        marker = GP.tenant_visibility().to_dict()
        for held in (frozenset(), frozenset({"site-a"}), frozenset({"site-a", "site-b", "site-c"})):
            assert GP.covers(marker, held) is False

    def test_the_marker_is_not_authority(self):
        """Same stored marker, different CURRENT reach, different answer:
        the marker names a site; whether the reader holds it is asked at
        read time, every time."""
        marker = GP.site_visibility("site-a").to_dict()
        assert GP.covers(marker, frozenset({"site-a"})) is True   # holds A
        assert GP.covers(marker, frozenset({"site-b"})) is False  # A revoked, B granted
        assert GP.covers(marker, frozenset({"site-a"})) is True   # A restored


class TestCombine:
    A = GP.site_visibility("site-a")
    B = GP.site_visibility("site-b")
    T = GP.tenant_visibility()

    def test_one_site_throughout_is_that_site(self):
        assert GP.combine([self.A]) == self.A
        assert GP.combine([self.A, self.A, self.A]) == self.A

    def test_unknown_is_contagious(self):
        assert GP.combine([self.A, None]) is None
        assert GP.combine([None, self.A]) is None
        assert GP.combine([self.T, None]) is None

    def test_tenant_is_contagious(self):
        assert GP.combine([self.A, self.T]) == self.T
        assert GP.combine([self.T, self.A]) == self.T
        assert GP.combine([self.T]) == self.T

    def test_two_sites_is_tenant_not_a_list(self):
        """The vocabulary has no multi-site form on purpose: a list of
        sites in the marker would BE a site list."""
        joined = GP.combine([self.A, self.B])
        assert joined == self.T
        assert "site-a" not in str(joined.to_dict()) and "site-b" not in str(joined.to_dict())

    def test_nothing_is_unknown(self):
        assert GP.combine([]) is None

    def test_order_does_not_matter(self):
        import itertools
        for parts in itertools.permutations([self.A, self.B, self.T]):
            assert GP.combine(parts) == self.T
        for parts in itertools.permutations([self.A, self.A, None]):
            assert GP.combine(parts) is None
