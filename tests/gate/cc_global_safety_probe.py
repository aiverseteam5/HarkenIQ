"""TEST-ONLY: Central Command with the global safety probe installed.

S3-E1 (spec A30.37 answer 2, as amended by A30.38). NEVER SHIPPED.

This file lives in the repository's test tree. The Central Command image
copies `src/` only (`deploy/r2b/Dockerfile.cc`), so no image contains it, and
no Central Command configuration -- `CCConfig`, YAML, the environment -- can
reach it: the production registry is fixed at import and nothing shipped
rebinds it (A30.38). The compose gate's own override mounts this directory
read-only at `/opt/harken-test` and runs `entrypoint-cc-probe.sh`, which runs
the SHIPPED entrypoint with only its final line replaced by

    exec python /opt/harken-test/cc_global_safety_probe.py <trigger-file>

`main()` then calls the SHIPPED `harkeniq_cc.__main__.main()` -- the same
logging, configuration loading and validation, license handling and
`runtime.run()` -- with ONE difference: once the production runtime has
started, and has recorded the production registry, this harness writes the
probe into that existing registry, in-process. Registration is the only
test-specific step. The gate, its evaluator, the autonomy composition,
dispatch revalidation, the approval path and the node boundary are the
production ones.

The probe can only narrow. Its only answers are CLEAR and CONSTRAIN, and a
probe that raises fails the gate CLOSED -- by the production evaluator's own
rule, not by anything here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Optional, Sequence

logger = logging.getLogger("harkeniq.test.global_safety_probe")

#: The member id the probe carries. The live proof walks every response and
#: the audit log for it: the gate must never let it out.
TEST_PROBE_MEMBER_ID = "test_only_probe"

#: A first line that makes the probe raise, so fail-closed can be proved live.
PROBE_RAISE = "raise"


class TestOnlyProbeMember:
    """TEST-ONLY. Never a production member; never in the shipped package.

    Constrains while its trigger file exists. The file's lines name the
    action classes it constrains; an empty file constrains every class; a
    first line of ``raise`` makes it raise, so a fail-closed gate can be
    proved on a live stack. It has no answer that clears anything the site
    assessment did not already clear.
    """

    __test__ = False  # not a pytest test class, whatever its name says

    member_id = TEST_PROBE_MEMBER_ID

    def __init__(self, trigger_path: str) -> None:
        self.trigger_path = trigger_path

    def evaluate(self, context: Any):
        from harkeniq_cc.global_safety import MemberVerdict

        if not os.path.exists(self.trigger_path):
            return MemberVerdict.CLEAR
        with open(self.trigger_path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
        if lines and lines[0].lower() == PROBE_RAISE:
            raise RuntimeError("TEST-ONLY probe asked to fail")
        classes = {ln.upper() for ln in lines}
        if not classes or context.action_type.upper() in classes:
            return MemberVerdict.CONSTRAIN
        return MemberVerdict.CLEAR


def install(trigger_path: str) -> tuple:
    """Write the probe into the EXISTING registry, in-process.

    Only onto the production registry: a registry something else already
    changed is refused, so the probe is the one and only addition. Says so
    with a WARNING every time.
    """
    from harkeniq_cc import global_safety

    trigger_path = (trigger_path or "").strip()
    if not trigger_path:
        raise ValueError("the TEST-ONLY probe needs a trigger file path")
    if global_safety.active_members() != global_safety.PRODUCTION_MEMBERS:
        raise RuntimeError(
            "refusing to install the TEST-ONLY probe over a registry that is "
            "not the production one"
        )
    global_safety._ACTIVE = global_safety.PRODUCTION_MEMBERS + (
        TestOnlyProbeMember(trigger_path),
    )
    logger.warning(
        "TEST-ONLY global safety probe INSTALLED by the test harness after "
        "startup (trigger file %s). It can only narrow execution, never widen "
        "it, and it is never part of a shipped Central Command.",
        trigger_path,
    )
    return global_safety._ACTIVE


async def serve_then_install(
    config: Any, trigger_path: str, state: Optional[Any] = None,
) -> None:
    """The production `run()`, with the probe installed once it has started.

    `state` is built exactly as `run()` builds it when none is given; it is
    built here only so the harness can see `state.started`. An install that
    fails stops the process rather than leaving it serving without the
    member the proof depends on.
    """
    from harkeniq_cc.runtime import make_state, run

    if state is None:
        state = await make_state(config)

    async def install_once_started() -> None:
        await state.started.wait()
        install(trigger_path)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(run(config, state=state), name="central_command")
            tg.create_task(install_once_started(), name="test_only_probe_install")
    except BaseExceptionGroup as group:
        # Surface a lone failure as itself, as the shipped main() expects.
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not args[0].strip():
        print("usage: cc_global_safety_probe.py <trigger-file>", file=sys.stderr)
        return 2
    trigger_path = args[0].strip()

    import harkeniq_cc.__main__ as shipped

    async def run_with_probe(config: Any) -> None:
        await serve_then_install(config, trigger_path)

    # The shipped main() calls `run(config)` by its module-global name; this
    # is the one substitution, and a unit test pins that it is honoured.
    shipped.run = run_with_probe
    return shipped.main()


if __name__ == "__main__":
    sys.exit(main())
