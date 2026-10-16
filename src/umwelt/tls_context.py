"""Shared TLS server-context construction — umwelt#688 (part of #583).

ONE place both servers (the world engine in `server.py`, the webrender
gateway in `webrender/gateway.py`) build their TLS listener config, per
this codebase's established "read from ONE place" discipline (the same
reasoning `resolve_actor`'s own docstring gives for itself). Both servers
plumb identical `--tls-cert`/`--tls-key` argparse flags through to this
module rather than each hand-rolling its own `ssl.SSLContext`.

Architecture, decided in the spec (umwelt#688, measured against
`origin/main`, not assumed): both servers run Flask's own `app.run()`
directly — no reverse proxy anywhere in the deployment. Werkzeug's
`run_simple` (which `Flask.run` forwards to) accepts an `ssl_context`
kwarg natively, so TLS termination happens IN this process, not in front
of it. This matters beyond convenience: a reverse proxy would collapse
every caller's `remote_addr` into the proxy's own address, silently
breaking the per-source rate limiter (`server.py`'s own documented
"if a reverse proxy is EVER added... revisit this function" trigger,
umwelt#561). Choosing Flask-native TLS is what keeps that trigger from
firing. If a reverse proxy is ever adopted later, it needs its own
security design (trusted-hop forwarded-for handling, backend bound to
loopback) — not a decision to make silently as a side effect of adding
TLS.

Acceptance baseline (Mira's 8 conditions, umwelt#688, folded into the
issue by Ariadne as hard requirements, not suggestions):

1. Explicit `ssl.SSLContext(PROTOCOL_TLS_SERVER)`, TLS 1.2 floor, maximum
   left open (Python/OpenSSL's own maintained default, not a bespoke
   cipher string that could fossilize or silently exclude TLS 1.3).
2. Fail-closed, comprehensively: both cert/key absent = intentional
   plaintext (today's unchanged default); both present = TLS; exactly
   one present, an unreadable file, a mismatched key, or an invalid/
   expired/not-yet-valid cert = a LOUD startup failure. No catch-and-
   retry plaintext path, ever.
3. Key hygiene: the private key must be a regular file and not group/
   world-readable, or startup refuses. Cert paths/config are not
   secrets; the key is — this module never logs key material, only
   paths and stat-derived facts.
4. `SESSION_COOKIE_SECURE` must be derived from the server's ACTUAL TLS
   mode, never a loosely related URL/config flag — this module's return
   value (`ssl.SSLContext | None`) IS that derivation; the caller (the
   gateway's `create_app`) threads a plain bool through app.config so a
   request handler can read "am I actually serving TLS" without
   re-deriving it. See `webrender/gateway.py`'s own `TLS_ENABLED` config
   key and its `set_cookie` call.
5. Renewal is an OPERATIONAL recipe (atomic file replace + controlled
   restart + a post-restart cert probe), not something this module can
   own — it only ever reads the cert/key at process startup, the same
   moment every other config value is read. Documented in
   `docs/DEPLOY.md`, not enforced in code here.
6. Certificate SAN must match the exact hostname clients use — this
   module has no config value naming "the hostname clients use", so it
   cannot enforce that at runtime; it is a deployment-time
   responsibility, documented in `docs/DEPLOY.md` alongside the
   acquisition recipe.
7. HSTS is explicitly OUT of scope for this slice (the cutover issue
   owns it, once every client is HTTPS-only and rollback semantics are
   understood) — nothing here sets or implies it.
8. Tested in `tests/test_tls_context.py`: minimum-protocol rejection,
   half-config refusal, bad/mismatched cert refusal, and (the same
   file's `test_live_listener_serves_the_configured_certificate`) that
   a live listener actually serves the configured certificate, plus
   `tests/test_library.py`'s end-to-end CLI-flag wiring tests and
   `tests/test_session_routes.py`'s cookie Secure-flag derivation tests.
"""
from __future__ import annotations

