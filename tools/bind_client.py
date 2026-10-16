#!/usr/bin/env python3
"""Identity-bind enrollment client -- umwelt#495 slice 2's answer to
Lei's own question: "the credential is now blocked by my sudo runs to
backfill the passwords? How do I do that?" It wasn't answerable before
this file existed: the bind socket (bind_socket.py) has no client, only
a wire protocol (JSON-lines over AF_UNIX) and a test suite that talks to
it directly.

WHAT THIS IS, AND ISN'T
------------------------
A thing a human with root can run that performs ONE bind and reports
clearly what happened. Not a CLI suite, not a management console, not a
substitute for the (not-yet-built) slice 3 login-verification path this
writes a credential FOR. If a second op ever needs a client, that is a
new tool, not a flag added to this one -- see bind_socket.py's own
docstring for why the socket itself deliberately supports only `bind`.

Enrollment tooling, not deploy-gated the way the listener is: nothing
here runs as part of the world server, and it never touches `:5003` or
`:5005` except through the bind socket's own front door, the same as
any other caller.

CREDENTIAL SCHEME (humans only -- see below)
---------------------------------------------
Password, hashed with the stdlib's `hashlib.scrypt` (no third-party
dependency). PROVENANCE, checked rather than assumed (Borges's question
on PR #517's security-lane read, traced against the design-of-record
document rather than relayed): "password for humans" IS Lei's own
verbatim ruling -- "just a password for now is good enough" -- but
"scrypt" and "stdlib" specifically are the design document's own
engineering choice under Borges's lead, not words of Lei's anywhere
found. Both are good calls; only one is Lei's to revisit unilaterally.
Filed as the design document's engineering judgment, not his ruling, so
a future reader doesn't treat the hash algorithm as more settled than it
is. The verifier written to `identities.credential_verifier` is self-
describing -- scheme name plus every parameter needed to re-derive the
hash at login time -- so slice 3 doesn't have to guess or hardcode cost
parameters this script chose:

    {"scheme": "scrypt", "hash": <base64>, "salt": <base64>,
     "n": 16384, "r": 8, "p": 1, "dklen": 32}

`n=2**14, r=8, p=1` is a standard interactive-login cost (OWASP's
current scrypt baseline) -- expensive enough to resist offline guessing,
cheap enough that this script and a future login check both return in a
fraction of a second on ordinary hardware. `dklen` (derived key length)
travels too -- Borges's find on #517's review: it's a real scrypt
parameter (changing it changes the output, same as n/r/p), and the
"every parameter travels" claim above was false while it was hardcoded
at the call site instead of in the verifier.

Password is read via `getpass` (terminal echo off) and typed twice --
never a CLI argument (shell history, `ps`, and shared-terminal scrollback
would all leak it) and never accepted from a single, unconfirmed entry
(this SETS the credential a human will later have to type correctly; a
silent typo here is a lockout with no error until they try to log in).

AGENT IDENTITIES: root-assisted enrollment (umwelt#606)
-------------------------------------------------------
`--kind agent --public-key <base64>` enrolls an ed25519 PUBLIC key on
behalf of an agent that cannot self-bind. Self-bind
(`bind_client_agent.py`) authorizes over `SO_PEERCRED` on a local
`AF_UNIX` socket, which is local-only *by construction* -- so an agent
with no OS account on the world host can never use it. Measured
2026-08-18: three of nine agent identities are in exactly that position.

Two things make this safe rather than a hole in the keypair story:

* **A public key is not a secret.** The remote agent generates its own
  keypair (`<checkout>/tools/bind_client_agent.py --generate-only`), keeps the private
  half, and sends only the public half -- the same shape as adding an
  SSH key to a hosting account. Whoever runs this script never sees, and
  never needs, anyone's private key.
* **Authorization is unchanged.** This script is just another client of
  `bind_socket`'s existing gate: root may bind any identity, a non-root
  caller may bind only its own. `_op_bind` already accepts
  `kind="agent"` with an opaque `credential_verifier`; nothing
  server-side is loosened here. See `authorized_bound_by`.

It is also the ONLY post-window recovery path. `bind_socket` contains no
reference to the backfill window, so root enrollment keeps working after
the window closes -- and after close, an unbound identity cannot log in
at all, so an agent that loses its private key would otherwise be locked
out permanently.

**The public key is validated before anything is sent** -- strict base64,
exactly 32 raw bytes, and accepted by the same `Ed25519PublicKey`
constructor the login path itself uses. A malformed key would bind an
identity nobody can ever authenticate as: the same permanent-loss
failure this path exists to prevent, arriving through the cure.

SAFETY
------
* Reads (never guesses) the socket path -- default matches the world
  server's own `--bind-socket-path` default
  (`/usr/local/lib/umwelt/run/identity-bind.sock`), overridable for a
  non-standard deployment or a dry run against a test listener.
* Requires an explicit `--identity` -- no default target, so a slipped
  Enter key can't bind the wrong person's credential.
* Confirms once, naming the exact identity and kind, before sending
  anything -- bind and reset are the SAME server operation
  (bind_identity's own docstring), so this may be overwriting a working
  credential and says so.
* The password itself never appears in this script's own output, and
  the hash/salt are the only credential-derived bytes that leave the
  process (over the socket, to the server that already gates writes to
  `identities` behind SO_PEERCRED authorization -- unchanged by this
  tool, which is just another client of that same gate).

USAGE
-----
    sudo python3 <checkout>/tools/bind_client.py --identity lei

    # enroll a remote agent's public key on its behalf (umwelt#606):
    sudo python3 <checkout>/tools/bind_client.py --identity luria --kind agent \\
        --public-key 3dRHtnonwM3/wTHNJa0BVfAfsAhKlbp2MTPMw+edPIU=

    # against a non-default socket (e.g. a test listener):
    sudo python3 <checkout>/tools/bind_client.py --identity lei \\
        --socket-path /tmp/some/test/identity-bind.sock
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import pwd
import socket
import sys

# Mirrors server.py's _DEFAULT_BIND_SOCKET_PATH -- not imported, because
# importing server.py here would pull in Flask and the rest of the world
# app for a client that only ever needs to open a socket. Kept in sync by
# eye; if the two drift, --socket-path is the escape hatch.
_DEFAULT_SOCKET_PATH = "/usr/local/lib/umwelt/run/identity-bind.sock"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _transact(sock_path: str, frame: dict, timeout: float = 5.0) -> dict:
    """One line-JSON frame in, one response out -- same shape as
    bind_socket.py's test suite's own `_transact`, deliberately lifted
    rather than reinvented so this client and the tests that already
    exercise the wire protocol agree on what "one transaction" means."""
    data = (json.dumps(frame) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(data)
        resp = b""
        while b"\n" not in resp:
            chunk = s.recv(65536)
            if not chunk:
                break
            resp += chunk
    if not resp:
        raise RuntimeError(
            "socket closed with no response -- the server may have "
            "crashed mid-request, or this isn't actually the bind "
            "socket at that path")
    return json.loads(resp.split(b"\n")[0])


def _read_password_twice() -> str:
    """getpass twice, refuse a mismatch rather than silently taking the
    first entry -- this SETS the credential, so a typo here is a
    lockout with no symptom until the human tries to log in with the
    password they actually meant to set."""
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("Passwords did not match. Nothing was sent. Re-run to try again.")
        sys.exit(1)
    if not first:
        print("Empty password refused. Nothing was sent.")
        sys.exit(1)
    return first


_ED25519_RAW_PUBLIC_KEY_LEN = 32


def _ed25519_verifier(public_key_b64: str) -> dict:
    """Validate a caller-supplied ed25519 PUBLIC key and build the
    `credential_verifier` the login path expects (umwelt#606).

    Shape is inherited, not invented -- `{"scheme": "ed25519",
    "public_key": <base64 of raw public key bytes>}`, identical to what
    `bind_client_agent.py` sends on the self-bind path and to what
    `server.py`'s login branch reads back.

    Three checks, all BEFORE anything is sent, because the failure they
    prevent is unrecoverable rather than merely annoying: a malformed
    key binds an identity nobody can ever authenticate as, and after the
    backfill window closes that identity cannot log in at all.

    1. **Strict base64.** `validate=True` is load-bearing: without it
       `b64decode` SILENTLY DISCARDS characters outside the alphabet, so
       a key mangled in transit (a stray space, a line break, a pasted
       ellipsis) can still decode -- to the wrong bytes, or to the right
       length by luck.
    2. **Exactly 32 raw bytes.** An ed25519 public key is a fixed 32
       bytes; anything else is not one. Checked explicitly rather than
       left to the constructor so the error names the actual problem.
    3. **The real constructor accepts it.** `Ed25519PublicKey.
       from_public_bytes` is the same call `server.py` makes at login,
       so passing here means the login path can at least parse what we
       are about to store. This is the practice check, not a redundant
       one -- (1) and (2) reason about the bytes, this one exercises the
       consumer.

    `cryptography` is imported INSIDE this function on purpose. The
    human/scrypt path above is stdlib-only and works on a host that has
    never installed this project's requirements; a module-level import
    would make an agent-only dependency break password enrollment too.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except ValueError as exc:
        # `binascii.Error` is what b64decode actually raises here, and it is
        # a ValueError SUBCLASS -- checked on this interpreter rather than
        # assumed, after review flagged the original
        # `except (ValueError, binascii.Error)` as redundant. It was.
        # Catching the base class alone is complete.
        raise ValueError(
            f"--public-key is not valid base64 ({exc}). Paste the key "
            f"exactly as the agent printed it, with no surrounding "
            f"quotes, spaces, or line breaks.") from exc

    if len(raw) != _ED25519_RAW_PUBLIC_KEY_LEN:
        raise ValueError(
            f"--public-key decoded to {len(raw)} bytes, but an ed25519 "
            f"public key is exactly {_ED25519_RAW_PUBLIC_KEY_LEN}. This "
            f"is usually a truncated paste, or a key in PEM/OpenSSH "
            f"form rather than raw base64.")

    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise ValueError(
            f"--public-key is 32 bytes but is not a usable ed25519 "
            f"public key ({exc}).") from exc

    return {
        "scheme": "ed25519",
        "public_key": base64.b64encode(raw).decode("ascii"),
    }


def _hash_password(password: str) -> dict:
    """scrypt, stdlib, self-describing verifier -- see module docstring's
    'CREDENTIAL SCHEME' section for why every parameter travels with the
    hash rather than being assumed at verification time."""
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return {
        "scheme": "scrypt",
        "hash": base64.b64encode(derived).decode("ascii"),
        "salt": base64.b64encode(salt).decode("ascii"),
        "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
        "dklen": _SCRYPT_DKLEN,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/bind_client.py",
        description="Bind (or reset) an identity's credential over "
                     "umwelt's identity-bind socket. Requires root for "
                     "any identity other than your own agent account -- "
                     "see bind_socket.py's authorized_bound_by.",
    )
    parser.add_argument(
        "--identity", required=True,
        help="the identity to bind (e.g. lei) -- required, no default, "
             "so a slipped Enter can't bind the wrong person")
    parser.add_argument(
        "--kind", default="human", choices=["human", "agent"],
        help="identity kind. 'human' (default) sets a password "
             "interactively. 'agent' enrolls an ed25519 public key "
             "supplied via --public-key, for an agent that cannot "
             "self-bind because it has no OS account on this host "
             "(umwelt#606) -- see module docstring")
    parser.add_argument(
        "--public-key", default=None,
        help="base64 of a raw ed25519 PUBLIC key, as printed by "
             "`<checkout>/tools/bind_client_agent.py --generate-only` on the agent's own "
             "host. Required with --kind agent, refused with --kind "
             "human. A public key is not a secret; the agent keeps its "
             "private half and it never travels")
    parser.add_argument(
        "--socket-path", default=_DEFAULT_SOCKET_PATH,
        help=f"path to the identity-bind socket (default: "
             f"{_DEFAULT_SOCKET_PATH}, matching the world server's own "
             f"--bind-socket-path default)")
    args = parser.parse_args(argv)

    # Refuse the two incoherent combinations LOUDLY and before anything
    # else happens. Neither is a typo a user would want silently
    # reinterpreted: "--kind agent" with no key would otherwise fall
    # through to a password prompt and write a scrypt verifier under an
    # ed25519 identity, and "--kind human --public-key" would silently
    # ignore a key the operator clearly meant to enroll.
    if args.kind == "agent" and not args.public_key:
        parser.error(
            "--kind agent requires --public-key (base64 of a raw ed25519 "
            "public key, as printed by `bind_client_agent.py "
            "--generate-only` on the agent's own host)")
    if args.kind == "human" and args.public_key:
        parser.error(
            "--public-key is only valid with --kind agent; the human "
            "path sets a password interactively and has no key to enroll")

    uid = os.getuid()
    try:
        account = pwd.getpwuid(uid).pw_name
    except KeyError:
        account = f"uid={uid}"
    if uid != 0:
        print(f"NOTE: running as {account!r} (uid {uid}), not root. "
              f"Binding an identity other than your own agent account "
              f"will be refused as 'unauthorized' -- that is the "
              f"authorization predicate working correctly, not a bug in "
              f"this script. Re-run under sudo to bind a human identity, "
              f"or to enroll a public key for an agent that cannot "
              f"self-bind.\n")

    # Validate + display the agent key BEFORE the confirmation prompt, not
    # after. The prompt asks "are you sure you want to bind THIS" -- an
    # operator cannot answer that about a key they have not been shown, and
    # a validation failure discovered afterwards would have already spent
    # their confirmation. (Caught by running the tool rather than reading
    # it: the first version printed the fingerprint after the prompt.)
    verifier = None
    if args.kind == "agent":
        # No password prompt on this path: there is no secret to type.
        # The agent generated its own keypair and kept the private half;
        # all that travels is the public one, already on the command line.
        try:
            verifier = _ed25519_verifier(args.public_key)
        except ValueError as exc:
            print(f"FAIL: {exc}")
            print("Nothing was sent.")
            return 1
        # Echo the key back and fingerprint it, so the operator can compare
        # against what the agent actually sent them. A base64 blob is
        # exactly the kind of string a human eye slides over; a short
        # fingerprint is comparable out-of-band (over a call, in a letter)
        # in a way the full key is not.
        fingerprint = hashlib.sha256(
            base64.b64decode(verifier["public_key"])).hexdigest()[:16]
        print(f"public key : {verifier['public_key']}")
        print(f"fingerprint: SHA256:{fingerprint}")
        print("Check this fingerprint against what the agent sent you -- "
              "enrolling the wrong key binds an identity nobody can "
              "authenticate as.")

    print(f"About to bind identity {args.identity!r} (kind={args.kind!r}) "
          f"at socket {args.socket_path!r}.")
    print("If this identity already has a credential, THIS WILL REPLACE IT "
          "-- bind and reset are the same operation.")
    # Deliberately no !r here (unlike the descriptive line above): !r wraps
    # the name in quotes, so a human who types back exactly what's on
    # screen ('lei') gets refused, since the bare comparison below wants
    # lei without them. Ariadne's #517 review finding, caught by running
    # the tool rather than reading it.
    confirm = input(f"Type the identity name ({args.identity}) to "
                     f"confirm: ")
    if confirm != args.identity:
        print("Confirmation did not match. Nothing was sent.")
        return 1

    if args.kind == "human":
        password = _read_password_twice()
        verifier = _hash_password(password)
        # Drops the last reference to the plaintext. CPython's str is
        # immutable, so this cannot zero the underlying bytes -- it narrows
        # the window before the allocator reuses them, it doesn't close it.
        # (Wording fixed per Ariadne's #517 review: "scrubbed" overstated what
        # `del` actually does.)
        del password

    try:
        resp = _transact(args.socket_path, {
            "op": "bind",
            "agent_id": args.identity,
            "kind": args.kind,
            "credential_verifier": verifier,
        })
    except FileNotFoundError:
        print(f"FAIL: no socket at {args.socket_path!r}. Is the world "
              f"server running, and is that the right --socket-path?")
        return 2
    except (ConnectionRefusedError, PermissionError) as exc:
        print(f"FAIL: could not connect to {args.socket_path!r}: {exc}\n"
              f"If this is a permission error, your account may not be "
              f"in the socket's OS group -- see bind_socket.py's "
              f"'Socket file mode' docstring section.")
        return 2
    except (TimeoutError, ConnectionResetError, BrokenPipeError) as exc:
        # Same-host operator tool, but a hung or crashing listener
        # shouldn't hand a human a raw traceback -- every other failure
        # here exits with a clean FAIL: line (reviewer-fairy's #517
        # suggestion).
        print(f"FAIL: transaction with {args.socket_path!r} did not "
              f"complete: {exc}")
        return 2
    except json.JSONDecodeError as exc:
        print(f"FAIL: server response at {args.socket_path!r} was not "
              f"valid JSON ({exc}) -- is this really the identity-bind "
              f"socket, and is it running a compatible version?")
        return 2

    if resp.get("status") != "ok":
        print(f"FAIL: {resp.get('error', resp)}")
        return 1

    print(f"OK -- bound {resp.get('agent_id')!r}: "
          f"bound_at={resp.get('bound_at')!r} "
          f"bound_by={resp.get('bound_by')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
