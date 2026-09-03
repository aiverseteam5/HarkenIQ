"""A test may not leave a database connection thread behind it.

The suite's async engines run on aiosqlite, whose connection is owned by a
worker THREAD. An engine that is never disposed keeps that thread alive past
the end of the test; when the abandoned pool is finalized it posts a close
onto a future belonging to the test's already-closed event loop and the
thread raises `RuntimeError: Event loop is closed`. pytest blames whichever
test is running at that moment, so the failure is intermittent and lands on
an innocent test.

`tests/conftest.py` disposes every engine a test creates. These tests pin
that behaviour from both directions: that the leak is real if nothing
disposes, and that the fixture actually closes it.
"""

import asyncio
import gc
import threading
import time

from harkeniq_cc.db.base import create_all, make_engine


def _aiosqlite_threads() -> int:
    return sum(
        "_connection_worker_thread" in (t.name or "") or
        "_connection_worker_thread" in getattr(getattr(t, "_target", None), "__name__", "")
        for t in threading.enumerate()
    )


# What the abandoning test leaves behind, for the test after it to inspect.
_abandoned: dict = {}


class TestAnAbandonedEngineIsDisposedForTheTest:
    """The shape every leaking helper had: build an engine, never dispose it.

    Note what is NOT asserted here. A surviving worker thread is the wrong
    probe: without disposal the orphan raises `Event loop is closed` and then
    exits, so counting threads afterwards sees zero either way -- and a
    finalization that happens to land while the loop is still alive closes
    cleanly, which is why the CI failure is intermittent rather than
    reproducible on demand. The invariant that IS deterministic, and the one
    the fix actually provides, is that the engine a test created has been
    disposed by the time the next test runs. SQLAlchemy replaces the pool
    object on dispose, so that is directly observable.
    """

    async def test_an_engine_the_test_abandons_holds_a_worker_thread(self):
        before = _aiosqlite_threads()
        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        # deliberately no dispose: the conftest fixture owns this lifecycle
        assert _aiosqlite_threads() > before, (
            "expected the engine to hold a live aiosqlite worker thread"
        )
        _abandoned["engine"] = engine
        _abandoned["pool"] = id(engine.pool)

    def test_conftest_disposed_it_before_this_test_began(self):
        engine = _abandoned.get("engine")
        assert engine is not None, "the previous test did not run"
        assert id(engine.pool) != _abandoned["pool"], (
            "the engine abandoned by the previous test was never disposed -- "
            "its aiosqlite worker thread outlives the event loop that owns "
            "its futures, and will raise `Event loop is closed` whenever the "
            "garbage collector reaches it, against whichever test happens to "
            "be running then. The conftest engine-disposal fixture is not "
            "doing its job."
        )


class TestDisposalIsSafeWhereverItRuns:
    """The two properties the conftest fixture stands on."""

    def test_disposing_after_the_loop_closed_raises_nothing(self):
        """Teardown runs on a fresh loop; aiosqlite must follow it."""
        caught: list[str] = []
        previous_hook = threading.excepthook
        threading.excepthook = lambda a: caught.append(f"{a.exc_type.__name__}: {a.exc_value}")
        try:
            loop = asyncio.new_event_loop()

            async def build():
                engine = make_engine("sqlite+aiosqlite:///:memory:")
                await create_all(engine)
                return engine

            engine = loop.run_until_complete(build())
            loop.close()  # the test's loop dies with the engine still open

            teardown = asyncio.new_event_loop()
            try:
                teardown.run_until_complete(engine.dispose())
            finally:
                teardown.close()

            del engine
            gc.collect()
            time.sleep(0.2)
        finally:
            threading.excepthook = previous_hook
        assert caught == [], f"disposal after loop close raised in a thread: {caught}"

    def test_disposing_twice_is_a_no_op(self):
        """Fixtures that already dispose must not be broken by the fixture."""
        loop = asyncio.new_event_loop()
        try:
            async def build_and_dispose():
                engine = make_engine("sqlite+aiosqlite:///:memory:")
                await create_all(engine)
                await engine.dispose()
                return engine

            engine = loop.run_until_complete(build_and_dispose())
            loop.run_until_complete(engine.dispose())  # second dispose
        finally:
            loop.close()
