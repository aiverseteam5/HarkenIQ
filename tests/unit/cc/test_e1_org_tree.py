"""E1.1: the pure organizational-tree arithmetic.

Path composition, the sibling-prefix trap, cycle refusal, the depth
bound, move rewriting and display assembly. No database, no HTTP: this
is the layer the API and (at E1.2) the scope resolver both stand on, so
its edges are proved here once.
"""

from __future__ import annotations

import pytest

from harkeniq_cc.org_tree import (
    DELIM,
    MAX_DEPTH,
    OrgTreeError,
    ancestor_ids,
    assemble_tree,
    check_depth,
    check_move,
    compose_path,
    depth_of,
    flatten,
    is_descendant,
    normalize_name,
    normalize_unit_type,
    rewrite_descendant_path,
    segments,
    self_id,
    subtree_prefix,
    total_site_count,
)

A = "a" * 32
B = "b" * 32
C = "c" * 32


class TestPathComposition:
    def test_a_root_path_is_the_id_between_delimiters(self):
        assert compose_path(None, A) == f"/{A}/"

    def test_a_child_appends_its_own_id(self):
        root = compose_path(None, A)
        assert compose_path(root, B) == f"/{A}/{B}/"

    def test_every_path_carries_a_trailing_delimiter(self):
        path = compose_path(compose_path(None, A), B)
        assert path.startswith(DELIM) and path.endswith(DELIM)

    def test_depth_counts_segments_not_delimiters(self):
        assert depth_of(compose_path(None, A)) == 1
        assert depth_of(compose_path(compose_path(None, A), B)) == 2

    def test_ancestors_are_root_first_and_exclude_self(self):
        path = compose_path(compose_path(compose_path(None, A), B), C)
        assert ancestor_ids(path) == [A, B]
        assert self_id(path) == C

    def test_a_root_has_no_ancestors(self):
        assert ancestor_ids(compose_path(None, A)) == []

    def test_a_malformed_parent_path_is_refused(self):
        with pytest.raises(OrgTreeError):
            compose_path("no-delimiters", A)

    def test_an_empty_unit_id_is_refused(self):
        with pytest.raises(OrgTreeError):
            compose_path(None, "")

    def test_segments_ignores_empty_pieces(self):
        assert segments("//") == []


class TestSiblingPrefixSafety:
    """The reason the trailing delimiter exists.

    Without it, a scope over Cluster 7 would silently cover Cluster 70.
    """

    def test_a_sibling_whose_id_extends_another_does_not_match(self):
        cluster_7 = "/u1/u7/"
        cluster_70 = "/u1/u70/"
        assert not is_descendant(cluster_70, cluster_7)

    def test_the_same_ids_without_trailing_delimiters_would_match(self):
        # Documents precisely what the delimiter buys: this is the bug
        # that would exist if paths were stored bare.
        assert "/u1/u70".startswith("/u1/u7")

    def test_a_real_descendant_still_matches(self):
        assert is_descendant("/u1/u7/u9/", "/u1/u7/")

    def test_a_unit_is_its_own_descendant(self):
        # Deliberate: the cycle check depends on it.
        assert is_descendant("/u1/u7/", "/u1/u7/")

    def test_empty_paths_never_match(self):
        assert not is_descendant("", "/u1/")
        assert not is_descendant("/u1/", "")

    def test_hex_ids_cannot_carry_a_delimiter_or_a_like_wildcard(self):
        from harkeniq_cc.db.models import new_id

        for _ in range(200):
            generated = new_id()
            assert len(generated) == 32
            assert all(ch in "0123456789abcdef" for ch in generated)
            assert "/" not in generated and "%" not in generated
            assert "_" not in generated

    def test_the_subtree_prefix_is_the_path_itself(self):
        assert subtree_prefix("/u1/u7/") == "/u1/u7/"


class TestDepthBound:
    def test_a_child_of_the_deepest_level_is_refused(self):
        with pytest.raises(OrgTreeError) as exc:
            check_depth(MAX_DEPTH)
        assert str(MAX_DEPTH) in str(exc.value)

    def test_a_child_one_level_short_is_allowed(self):
        check_depth(MAX_DEPTH - 1)

    def test_a_root_child_is_allowed(self):
        check_depth(1)


