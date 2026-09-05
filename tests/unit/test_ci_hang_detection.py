"""The hang detector must stay armed.

CI has twice stalled dead in `tests/unit/sm/test_directives.py` and run to
GitHub's six-hour job ceiling -- py3.12 at the PR #28 merge, py3.11 at the
PR #32 merge -- reporting nothing but "cancelled" and the name of the last
file that finished. The hang does not reproduce locally, so CI is the only
instrument that can observe it, and an uninstrumented CI reports nothing.

Two layers stand between us and that silence, and they catch different
things. `pytest-timeout` names the hung test and dumps its stack, but only
sees hangs *inside* pytest. A job-level `timeout-minutes` sees everything
else -- a stalled dependency resolve, a docker pull that never lands -- but
can say nothing about why. Neither substitutes for the other, so both are
pinned here: silently losing either one returns a six-hour cancel with no
diagnostic, which is the state this slice exists to end.
"""

import pathlib

import yaml

# The slowest phase in the suite is ~6.5s and the longest *intentional* wait
# anywhere is 30s (the r2a exit gate's `wait_until`). A detector must sit far
# enough above that a loaded runner cannot clip a working test, and far enough
# below the runner ceiling to actually report before the job is cancelled.
_LONGEST_DELIBERATE_WAIT_S = 30
_RUNNER_CEILING_S = 6 * 60 * 60

_WORKFLOWS = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"


class TestPytestNamesAHungTest:
    """Read from the resolved config, not the file: what pytest actually uses."""

    @staticmethod
    def _armed_timeout(pytestconfig) -> float:
        """The configured budget in seconds, or 0.0 if the detector is off.

        `getini` hands back a *string*, so a disabled `timeout = 0` arrives as
        `"0"` -- truthy. Asserting on the raw value would pass with the
        detector switched off, which is the failure this module exists to
        prevent, so coerce before judging.
        """
        raw = pytestconfig.getini("timeout")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def test_a_timeout_is_armed(self, pytestconfig):
        assert self._armed_timeout(pytestconfig) > 0, (
            "no pytest timeout is configured, so a hung test runs until the CI "
            "runner's six-hour ceiling cancels the job without naming it"
        )

    def test_the_budget_detects_a_hang_without_clipping_slow_work(self, pytestconfig):
        timeout = self._armed_timeout(pytestconfig)
        assert timeout > _LONGEST_DELIBERATE_WAIT_S * 2, (
            f"a {timeout}s timeout is too close to the suite's longest "
            f"deliberate wait ({_LONGEST_DELIBERATE_WAIT_S}s): it would fail "
            "loaded-but-working tests, which is worse than the hang"
        )
        assert timeout < _RUNNER_CEILING_S, (
            "a timeout at or above the runner ceiling never fires -- the job "
            "is cancelled first and reports nothing"
        )

    def test_the_method_is_one_guaranteed_to_fire(self, pytestconfig):
        """`signal` reports more neatly but cannot promise to fire.

        Measured both ways: `signal` does interrupt a pure-Python block, names
        the test in the summary and lets the run continue. What it cannot
        promise is firing when the main thread sits in a non-interruptible C
        call -- and the observed hang is in the SM's gRPC tests, where the C
        core is exactly that risk. A detector that might not fire is the
        silence we are trying to end, so the guarantee outranks the tidier
        report.
        """
        assert pytestconfig.getini("timeout_method") == "thread"


class TestEveryCIJobHasACeiling:
    """The outer net, for hangs pytest cannot see at all."""

    def test_every_job_caps_its_own_runtime(self):
        for workflow in sorted(_WORKFLOWS.glob("*.yml")):
            jobs = yaml.safe_load(workflow.read_text())["jobs"]
            for name, job in jobs.items():
                cap = job.get("timeout-minutes")
                assert cap, (
                    f"{workflow.name} job '{name}' has no timeout-minutes, so a "
                    "stall outside pytest (dependency resolution, a docker pull) "
                    "holds a runner for six hours and reports only 'cancelled'"
                )
                assert cap < 6 * 60, (
                    f"{workflow.name} job '{name}' caps at {cap}min, at or above "
                    "the runner ceiling it is meant to pre-empt"
                )