import os
import socket
import socketserver
import ssl
import sys
import threading
import types
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from werkzeug.serving import make_server

if TYPE_CHECKING:
    from flask import Flask

# umwelt#785: bounds ONLY the deferred per-connection TLS handshake (see
# `_lazy_finish_request`) -- never normal request serving. Ten seconds is
# generous for any real client's handshake (which completes in
# milliseconds even over a slow link) while still reclaiming a stalled
# peer's worker thread in bounded time rather than never. This is a
# POLICY choice -- generous headroom, not a measured minimum -- not a
# tuned threshold; don't read it as load-bearing precision if it's ever
# changed.
_TLS_HANDSHAKE_TIMEOUT_S = 10.0


class TLSConfigError(RuntimeError):
    """Raised on any fail-closed TLS configuration problem. The whole
    point of this being a distinct, loud exception is that a broken TLS
    setup crashes the process at startup — it never silently falls back
    to serving plaintext."""


def resolve_tls_context(
        cert_path: str | None, key_path: str | None) -> ssl.SSLContext | None:
    """Build a TLS server context from `--tls-cert`/`--tls-key`, or
    return `None` for plaintext mode (today's unchanged default).

    Fail-closed per umwelt#688's acceptance baseline (condition 2): any
    half-configured, unreadable, mismatched, or expired/not-yet-valid
    TLS setup raises `TLSConfigError` rather than silently degrading to
    plaintext. Blank strings are treated the same as `None` (an argparse
    default of `""` from an unset environment-sourced flag should not
    read as "configured").
    """
    cert_path = cert_path.strip() if cert_path else None
    key_path = key_path.strip() if key_path else None
    cert_present = bool(cert_path)
    key_present = bool(key_path)

    if not cert_present and not key_present:
        return None  # intentional plaintext -- unchanged from today

    if cert_present != key_present:
        missing = "--tls-key" if cert_present else "--tls-cert"
        given = "--tls-cert" if cert_present else "--tls-key"
        raise TLSConfigError(
            f"TLS half-configured: {given} was given but {missing} was "
            f"not. Both flags are required together for TLS, or both "
            f"must be absent for plaintext -- never one alone.")

    # Both present from here on -- validate before touching ssl.SSLContext
    # so every failure mode names itself precisely rather than surfacing
    # as an opaque ssl.SSLError.
    _check_key_hygiene(key_path)
    _check_cert_validity_window(cert_path)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Condition 1: explicit floor, maximum deliberately left at its
    # Python/OpenSSL-maintained default so TLS 1.3 is used where
    # available and this doesn't need updating as OpenSSL adds newer
    # protocol versions.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(cert_path, key_path)
    except (ssl.SSLError, OSError) as e:
        raise TLSConfigError(
            f"TLS cert/key failed to load ({cert_path!r}, {key_path!r}): "
            f"{e}. Refusing to start rather than silently downgrading to "
            f"plaintext.") from e

    return ctx


def _check_key_hygiene(key_path: str) -> None:
    """Condition 3: the private key must be a regular file, not group/
    world-readable. Never logs key material -- only the path and the
    stat-derived permission bits ever appear in an error message."""
    try:
        st = os.stat(key_path)
    except OSError as e:
        raise TLSConfigError(
            f"--tls-key {key_path!r} is not readable: {e}") from e
    if not os.path.isfile(key_path):
        raise TLSConfigError(
            f"--tls-key {key_path!r} is not a regular file.")
    mode = st.st_mode & 0o777
    if mode & 0o077:
        raise TLSConfigError(
            f"--tls-key {key_path!r} is group/world-readable (mode "
            f"{oct(mode)}) -- refusing to start with an exposed private "
            f"key. chmod 600 the key file.")


