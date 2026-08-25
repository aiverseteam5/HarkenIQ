"""QA-025: the A2.5 resource monitor is constructed and consulted.

ResourceMonitor existed (and was tested) since R3a but was imported by
nothing in production — HARKENIQ_RESOURCES_PROFILE was silently discarded.
"""

from harkeniq.agent import Agent
from harkeniq.config import load_config


def make_agent(profile: str | None = None) -> Agent:
    env = {"HARKENIQ_BMC_HOST": "https://127.0.0.1:9"}
    if profile:
        env["HARKENIQ_RESOURCES_PROFILE"] = profile
    config = load_config(env=env)
    return Agent(config)


class TestResourceWiring:
    def test_default_profile_is_standard(self):
        agent = make_agent()
        assert agent.resource_monitor is not None
        assert agent.resource_monitor.profile.name == "standard"

    def test_env_profile_override_finally_works(self):
        # The compose file set this env var for months; it did nothing.
        agent = make_agent("constrained")
        assert agent.resource_monitor.profile.name == "constrained"
        assert agent.resource_monitor.profile.memory_hard_mb == 50

    def test_multiplier_defaults_to_normal(self):
        agent = make_agent()
        assert agent.resource_monitor.poll_interval_multiplier == 1.0
