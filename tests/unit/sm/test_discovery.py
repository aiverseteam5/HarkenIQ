"""Discovery helpers: rack hints, peer graph, components."""

from harkeniq_sm.sitemodel.discovery import components, peer_graph, rack_hint


class FakeDevice:
    def __init__(self, id, agent_id, agent_name="", peers=None):
        self.id = id
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.peers = peers


class FakeStatus:
    def __init__(self, last_peer_status):
        self.last_peer_status = last_peer_status


class TestRackHint:
    def test_patterns(self):
        assert rack_hint("rack-12-srv-04") == "rack-12"
        assert rack_hint("RACK_7-node2") == "rack-7"
        assert rack_hint("edge-rack3-a") == "rack3"  # hyphen is a boundary
        assert rack_hint("crack1-srv") is None  # no false positive inside words
        assert rack_hint("srv-04") is None
        assert rack_hint("") is None
        assert rack_hint(None) is None


class TestPeerGraph:
    def test_resolves_by_agent_id_and_name(self):
        a = FakeDevice("d1", "agent-a", "srv-a", peers=["agent-b"])
        b = FakeDevice("d2", "agent-b", "srv-b")
        c = FakeDevice("d3", "agent-c", "srv-c")
        statuses = {"d3": FakeStatus({"srv-a": "ALIVE"})}
        graph = peer_graph([a, b, c], statuses)
        assert graph["d1"] == {"d2", "d3"}
        assert graph["d2"] == {"d1"}
        assert graph["d3"] == {"d1"}

    def test_unresolvable_keys_dropped(self):
        a = FakeDevice("d1", "agent-a", peers=["10.0.0.9:5150"])
        graph = peer_graph([a], {})
        assert graph == {"d1": set()}

    def test_components(self):
        graph = {"a": {"b"}, "b": {"a"}, "c": set(), "d": {"e"}, "e": {"d"}}
        comps = sorted(sorted(c) for c in components(graph))
        assert comps == [["a", "b"], ["d", "e"]]