def _check_cert_validity_window(cert_path: str) -> None:
    """Condition 2's expiry half: an expired or not-yet-valid cert must
    refuse startup loudly. A browser would only warn on this, which is
    not loud enough for a fail-closed guarantee -- the server itself
    must not start serving on a cert outside its validity window."""
    try:
        with open(cert_path, "rb") as f:
            cert_bytes = f.read()
        cert = x509.load_pem_x509_certificate(cert_bytes, default_backend())
    except (OSError, ValueError) as e:
        raise TLSConfigError(
            f"--tls-cert {cert_path!r} could not be read/parsed: {e}") from e

    now = datetime.now(timezone.utc)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    if now < not_before:
        raise TLSConfigError(
            f"--tls-cert {cert_path!r} is not yet valid (starts "
            f"{not_before.isoformat()}, now is {now.isoformat()}).")
    if now > not_after:
        raise TLSConfigError(
            f"--tls-cert {cert_path!r} has expired (ended "
            f"{not_after.isoformat()}, now is {now.isoformat()}).")


def check_tls_args(ssl_context, tls_port: int | None, port: int) -> str | None:
    """Pure validation for the --tls-port/--tls-cert/--tls-key CLI
    arguments shared by both servers (`server.py`'s `main()` and
    `webrender/gateway.py`'s `main()`) -- factored out so it's unit-
    testable without invoking argparse or a real `resolve_tls_context`
    call. Returns an error message for the caller's own `p.error()`, or
    `None` if the arguments are consistent.

    Mira's security-lane finding on umwelt PR#720 (umwelt#699): server.py's
    original inline guard only checked the cert-present direction
    (ssl_context is not None). --tls-port given ALONE -- no --tls-cert/
    --tls-key, so ssl_context is None -- fell through this check entirely
    and reached the plaintext-only app.run() branch with no error: an
    operator's explicit TLS-cutover setting silently discarded, degrading
    a security-sensitive transport transition to plaintext-only serving.
    Both directions are checked here, and moved to this shared module
    (rather than left duplicated in server.py, and reimplemented in
    gateway.py) so the fix and its test coverage apply to both servers by
    construction, not by remembering to copy it a second time.
    """
    if ssl_context is not None:
        if tls_port is None:
            return "--tls-port is required when --tls-cert/--tls-key are given"
        if tls_port == port:
            return (f"--tls-port ({tls_port}) must differ from --port "
                     f"({port}) -- they are two separate listeners in "
                     "this one process, not a replacement of one by the other")
    elif tls_port is not None:
        return "--tls-cert and --tls-key are required when --tls-port is given"
    return None


def _lazy_get_request(self):
    """Rebound onto the TLS listener's server instance (see
    `serve_dual_listener`) to fix umwelt#785: werkzeug's own
    `BaseWSGIServer.__init__` SSL-wraps the LISTENING socket (`self.socket`
    is an `ssl.SSLSocket`), which means the unmodified
    `socketserver.TCPServer.get_request` -- `return self.socket.accept()`
    -- calls `ssl.SSLSocket.accept()`, and THAT performs the full TLS
    handshake inside `accept()` itself. `accept()` runs on the single
    foreground `serve_forever()` loop, before `ThreadingMixIn` has spawned
    any worker thread for the connection -- so a peer that completes the
    TCP connect and then stalls mid-handshake blocks every subsequent
    `accept()` indefinitely. `threaded=True` gives zero protection against
    this: no thread exists yet at the point the stall happens.

    The fix: bypass `ssl.SSLSocket.accept()`'s override entirely by
    calling the plain, unwrapped `socket.socket.accept` directly against
    `self.socket`. `socket.socket.accept()`'s own body builds its return
    value via the bare module-level `socket` class, not `self.__class__`
    -- so even though `self.socket` IS an `SSLSocket` instance, calling
    the base implementation on it returns an ordinary, un-negotiated
    `socket.socket`: no TLS has happened yet. The deferred handshake
    itself happens in `_lazy_finish_request`, which `ThreadingMixIn`
    machinery always runs in a freshly spawned per-connection worker
    thread -- never on this accept loop.
    """
    return socket.socket.accept(self.socket)


