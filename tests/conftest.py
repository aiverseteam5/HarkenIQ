"""Shared test fixtures for HarkenIQ."""

import datetime
import ipaddress

import pytest


@pytest.fixture(scope="session")
def dev_tls(tmp_path_factory):
    """Ephemeral CA + server cert/key for TLS tests (no openssl needed).

    Returns a dict with paths: ca (PEM), cert (server PEM), key (server PEM).
    Server cert is valid for localhost / 127.0.0.1.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    tmp = tmp_path_factory.mktemp("tls")
    now = datetime.datetime.now(datetime.timezone.utc)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "harkeniq-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    paths = {
        "ca": tmp / "ca.pem",
        "cert": tmp / "server.pem",
        "key": tmp / "server-key.pem",
    }
    paths["ca"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    paths["cert"].write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    paths["key"].write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return {k: str(v) for k, v in paths.items()}


# ---------------------------------------------------------------------------
# Async engine lifecycle: no test may outlive its own database connection
# ---------------------------------------------------------------------------
#
# `create_async_engine("sqlite+aiosqlite:///:memory:")` uses a StaticPool: one
# persistent connection, held open by an aiosqlite worker THREAD. An engine
# that is never disposed keeps that thread alive past the end of the test.
# When the abandoned pool is finalized -- whenever the garbage collector gets
# to it -- it posts a close onto a future belonging to the test's event loop,
# which by then is closed, and the worker thread raises
#
#     RuntimeError: Event loop is closed
#
# pytest's threadexception plugin surfaces that against whichever test happens
# to be running at the time, so the failure is both intermittent and blamed on
# an innocent test. It reddened main CI on ab4f8b7 (test_api_users) and warned
# locally on the identical tree against a different test entirely
# (test_rbac_ui_contract).
#
# The leak sites are module-level helper coroutines -- `_stack()`, `_client()`,
# `_sessionmaker()` -- not fixtures, so there was never a teardown hook to put
# `await engine.dispose()` in. Rather than thread try/finally through hundreds
# of call sites (and re-leak on the next helper someone writes), close the ring
# where every engine in the suite is actually born: each service's
# `db.base.make_engine` resolves `create_async_engine` as a module global at
# call time, so wrapping that global catches every engine no matter how a test
# module imported `make_engine`.

_DB_BASE_MODULES = (
    "harkeniq_cc.db.base",
    "harkeniq_console.db.base",
    "harkeniq_sm.db.base",
)

_live_engines: list = []


@pytest.fixture(scope="session", autouse=True)
def _track_async_engines():
    """Record every async engine the suite creates, at its one choke point."""
    import importlib

    patched = []
    for name in _DB_BASE_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:  # a service not installed in this environment
            continue
        real = module.create_async_engine

        def tracking(*args, _real=real, **kwargs):
            engine = _real(*args, **kwargs)
            _live_engines.append(engine)
            return engine

        module.create_async_engine = tracking
        patched.append((module, real))

    yield

    for module, real in patched:
        module.create_async_engine = real


@pytest.fixture(autouse=True)
def _dispose_async_engines():
    """Dispose the engines a test created, before its worker threads outlive it.

    Deliberately synchronous: over half the suite's tests are sync, and an
    autouse *async* fixture would not run for them. Disposal is driven on a
    fresh event loop, which is safe even once the test's own loop has closed --
    aiosqlite delivers to `future.get_loop()`, so the worker follows the new
    loop. Disposing twice is a no-op, so fixtures that already dispose on their
    own loop (the `db` and `client` fixtures) are unaffected.
    """
    import asyncio

    _live_engines.clear()
    yield
    engines, _live_engines[:] = list(_live_engines), []
    if not engines:
        return
    loop = asyncio.new_event_loop()
    try:
        for engine in engines:
            loop.run_until_complete(engine.dispose())
    finally:
        loop.close()
