"""The engine API number and the client/server header pair (umwelt#1105).

``ENGINE_API`` is the version of the contract between the world server and
every client of it (the MCP server, the hooks, the bell, ``uw``, the gateway,
...). A client sends the number it was BUILT against in ``X-Umwelt-Client-API``
on every request; the server refuses (HTTP 409) any non-exempt request whose
number is absent, malformed, or not equal to its own. The server also stamps
``X-Umwelt-Engine-API`` on every response produced inside the app (errors and
404s included). The refusals the ingress wrapper hand-builds (413 body too
large, 400 bad Content-Length) are produced before the app and do not carry
it, so an oversized body from a stale client is refused 413, not 409. See
PROTOCOL.md "Engine API gate".

This module is stdlib-only and is vendored byte-identically into
``plugin/umwelt/engine_api.py`` by ``tools/build-umwelt-plugin.sh``: the
clients import it, so it must never grow a dependency on the server.

Bump ``ENGINE_API`` whenever a route, a method, or a kinddef the clients rely
on changes; ``tools/gen_api_manifest.py`` regenerates ``api_manifest.json`` and
the test suite requires the matching ``api_history.json`` entry.
"""
from __future__ import annotations

import json
import re

ENGINE_API: int = 2

CLIENT_HEADER = "X-Umwelt-Client-API"
ENGINE_HEADER = "X-Umwelt-Engine-API"

REMEDY = (
    "client is older: rebuild the plugin "
    "(tools/install-local-marketplace-umwelt.sh, then /reload-plugins) "
    "· server is older: promote the server to the current deploy tag"
)

# The canonical integer: "0", or a non-zero leading digit and at most 8 more,
# ASCII digits only. fullmatch + [0-9] (never \d, never int()) so a sign, a
# space, a leading zero, a non-ASCII digit, or a 5,000-digit string is simply
# "not a number" -- and is never handed to int(), which raises ValueError past
# 4,300 digits.
_CANONICAL_INT = re.compile(r"0|[1-9][0-9]{0,8}")


def parse_client_api(value: str | None) -> int | None:
    """The header value as an int, or None when it is not canonical."""
    if value is None or _CANONICAL_INT.fullmatch(value) is None:
        return None
    return int(value)


def client_headers() -> dict[str, str]:
    """The header every client of the world server must send."""
    return {CLIENT_HEADER: str(ENGINE_API)}


def render_mismatch(body: dict) -> str:
    """One line for a gate refusal body, or for a login ``engine_api_notice``.

    ``⚠️ engine API mismatch (client N, server M): <remedy>`` -- ``none`` when
    the server could not read the client's number."""
    client = body.get("client_api")
    return (f"⚠️ engine API mismatch (client {'none' if client is None else client}, "
            f"server {body.get('engine_api')}): {body.get('remedy')}")


G1_KEYS = ("error", "client_api", "engine_api", "remedy")


def is_mismatch_refusal(status: int, body: object) -> bool:
    """True for the gate's own 409: status 409 and a JSON object carrying ALL
    FOUR body keys. A route's own 409 (e.g. ``{"error": "name taken"}``) is not."""
    return (status == 409 and isinstance(body, dict)
            and all(k in body for k in G1_KEYS))


def refusal_from_http_error(err: object) -> dict | None:
    """The gate's 409 body from an ``urllib.error.HTTPError``; None for any
    other error (a 401, a 500, a route's own 409, a non-JSON body). Never
    raises: the hooks that call it must never block a prompt."""
    try:
        if getattr(err, "code", 0) != 409:
            return None          # only the gate's status; leave other bodies unread
        body = json.loads(err.read().decode())  # type: ignore[attr-defined]
        if is_mismatch_refusal(409, body):
            return body
    except Exception:
        pass
    return None


def mismatch_line_from_http_error(err: object) -> str | None:
    """:func:`render_mismatch` of :func:`refusal_from_http_error`, or None."""
    body = refusal_from_http_error(err)
    return render_mismatch(body) if body is not None else None
