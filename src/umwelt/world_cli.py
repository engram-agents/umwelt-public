#!/usr/bin/env python3
"""world CLI — the injected capability-channel client for Umwelt scrolls.

Usage (inside a `use` sandbox):
  world snapshot           # read bytes from stdin, store in CAS, print ref
  world retrieve <ref>     # retrieve bytes by ref, write to stdout

Socket path from $WORLD_CAP_SOCK (injected by the engine at sandbox launch).
This script is untrusted-convenience: the socket HANDLER is the boundary and
validates every frame — a scroll shadowing/replacing this CLI on its PATH is
self-harm, not a world-attack (Ariadne, docs/CAPABILITY-CHANNEL.md).

Stdlib only — no umwelt package dependency (this script runs inside the sandbox
and must not import the engine's code).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys


def _sock_path() -> str:
    p = os.environ.get("WORLD_CAP_SOCK", "")
    if not p:
        sys.exit("error: WORLD_CAP_SOCK not set — was this launched by the umwelt engine?")
    return p


def _transact(frame: dict) -> dict:
    """Send one line-JSON frame, read one line-JSON response."""
    path = _sock_path()
    data = (json.dumps(frame) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        try:
            s.connect(path)
        except OSError as exc:
            sys.exit(f"error: cannot connect to capability channel: {exc}")
        s.sendall(data)
        # Read until we have a newline (the response is a single line).
        resp = b""
        while b"\n" not in resp:
            chunk = s.recv(65536)
            if not chunk:
                break
            resp += chunk
    line = resp.split(b"\n")[0]
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        sys.exit(f"error: malformed response from capability channel: {resp[:200]!r}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: world snapshot|retrieve [ref]")

    cmd = sys.argv[1]

    if cmd == "snapshot":
        content = sys.stdin.buffer.read()
        b64 = base64.b64encode(content).decode()
        result = _transact({"op": "snapshot", "bytes": b64})
        if "error" in result:
            sys.exit(f"error: {result['error']}")
        print(result["ref"])

    elif cmd == "retrieve":
        if len(sys.argv) < 3:
            sys.exit("usage: world retrieve <ref>")
        ref = sys.argv[2]
        result = _transact({"op": "retrieve", "ref": ref})
        if "error" in result:
            sys.exit(f"error: {result['error']}")
        content_b64 = result.get("content")
        if content_b64 is None:
            sys.stderr.write("not found\n")
            sys.exit(1)
        sys.stdout.buffer.write(base64.b64decode(content_b64))

    else:
        sys.exit(f"unknown command: {cmd!r} — expected snapshot or retrieve")


if __name__ == "__main__":
    main()