def _lazy_finish_request(self, request, client_address):
    """Rebound onto the TLS listener's server instance (see
    `serve_dual_listener`), paired with `_lazy_get_request`: performs the
    TLS handshake that function deferred, here in the worker thread
    `ThreadingMixIn.process_request` spawned for THIS connection -- never
    on the shared accept loop. A stalled or malicious peer at this point
    blocks only its own worker thread; every other peer keeps being
    accepted and served normally.

    This is also the fix's backstop half, not just the relocation:
    `ThreadingMixIn` spawns worker threads unboundedly, so moving the
    handshake off the accept loop ALONE would still let a peer that never
    completes it tie up its own thread forever -- a slow-loris-style pool
    of such peers would still exhaust threads over time. `settimeout`
    bounds the handshake to `_TLS_HANDSHAKE_TIMEOUT_S` and is cleared
    immediately afterward (`request.settimeout(None)`), so the bound
    never leaks into normal request serving -- a legitimately slow
    request body or an idle keep-alive connection is never subject to it.

    `self.ssl_context` is unaffected by any of this: it's set by
    werkzeug's own `BaseWSGIServer.__init__` at construction (from the
    LISTENING-socket wrap, untouched here) and is read by werkzeug itself
    to report `url_scheme="https"` and to classify SSL errors -- both
    keep working exactly as before.

    `finish_request` is not overridden anywhere between
    `socketserver.BaseServer` and `werkzeug.serving.ThreadedWSGIServer`
    (verified via `inspect`/`vars()` against the installed werkzeug), so
    calling `socketserver.BaseServer.finish_request` directly here -- once
    the (possibly now SSL-wrapped) `request` is ready -- is exactly the
    same call the unmodified server would have made.
    """
    if self.ssl_context is not None:
        request.settimeout(_TLS_HANDSHAKE_TIMEOUT_S)
        try:
            request = self.ssl_context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            # Handshake failed outright, or the bounded settimeout()
            # above fired. A socket timeout during the handshake raises
            # TimeoutError (empirically confirmed: repr is
            # "TimeoutError('_ssl.c:983: The handshake operation timed
            # out')") -- and TimeoutError IS socket.timeout
            # (`socket.timeout is TimeoutError` evaluates True on this
            # interpreter) and is an OSError subclass, so `except
            # OSError` alone already catches it -- it is caught because
            # of what it IS, not because it arrives disguised as
            # something else. Drop the connection quietly, exactly like
            # any other aborted TCP peer. Never let this crash the
            # worker thread or, more importantly, reach back to the
            # accept loop.
            #
            # This request.close() is a harmless NO-OP, not what actually
            # reclaims the fd -- ssl.SSLSocket._create() (which
            # wrap_socket() above calls into) does sock.detach() on the
            # raw socket BEFORE attempting the handshake, then wraps its
            # own construction in `except: self.close()`, so on ANY
            # failure (including this one) the real fd is already closed
            # by the SSLSocket itself by the time we get here -- `request`
            # is a detached, fd-less shell. Verified empirically
            # (colleague pass, umwelt#785): 20 concurrent stalled peers
            # produced +40 fds that dropped to +20 (the test's own client
            # sockets) within 3s of the handshake timeout, with no leak
            # after clients closed. Kept anyway as defense-in-depth for
            # any future refactor where `request` might not already be
            # detached at this point.
            try:
                request.close()
            except OSError:
                pass
            return
        # Handshake done -- release the bound immediately so it can
        # never leak into normal request serving. `settimeout(None)`
        # restores UNLIMITED BLOCKING, not "whatever the socket's timeout
        # was before this function touched it" -- there is no prior value
        # to restore to (a freshly accepted socket's timeout is whatever
        # the listening socket's was, which werkzeug never sets, so it's
        # already None). This is behaviourally identical to unmodified
        # werkzeug ONLY because nothing in this tree calls
        # `socket.setdefaulttimeout()` (confirmed: zero hits under src/)
        # -- if a process-wide default were ever introduced, plain
        # sockets elsewhere would pick it up too, but THIS socket would
        # still end up at unconditional None, diverging silently for TLS
        # connections only.
        request.settimeout(None)
    socketserver.BaseServer.finish_request(self, request, client_address)