class TestMoveRules:
    def test_moving_a_unit_under_itself_is_refused(self):
        path = "/r/u/"
        with pytest.raises(OrgTreeError) as exc:
            check_move(path, "u", path, 2, 1)
        assert "cycle" in str(exc.value)

    def test_moving_a_unit_under_its_own_descendant_is_refused(self):
        with pytest.raises(OrgTreeError):
            check_move("/r/u/", "u", "/r/u/child/", 3, 2)

    def test_moving_to_an_unrelated_branch_is_allowed(self):
        check_move("/r/u/", "u", "/r/other/", 2, 1)

    def test_promoting_a_unit_to_root_is_allowed(self):
        check_move("/r/u/", "u", None, 0, 1)

    def test_a_move_that_would_push_descendants_past_the_bound_is_refused(self):
        # A three-level subtree cannot land under a level-7 parent: its
        # leaves would sit at level 10. Checking only the dragged node
        # would let this through.
        with pytest.raises(OrgTreeError) as exc:
            check_move("/a/b/", "b", "/x" * 7 + "/", 7, 3)
        assert "10" in str(exc.value)

    def test_the_same_subtree_fits_under_a_shallower_parent(self):
        check_move("/a/b/", "b", "/x/", 1, 3)


class TestPathRewriting:
    def test_a_descendant_is_rerooted_onto_the_new_prefix(self):
        assert (
            rewrite_descendant_path("/r/u/x/y/", "/r/u/", "/other/u/")
            == "/other/u/x/y/"
        )

    def test_rewriting_something_outside_the_subtree_is_refused(self):
        with pytest.raises(OrgTreeError):
            rewrite_descendant_path("/r/z/", "/r/u/", "/other/u/")

    def test_a_sibling_prefix_is_not_rewritten(self):
        with pytest.raises(OrgTreeError):
            rewrite_descendant_path("/u1/u70/", "/u1/u7/", "/u2/u7/")


class TestValidation:
    @pytest.mark.parametrize(
        "word", ["region", "cluster", "circle", "trust", "territory", "az",
                 "availability-zone", "business_unit"]
    )
    def test_the_customer_chooses_the_level_word(self, word):
        assert normalize_unit_type(word) == word

    def test_the_word_is_lowercased(self):
        assert normalize_unit_type("  Region  ") == "region"

    @pytest.mark.parametrize("bad", ["", "  ", "9region", "-region", "a" * 33,
                                     "region/cluster", "region unit"])
    def test_a_shape_that_is_not_a_slug_is_refused(self, bad):
        with pytest.raises(OrgTreeError):
            normalize_unit_type(bad)

    def test_a_name_is_trimmed_and_required(self):
        assert normalize_name("  Region West ") == "Region West"
        with pytest.raises(OrgTreeError):
            normalize_name("   ")

    def test_an_overlong_name_is_refused(self):
        with pytest.raises(OrgTreeError):
            normalize_name("x" * 256)


class _Unit:
    def __init__(self, uid, parent, name, depth, path, order=0, utype="region"):
        self.id, self.parent_id, self.name = uid, parent, name
        self.depth, self.path, self.sort_order = depth, path, order
        self.unit_type = utype


class TestAssembly:
    def _tree(self):
        return [
            _Unit("r", None, "meridian", 1, "/r/", utype="organization"),
            _Unit("w", "r", "Region West", 2, "/r/w/"),
            _Unit("e", "r", "Region East", 2, "/r/e/"),
            _Unit("c7", "w", "Cluster 7", 3, "/r/w/c7/", utype="cluster"),
        ]

    def test_roots_carry_their_children(self):
        roots = assemble_tree(self._tree())
        assert len(roots) == 1
        assert {c["name"] for c in roots[0]["children"]} == {
            "Region West", "Region East"
        }

    def test_children_are_ordered_by_sort_then_name(self):
        roots = assemble_tree(self._tree())
        assert [c["name"] for c in roots[0]["children"]] == [
            "Region East", "Region West"
        ]

    def test_site_counts_roll_up_through_the_subtree(self):
        roots = assemble_tree(self._tree(), site_counts={"c7": 3, "e": 1})
        assert total_site_count(roots[0]) == 4

    def test_a_unit_whose_parent_is_absent_surfaces_as_a_root(self):
        # E1.2 hands this function a subtree, not the whole tree. Dropping
        # the top node because its parent is missing would render empty.
        subtree = [u for u in self._tree() if u.id in ("w", "c7")]
        roots = assemble_tree(subtree)
        assert [r["name"] for r in roots] == ["Region West"]
        assert [c["name"] for c in roots[0]["children"]] == ["Cluster 7"]

    def test_flatten_walks_depth_first(self):
        roots = assemble_tree(self._tree())
        assert [n["name"] for n in flatten(roots)] == [
            "meridian", "Region East", "Region West", "Cluster 7"
        ]
