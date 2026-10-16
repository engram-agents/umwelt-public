"""umwelt#923 -- one app-agnostic WSGI request-body bound.

A WSGI middleware that bounds the raw request body of every WRITE request
(POST, PUT, PATCH, DELETE) BELOW the application's own hooks. It takes a
byte table keyed by (method, route) plus a global default as ARGUMENTS and
imports nothing from any particular app, so the world server and (later,
umwelt#1030) the webrender gateway can each adopt it with their own table.

It generalises, without re-deriving, the send_crossing bound umwelt#862 T5
(PR #924, five review rounds) built and documented. The framing rules below
are that wrapper's, verbatim in behaviour:

    The bound must sit BELOW every Flask hook: a before_request hook that
    calls request.get_json() on every write body (the world server's
    _reject_non_object_body) drains wsgi.input unbounded before any route
    runs, so a bound placed inside a route sees an already-drained stream.
    Here, by the time the app sees wsgi.input it is a bounded, fully-read
    BytesIO.

    - CONTENT_LENGTH present (a valid digit string) and over the limit: 413
      WITHOUT reading the body at all. The undrained body is NOT followed
      by a connection close: measured on Werkzeug 3.1.8 (the server
      app.run() uses), the 413 carries Werkzeug's own `Connection: close`
      header but no FIN arrives (none within 35 s). The refusal is safe for
      a different reason: bytes left on the connection are never parsed as
      a following request (a pipelined request behind an undrained 413 is
      not processed), and other connections are served normally (umwelt
      #1032 security read). A different WSGI container re-opens both
      questions -- re-measure before relying on either.
    - When both Content-Length and Transfer-Encoding are present, this layer
      follows Content-Length (RFC 9112 6.1 gives Transfer-Encoding
      precedence); safe here only because the layer rewrites both headers
      before the app sees them -- load-bearing if umwelt is ever fronted by
      another server.
    - CONTENT_LENGTH present and within the limit: read EXACTLY that many
      bytes. Reading limit + 1 against a shorter declared body blocks
      waiting for bytes that will never arrive (measured).
    - No valid CONTENT_LENGTH but `wsgi.input_terminated` is true (chunked;
      the server has already de-chunked and signals EOF itself): read
      limit + 1 bytes and refuse if that read came back longer.
    - Neither: treated as an empty body, never a read, so never a hang.
    - On pass: wsgi.input is replaced with the already-read bytes,
      CONTENT_LENGTH is corrected, any Transfer-Encoding header is dropped,
      and wsgi.input_terminated is reset to False -- downstream sees an
      ordinary, already-complete, Content-Length-declared body.

    Do NOT use request.max_content_length / MAX_CONTENT_LENGTH here
    (measured: on an over-limit CHUNKED body it silently truncates to the
    limit and the route still answers 201).

    The 413 is hand-built (no app/request context exists at this layer) in
    the house error shape {"error": "request body exceeds N bytes"}, N being
    the limit that APPLIED, and never echoes any part of the payload.

    CONTENT_LENGTH fails closed: only ASCII 0-9 matches (never
    str.isdigit(), which accepts e.g. "²" that int() then raises on); a
    present value failing that match gets an explicit 400 naming
    Content-Length, body unread, value never echoed. A present-but-EMPTY
    value ("") means "no length given" (WSGI/CGI servers commonly set it
    when no header was sent) and takes the same path as absent. Leading
    zeros are stripped (RFC 9110's 1*DIGIT allows them) and the
    significant-digit COUNT is checked against the limit's own digit count
    BEFORE int() is ever called, so an attacker-controlled digit string
    never reaches Python's int-string conversion limit.

Route resolution is the caller's: `resolve_route(environ)` returns the key
to look rows up under (for a Flask app, the matched url_map RULE string, so
a parametrized rule maps to its rule rather than to a raw path) or None when
nothing matches; an unmatched write request still gets `default_limit`.

Unbounded is an EXPLICIT opt-in. `default_limit` must be a positive int
(an unlisted write route is bounded, never unbounded); `default_limit=None`
("unlisted routes pass through untouched") raises ValueError unless
`send_only_compat=True` is also passed. That flag exists only so the
single-row send_crossing compatibility wrapper can reuse this layer; a new
adopter (the webrender gateway, umwelt#1030) should never pass it.

The request method is normalised with .upper() once, before the row lookup:
Flask upper-cases request.method, so a lowercase `post` reaches the same
routes, and a case-sensitive comparison here would wave it past the bound
while the application's own hooks read the body whole.
"""
from __future__ import annotations