def _validate_tls_handshake_timeout(value) -> None:
    """Fail LOUDLY and EARLY -- once, at server-construction time --
    rather than letting a bad `_TLS_HANDSHAKE_TIMEOUT_S` reach
    `_lazy_finish_request`'s per-connection `request.settimeout(...)`
    call. That call sits OUTSIDE the `try: ... except (ssl.SSLError,
    OSError)` block, and `socket.settimeout()` raises `ValueError` for a
    non-positive value -- `ValueError` is NOT an `OSError` subclass, so
    it would escape uncaught on every single accepted connection (not
    once at startup), each one printing a full traceback via
    `handle_error`: a log-flood on a public port. Unreachable today
    (`_TLS_HANDSHAKE_TIMEOUT_S` is a hardcoded `10.0`), reachable by one
    future edit to that constant -- this makes such an edit fail at
    import/construction instead of at the first connection in
    production.
    """
    if not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(
            f"_TLS_HANDSHAKE_TIMEOUT_S must be a positive number, got "
            f"{value!r} -- a non-positive value would reach "
            f"socket.settimeout() on every accepted TLS connection and "
            f"raise ValueError there instead, per-connection, forever.")


def _make_tls_server(host: str, port: int, app: "Flask", ssl_context):
    """`make_server(..., threaded=True, ssl_context=ssl_context)` PLUS the
    umwelt#785 lazy-handshake rebind, as one inseparable unit.

    This exists so a TLS server can never be obtained without the rebind:
    if the two rebind lines lived as loose statements in
    `serve_dual_listener`'s body, a future edit that adds a third
    listener, moves the construction, or refactors this function would
    silently reintroduce the accept-loop-blocking bug -- and every
    existing test would stay green, because none of them assert the
    rebind happened (they assert `make_server`'s call args, which are
    unaffected either way). Making the broken state unrepresentable
    (one call, always both halves) is stronger than a comment asking the
    next editor to remember.

    Also validates `_TLS_HANDSHAKE_TIMEOUT_S` before constructing
    anything -- see `_validate_tls_handshake_timeout`'s own docstring for
    why that has to happen here rather than per-connection.
    """
    _validate_tls_handshake_timeout(_TLS_HANDSHAKE_TIMEOUT_S)
    tls_server = make_server(host, port, app, threaded=True,
                              ssl_context=ssl_context)
    tls_server.get_request = types.MethodType(_lazy_get_request, tls_server)
    tls_server.finish_request = types.MethodType(
        _lazy_finish_request, tls_server)
    return tls_server


def serve_dual_listener(app: "Flask", host: str, plain_port: int,
                         tls_port: int, ssl_context) -> None:
    """Two listeners, ONE process (umwelt#699 cutover migration window):
    a plaintext werkzeug server on `plain_port` alongside a TLS one on
    `tls_port`, both against the SAME Flask app object. This is a
    lower-level equivalent of what `app.run()` already does internally
    (Flask's `run()` is itself a thin wrapper over
    `werkzeug.serving.make_server(...).serve_forever()`, with reloader/
    debugger scaffolding this deployment doesn't use) -- not a new
    server implementation, just two of the existing one.

    Shared between `server.py` (the world engine, port pair 5003/5006)
    and `webrender/gateway.py` (the render gateway, port pair 5011/5007)
    rather than duplicated -- both need the identical one-process/two-
    listener shape for the identical reason, and a fix or finding on one
    (see below) must not need a second, separate application to the other.

    ONE process rather than two is load-bearing, not a preference:
    Ariadne's measured finding (umwelt#699, 2026-08-23) is that a SECOND
    OS process against the live world hits three real hazards a second
    thread in this same process does not -- non-WAL db contention,
    bind_socket.py's unconditional unlink-before-bind orphaning the other
    process's socket, and per-process in-app.config rate limiters doubling
    an attacker's effective allowance. One process means one db writer,
    one identity-bind socket, one set of rate limiters -- each hazard
    doesn't get worked around, it doesn't exist. The reasoning applies
    identically to the gateway, which has its own db-adjacent state
    (session store) and its own rate limiter.

    The plaintext listener runs in a background daemon thread; the TLS
    listener runs in the foreground (blocking `serve_forever()`), so the
    process still behaves like a normal foreground service under
    systemd's `Type=simple` (SIGTERM kills the process, both listeners
    stop together -- the same shutdown semantics `app.run()` already
    has today, no new signal handling needed). Ending the migration
    window (umwelt#699 §4's plaintext-listener-stops step) is then a
    code path that stops feeding the plaintext thread, not a second
    deployment.

    BOTH listeners are `threaded=True` -- restoring parity with, not
    adding risk beyond, today's behavior: `make_server()` itself
    defaults `threaded=False`, but Flask's own `app.run()` (the
    ssl_context-is-None branch each caller leaves untouched) sets
    `threaded=True` via its own `options.setdefault`. Ariadne's
    colleague-pass finding (umwelt#699, 2026-08-23), measured not
    inferred: three concurrent requests against a 1s endpoint took
    3.03s (serial) without this, 1.00s (concurrent) with it -- every
    agent's 5s bell poll, every hook firing per prompt, and the
    webrender's 2s crossings poll all queue up behind one slow request
    (a large look_at, a tome read, a use that shells out) on a
    single-threaded listener, where today's threaded server only ever
    blocks itself.

    umwelt#785: the TLS listener's `get_request`/`finish_request` are
    rebound (via `types.MethodType`, onto this ONE instance, not a new
    server subclass) to `_lazy_get_request`/`_lazy_finish_request` --
    deferring the TLS handshake from the shared accept loop to each
    connection's own worker thread. See those two functions' docstrings
    for the full defect and fix. Rebinding onto the instance -- rather
    than instantiating a dedicated subclass in place of `make_server()`
    -- keeps `make_server()` the single construction call site both
    listeners share, unchanged in its arguments; the plaintext listener
    is completely untouched by this (only `tls_server` is rebound). The
    construction + both rebinds are one call, `_make_tls_server` -- see
    its own docstring for why that's a helper rather than three loose
    statements here.
    """
    plain_server = make_server(host, plain_port, app, threaded=True)
    tls_server = _make_tls_server(host, tls_port, app, ssl_context)
    plain_thread = threading.Thread(
        target=plain_server.serve_forever, name="plaintext-listener",
        daemon=True)
    plain_thread.start()
    print(f"umwelt: plaintext listener on {host}:{plain_port}, TLS "
          f"listener on {host}:{tls_port}", file=sys.stderr)
    try:
        tls_server.serve_forever()
    finally:
        # This coupling is also, by accident, a free liveness probe: the
        # plaintext listener only ever gets told to shut down AFTER
        # tls_server.serve_forever() has returned/raised -- so as long as
        # :5003 (or the gateway's plaintext port) still answers, the TLS
        # accept loop has NOT exited. That's what let Ariadne rule out
        # "the accept loop died outright" during the umwelt#785 live
        # outage without attaching a debugger -- the plaintext port was
        # still serving /health in 1.2ms while the TLS side was fully
        # wedged, which is a DIFFERENT failure mode (an accept loop stuck
        # on one peer's handshake, not a dead process). A refactor that
        # decouples these two listeners' lifecycles would remove this
        # diagnostic without anyone deciding to.
        plain_server.shutdown()