import io
import json
import re
from typing import Callable, Mapping, NamedTuple

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# ASCII-only digit match -- deliberately NOT str.isdigit() (see above).
_CONTENT_LENGTH_DIGITS_RE = re.compile(r"[0-9]+")


class BodyRow(NamedTuple):
    """One table row: the byte limit for a (method, route), plus the names
    of any fields on that route that have NO server-side cap of their own.
    A non-empty `uncapped_field` marks a row whose limit is a ceiling
    standing in for the missing field cap; it is data, not a comment, so a
    later field cap cannot land as a silent edit to one row without the
    marker moving."""

    limit: int
    uncapped_field: tuple[str, ...] = ()


def _json_response(start_response, status: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    start_response(status, [("Content-Type", "application/json"),
                            ("Content-Length", str(len(body)))])
    return [body]


def bound_write_bodies(
    wsgi_app,
    *,
    rows: Mapping[tuple[str, str], BodyRow],
    default_limit: int | None,
    resolve_route: Callable[[dict], str | None],
    send_only_compat: bool = False,
):
    """Wrap `wsgi_app` so every write request body is bounded. See the module
    docstring for the framing contract. Raises ValueError for a missing or
    invalid `default_limit` (None needs send_only_compat=True)."""
    if default_limit is None:
        if not send_only_compat:
            raise ValueError(
                "default_limit=None leaves unlisted write routes unbounded; "
                "pass a positive int, or send_only_compat=True to opt in")
    elif (isinstance(default_limit, bool) or not isinstance(default_limit, int)
            or default_limit <= 0):
        raise ValueError(
            f"default_limit must be a positive int, got {default_limit!r}")

    def _refuse(start_response, limit: int):
        return _json_response(
            start_response, "413 REQUEST ENTITY TOO LARGE",
            {"error": f"request body exceeds {limit} bytes"})

    def _refuse_bad_length(start_response):
        return _json_response(
            start_response, "400 BAD REQUEST",
            {"error": "Content-Length must be a non-negative integer"})

    def _bounded_wsgi_app(environ, start_response):
        method = (environ.get("REQUEST_METHOD") or "").upper()
        if method not in WRITE_METHODS:
            return wsgi_app(environ, start_response)
        try:
            route = resolve_route(environ)
        except Exception:  # resolution must never be a way around the bound
            route = None
        row = rows.get((method, route)) if route is not None else None
        limit = row.limit if row is not None else default_limit
        if limit is None:
            return wsgi_app(environ, start_response)
        limit_digits = len(str(limit))

        content_length = environ.get("CONTENT_LENGTH")
        if content_length:
            if not _CONTENT_LENGTH_DIGITS_RE.fullmatch(content_length):
                return _refuse_bad_length(start_response)
            # Strip leading zeros before every check below -- the raw digit
            # COUNT is not the value's significance. Never call int() on
            # the raw string.
            significant = content_length.lstrip("0") or "0"
            if len(significant) > limit_digits:
                # More digits than the limit could ever need: over-limit,
                # WITHOUT calling int() on an attacker-controlled string.
                return _refuse(start_response, limit)
            if int(significant) > limit:
                return _refuse(start_response, limit)
            # Read EXACTLY the declared length -- one byte past a
            # correctly-declared SHORTER body blocks forever.
            data = environ["wsgi.input"].read(int(significant))
        elif environ.get("wsgi.input_terminated"):
            # Chunked: the server de-chunked and signals EOF itself, so
            # reading one byte past the bound is safe.
            data = environ["wsgi.input"].read(limit + 1)
        else:
            data = b""
        if len(data) > limit:
            return _refuse(start_response, limit)
        environ["wsgi.input"] = io.BytesIO(data)
        environ["CONTENT_LENGTH"] = str(len(data))
        environ.pop("HTTP_TRANSFER_ENCODING", None)
        environ["wsgi.input_terminated"] = False
        return wsgi_app(environ, start_response)

    return _bounded_wsgi_app
