#!/usr/bin/env python3
"""Agent self-bind enrollment client -- umwelt#495 slice 2's answer to the
agent-side half of "how do I get a credential bound?" `tools/bind_client.py`
is the sibling tool for the HUMAN path (sudo + password/scrypt); this script
is its agent counterpart, and the two are deliberately NOT the same tool with
a flag -- bind_client.py explicitly refuses `--kind agent` (see its own
docstring) precisely because the agent path has a different credential
shape, a different authorization story (no root needed), and a different
identity-derivation story (never typed, always the caller's own account).

WHAT THIS IS, AND ISN'T
------------------------
A thing an agent runs, as itself, over its own OS account, that generates a
fresh ed25519 keypair, binds the public half over the identity-bind socket
(`bind_socket.py`), and persists the private half locally.

`--generate-only` (umwelt#606) is the same tool with the socket step
removed: generate, persist the private half, print the public half, stop.
It exists for agents on a REMOTE host, who cannot self-bind at all --
the socket authorizes over `SO_PEERCRED` and is local-only by
construction -- so their public key is enrolled for them by root on the
world host via `bind_client.py --kind agent`. Documented in full at that
flag's own `--help`; named here because this section is the highest
altitude anyone reads. Not a CLI suite,
not a management console, not a substitute for the (not-yet-built) slice 3
login-time client integration that will eventually READ this key back to
sign a login challenge -- see "SAFETY" below for exactly what that means in
practice today. If a second op ever needs a client, that is a new tool, not
a flag added to this one -- see bind_socket.py's own docstring for why the
socket itself deliberately supports only `bind`.

Enrollment tooling, not deploy-gated the way the listener is: nothing here
runs as part of the world server, and it never touches `:5003` or `:5005`
except through the bind socket's own front door, the same as any other
caller.

CREDENTIAL SCHEME (agents only -- see bind_client.py for humans)
-------------------------------------------------------------------
ed25519, via the `cryptography` package (already a project dependency --
see requirements.txt's own comment citing umwelt#495 slice 3). The
credential_verifier SHAPE below is NOT this script's own invention -- it is
inherited, unchanged, from the already-implemented and already-tested
slice-3 login flow: the internal login test's (not in this release)
`_make_ed25519_keypair` (~line 71) builds and round-trips exactly this
shape against the real HTTP login challenge/response path. This script
generates the keypair and sends the verifier half; it does not define what
the verifier looks like.

    {"scheme": "ed25519", "public_key": <base64 of raw public key bytes>}

The public key bytes are raw (`Encoding.Raw, PublicFormat.Raw`), not a PEM
or DER wrapper -- matching test_agent_login.py exactly, so a key bound by
this script verifies against the same code path that test already proves
works end-to-end.

Why no root, no `--kind`, no `--identity` (unlike bind_client.py)
-----------------------------------------------------------------
This script always runs as the caller's own OS account, and the bind
socket's authorization predicate (`bind_socket.authorized_bound_by`) can
only ever succeed for a non-root caller binding ITS OWN identity -- a
non-root caller whose account name is `agent-X` may bind X and nothing
else (Kepler's ruling, see bind_socket.py's own docstring). So there is no
`--kind` flag (always "agent", always self) and no `--identity` flag: since
the server can only ever authorize the caller's own identity anyway,
letting a human type a name here would only create a way to typo into
attempting someone else's identity for no benefit -- the identity is
derived from `pwd.getpwuid(os.getuid()).pw_name` instead (see
`_derive_agent_id`), stripping the `agent-` prefix. Running this as any
account NOT shaped `agent-<name>` is refused outright with a clear message,
rather than guessed at -- see `_derive_agent_id`.

SAFETY
------
* No root, no sudo, ever -- if you need to bind/reset an identity as
  someone OTHER than yourself (e.g. recovering an agent account you don't
  currently have shell access to), that is bind_client.py's `sudo` path,
  not this script's job.
* The identity bound is always derived from the caller's own OS account,
  never typed -- see "Why no root, no --kind, no --identity" above.
* The PRIVATE key is written to `_DEFAULT_KEY_PATH` (default
  `~/.umwelt/identity_key`, expanduser'd) via stage-in-the-same-directory
  then atomically replace -- never by opening the final path for writing
  directly. This means a crash, short write, or disk-full error at any
  point before the final swap leaves whatever was already at that path
  (a prior key, or nothing) completely untouched -- never partial or
  empty. The final path is also refused if anything already occupies it
  in a way this function didn't just create (a symlink included, checked
  via `lstat` rather than `Path.exists()`, since `.exists()` follows
  symlinks and returns False for a dangling one) -- see
  `_write_private_key`'s and `_path_occupied`'s docstrings for the full
  reasoning (Mira's #601 security-lane review named both gaps in an
  earlier, simpler open()-with-O_TRUNC-in-place version; this is the
  fix). **This location is a PLACEHOLDER, not yet a ratified team
  convention** -- proposed by Ariadne during umwelt#495 step-3 readiness
  discussion, 2026-08-16, and currently routed to the login-design seat for
  confirmation in parallel with this tool's build. Precisely because it
  isn't settled, the path lives in exactly ONE place in this file --
  the `_DEFAULT_KEY_PATH` module constant -- and is also overridable at
  the CLI via `--key-path`, so if the design call lands somewhere else
  this becomes a one-line constant change (or a documented override),
  never a search-and-replace across the file. Flag in review if a
  different location is preferred before this merges.
* **No code in this repository yet READS `~/.umwelt/identity_key`.** This
  script only WRITES it. The slice-3 login-time client integration that
  will eventually load this file and sign a login challenge with it is
  separate, upstream, unbuilt work -- do not assume the loop between "a key
  is bound" and "an agent logs in with it" is closed just because this
  script exists. It isn't, yet.
* If the key file already exists, this is treated as a RESET (bind and
  reset are the SAME server operation -- bind_socket.py's own docstring:
  "reset reuses [`bind`]"). Overwriting it invalidates the OLD public key
  server-side (the server only ever stores the most recent verifier) and
  destroys the only copy of the old private key locally. This script warns
  about BOTH consequences explicitly and requires typing `yes` (not merely
  pressing Enter) before generating a new keypair or touching the file --
  same confirm-before-destructive-action posture as bind_client.py, adapted
  since there is no identity name left to type back (see above).
* **The private key material is NEVER printed, echoed, or logged, in full
  or in part, at any point -- not even for a "here's your key, save it"
  confirmation to the human/agent running this.** That flow is deliberately
  wrong for this tool: the key goes from generation directly to the
  mode-0600 file and nothing else ever reads it in this process. The
  PUBLIC key MAY be printed (e.g. "bound public key: <base64>") purely for
  the operator's own confidence-check that a bind actually happened; it is
  not sensitive.

USAGE
-----
    python3 tools/bind_client_agent.py

    # against a non-default socket (e.g. a test listener):
    python3 tools/bind_client_agent.py \\
        --socket-path /tmp/some/test/identity-bind.sock

    # against a non-default local key path (e.g. if the placeholder
    # location above is superseded before you've re-run this):
    python3 tools/bind_client_agent.py \\
        --key-path /tmp/some/test/identity_key
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pwd
import socket
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

# The default identity-key path is shared with the reader (client_auth.py)
# via umwelt._ids, the single source of truth for it -- so the writer and the
# reader cannot drift on where the key lives. _ids is deliberately light
# (stdlib `os` only); importing it here does NOT pull Flask or the world app
# (unlike server.py), which is the whole reason this script historically
# avoided importing umwelt at all.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from umwelt._ids import DEFAULT_IDENTITY_KEY_PATH  # noqa: E402

# Mirrors server.py's _DEFAULT_BIND_SOCKET_PATH -- not imported, because
# importing server.py here would pull in Flask and the rest of the world
# app for a client that only ever needs to open a socket. Kept in sync by
# eye with bind_client.py's own copy of this same constant; if the two
# drift, --socket-path is the escape hatch.
_DEFAULT_SOCKET_PATH = "/usr/local/lib/umwelt/run/identity-bind.sock"

# The default key path -- the shared constant from umwelt._ids (imported
# above), so writer and reader agree. THIS IS A PLACEHOLDER location, not
# yet a ratified convention (see module docstring's SAFETY section); the
# default string lives in one place (_ids.py), overridable here via --key-path.
_DEFAULT_KEY_PATH = DEFAULT_IDENTITY_KEY_PATH

_KEY_FILE_MODE = 0o600


def _transact(sock_path: str, frame: dict, timeout: float = 5.0) -> dict:
    """One line-JSON frame in, one response out -- same shape as
    bind_socket.py's test suite's own `_transact`, and the exact same
    helper bind_client.py already lifted from it -- deliberately not
    reinvented a third time, so all three agree on what "one transaction"
    means."""
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


def _derive_agent_id() -> str:
    """Derive the identity to bind from the CALLER's own OS account name --
    never typed, never taken as a CLI argument. See module docstring's "Why
    no root, no --kind, no --identity" section for why this is load-bearing
    rather than a convenience: the server-side authorization predicate can
    only ever succeed for a non-root caller's OWN identity, so there is no
    legitimate value this script could accept here other than the one
    already implied by `os.getuid()`.

    Exits (loudly, not a guess) if the account name doesn't start with
    `agent-` -- this script is not meant to run as any other kind of
    account, and guessing at an identity for a differently-shaped account
    name would risk attempting a bind against the wrong row.
    """
    uid = os.getuid()
    try:
        account = pwd.getpwuid(uid).pw_name
    except KeyError:
        print(f"FAIL: no OS account name resolves for uid {uid} -- cannot "
              f"derive an agent identity to bind.")
        sys.exit(2)
    if not account.startswith("agent-"):
        print(f"FAIL: running as OS account {account!r}, which does not "
              f"start with 'agent-'. This script is only meant to run as "
              f"an agent's own OS account (e.g. 'agent-sol'), self-binding "
              f"its own identity -- it will not guess an identity for any "
              f"other account shape. If you need to bind a HUMAN identity, "
              f"or recover an agent identity you don't have shell access "
              f"to, use tools/bind_client.py (with sudo) instead.")
        sys.exit(2)
    return account[len("agent-"):]


def _generate_keypair() -> tuple[Ed25519PrivateKey, dict]:
    """Fresh ed25519 keypair, plus the credential_verifier shape the bind
    socket expects -- see module docstring's "CREDENTIAL SCHEME" section
    for why this shape is not invented here."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    pub_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    verifier = {
        "scheme": "ed25519",
        "public_key": base64.b64encode(pub_bytes).decode("ascii"),
    }
    return private_key, verifier


def _path_occupied(path: Path) -> bool:
    """True if ANYTHING sits at `path` -- regular file, directory, or a
    symlink whether it resolves or not. Deliberately `os.lstat`, never
    `Path.exists()`/`os.stat`: those FOLLOW symlinks, so a dangling
    symlink at `path` makes `.exists()` return False -- indistinguishable
    from truly nothing being there. That is exactly the TOCTOU gap Mira's
    #601 security-lane review named: a dangling symlink would silently
    skip the reset-confirmation prompt entirely, and the caller would
    proceed to `_write_private_key` believing this is a first-ever bind.
    `lstat` reports on the symlink itself, never the thing it points to,
    so it can't be fooled by what the link resolves (or fails to resolve)
    to. Used by `main()` to decide whether to ask for reset confirmation --
    ANYTHING existing (including a dangling symlink) means ask."""
    try:
        os.lstat(str(path))
    except FileNotFoundError:
        return False
    return True


def _refuse_if_non_regular(path: Path) -> None:
    """Raise if `path` exists and is anything OTHER than a plain regular
    file -- a symlink (dangling or not), a directory, a device node, etc.

    This is narrower than `_path_occupied` on purpose: a pre-existing
    REGULAR file is exactly the legitimate reset case (`os.replace` in
    `_write_private_key` safely and atomically swaps it for the new key),
    so it must NOT be refused here. What must be refused is anything
    `os.replace`'s target semantics don't make safe -- most importantly a
    SYMLINK, where letting a plain existence check pass through would mean
    `_write_private_key` swaps out the symlink itself (fine) after a
    caller-facing confirmation prompt that talked about "overwriting your
    key" without ever mentioning that a symlink -- possibly planted by
    something other than this tool -- was standing at the path at all.
    Absence is always fine (the ordinary first-bind case)."""
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError(
            f"refusing to write the private key: {path} exists but is "
            f"not a regular file (lstat mode {oct(st.st_mode)}) -- most "
            f"likely a symlink. Writing through a symlink at this path "
            f"could silently truncate or overwrite an unrelated target "
            f"file. Remove or replace whatever is at this path with "
            f"either nothing or a plain regular file before re-running.")


def _stage_private_key(path: Path, private_key: Ed25519PrivateKey) -> Path:
    """Write the private key as base64-encoded raw bytes, single line,
    newline-terminated, at mode 0600, to a FRESH, uniquely-named file in
    `path`'s own directory (same directory is required so the later swap
    stays on one filesystem and is therefore atomic) -- flushed and
    fsync'd to disk, WITHOUT ever opening or otherwise touching `path`
    itself. Returns the staged file's path; call `_finalize_private_key`
    to swap it into place, or `_discard_staged_key` to drop it.

    Split out of a single write-after-bind function (Mira's #601 security
    review, SECOND round -- her first-round REQUEST CHANGES on the
    original open()-with-O_TRUNC-in-place version is items 1-2 below; her
    re-review found a THIRD, deeper gap this split exists to close, see
    `main()`'s own comments at each call site):

    3. STAGED AFTER THE POINT OF NO RETURN. The atomic-write version this
       replaces was still correct in isolation, but `main()` called it
       AFTER the server-side bind had already succeeded -- so any staging
       failure (disk full, fsync error, a kill) past that point left the
       server trusting a verifier whose private half existed nowhere but
       this process's volatile memory: an orphaned credential with no
       recovery path at all, the old key (if any) already superseded
       server-side. Fixed by splitting persistence into two phases around
       the bind transaction: THIS function runs BEFORE it (so a staging
       failure aborts while nothing server-side has changed yet -- the old
       binding, if any, is untouched), and `_finalize_private_key` runs
       only AFTER the server confirms acceptance.

    1. NOT ATOMIC (first-round finding). The original version opened
       `path` directly with O_TRUNC and wrote in place, so a crash, short
       write, or disk-full error mid-write could leave `path` empty or
       partial with the old key already destroyed. Fixed: the complete key
       is written to this staging file first, flushed and fsync'd to disk,
       and swapped into `path` only via a later atomic `os.replace` (see
       `_finalize_private_key`) -- a single directory-entry update, not a
       content write, so it cannot itself leave a partial file. Nothing in
       THIS function ever touches `path`'s own directory entry, so a
       failure during staging leaves whatever is (or isn't) at `path`
       completely untouched.
    2. SYMLINK-FOLLOWING (first-round finding). The original mode check
       used `Path.exists()` and wrote via `os.open(path, ...)` directly --
       both follow symlinks, so a dangling symlink at `path` would
       silently skip the reset-confirmation prompt, and an existing
       symlink would have its TARGET truncated. Fixed: `path` is refused
       via `_refuse_if_non_regular` (lstat-based, see its own docstring)
       if it is anything OTHER than a plain regular file or nothing at all
       -- a second, independent guard, not only relying on the caller
       having already checked via `_path_occupied`. The staging file
       itself is opened with `O_EXCL` (fail rather than silently
       reuse/overwrite a leftover from a prior crashed run) and
       `O_NOFOLLOW` where the platform supports it.

    Never prints or returns the encoded key material -- see module
    docstring's SAFETY section: nothing downstream of this function ever
    sees the private key again in this process.
    """
    priv_bytes = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    encoded = base64.b64encode(priv_bytes).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_if_non_regular(path)  # second, independent guard -- see its own docstring

    # O_NOFOLLOW may not exist on every platform (e.g. some non-Linux
    # POSIX systems lack it); fall back to 0 (no-op flag) rather than
    # crash the import/call on a platform where it's simply unavailable.
    _o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    tmp_name = f".{path.name}.tmp.{os.getpid()}.{base64.b16encode(os.urandom(6)).decode('ascii').lower()}"
    tmp_path = path.parent / tmp_name

    fd = os.open(
        str(tmp_path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_nofollow,
        _KEY_FILE_MODE)
    try:
        os.fchmod(fd, _KEY_FILE_MODE)  # belt-and-suspenders over O_EXCL's own new-file mode
        with os.fdopen(fd, "w") as f:
            f.write(encoded + "\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        # Staging failed -- clean up the temp file (best-effort; it may
        # already be gone if fdopen's own `with` block closed it before a
        # later step failed) and re-raise. `path` itself was never opened,
        # so whatever was (or wasn't) there before this call is untouched.
        try:
            os.unlink(str(tmp_path))
        except FileNotFoundError:
            pass
        raise

    return tmp_path


def _finalize_private_key(tmp_path: Path, path: Path) -> None:
    """Atomically swap an ALREADY-STAGED key (from `_stage_private_key`)
    into `path`. Call this ONLY after the server-side bind transaction has
    been accepted -- see `main()`'s call sites.

    If `os.replace` itself fails (target became a directory, an immutable
    attribute, etc.), `tmp_path` is DELIBERATELY LEFT ON DISK, never
    cleaned up here: past this point the server already trusts the new
    verifier, so `tmp_path` is the ONLY surviving copy of its matching
    private half, and deleting it would orphan the credential with no
    recovery path at all (Mira's #601 second-round finding -- the version
    this replaces called `os.unlink` on exactly this failure, discarding
    the one thing worth keeping). An explicit message names the staged
    path and the manual recovery step, and this re-raises so the caller's
    exit code reflects the failure.
    """
    try:
        os.replace(str(tmp_path), str(path))
    except OSError as exc:
        print(
            f"CRITICAL: the server has ALREADY ACCEPTED this credential, "
            f"but moving the staged private key into place at {path} "
            f"failed ({exc}). The key is NOT lost -- it is preserved, "
            f"exactly as written, at {tmp_path}. To recover: confirm "
            f"{tmp_path} contains the expected key material, then move it "
            f"into place yourself: `mv {tmp_path} {path} && chmod 600 "
            f"{path}`. Do NOT re-run this tool -- a fresh run would treat "
            f"this as an ordinary reset and generate ANOTHER new keypair, "
            f"discarding the one the server already has.", file=sys.stderr)
        raise

    # Past this point the bind has fully succeeded and the key is
    # correctly, durably (content-wise) written to `path`. Only the
    # DIRECTORY ENTRY's own crash-durability remains open -- and a failure
    # here must NEVER look like the operation failed. Ariadne's #601
    # review reproduced exactly why: raising past this point surfaces a
    # raw traceback on a run that fully succeeded, and the natural
    # recovery (re-run the tool) hits the existing-key path, prompts for a
    # reset, and a user who believes the first run failed types "yes" --
    # destroying a perfectly good private key and forcing an unnecessary
    # second server-side bind. So this step WARNS, never raises; the
    # function still returns normally.
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        print(
            f"WARNING: the key at {path} is correctly written and the "
            f"bind has ALREADY SUCCEEDED -- but this process could not "
            f"confirm the directory entry itself is crash-durable "
            f"({exc}). Do NOT re-run this tool: the key is already bound "
            f"and present; re-running would prompt to overwrite a "
            f"perfectly good credential. This is expected on some "
            f"network or FUSE filesystems and is not itself a sign that "
            f"anything is wrong.", file=sys.stderr)


def _discard_staged_key(tmp_path: Path) -> None:
    """Best-effort delete of a staged key file that was never accepted
    server-side -- the bind was refused or the transaction failed BEFORE
    ever completing. Safe: no live credential depends on this file, unlike
    the post-acceptance case `_finalize_private_key` guards."""
    try:
        os.unlink(str(tmp_path))
    except FileNotFoundError:
        pass


def _write_private_key(path: Path, private_key: Ed25519PrivateKey) -> None:
    """Stage then finalize in one call, with no bind transaction in
    between. Kept as a convenience for tests exercising the stage/finalize
    mechanics directly, and for any future caller with no server-side step
    to sequence around. `main()` itself never calls this -- it calls
    `_stage_private_key` and `_finalize_private_key` separately, with the
    bind transaction between them, so a staging failure aborts before any
    server-side change and a post-bind failure preserves the staged file
    for recovery (see both functions' own docstrings)."""
    tmp_path = _stage_private_key(path, private_key)
    _finalize_private_key(tmp_path, path)


def _confirm_reset(generate_only: bool) -> bool:
    """Explicit typed 'yes' required before overwriting an existing local
    key -- same confirm-before-destructive-action posture as
    bind_client.py, adapted since there is no identity name left to type
    back (see module docstring: the identity is derived, never typed).
    Default to abort on anything else, including a bare Enter.

    `generate_only` selects between two GENUINELY DIFFERENT consequences,
    not two wordings of the same one -- Mira's security review of PR #634,
    exact tip b12da10: the bind path's warning ("resets the server-side
    credential") is TRUE only on the bind path. `--generate-only` never
    touches the socket at all (see `main`'s own branch below), so an
    existing-key reset there overwrites the LOCAL private key while the
    server keeps enrolling the OLD public key -- the caller is left
    holding a new keypair the server has never heard of, and their
    current login breaks until the new public key is sent through and
    enrolled. Presenting the bind-path wording here would tell a
    generate-only caller their server credential was reset when it was
    not touched at all -- the opposite of what happened, at the exact
    moment they most need the correct fact to decide whether to proceed.
    """
    print("A local identity key already exists at the target path.")
    if generate_only:
        print("Proceeding will OVERWRITE the local private key file (the "
              "old key is destroyed, not recoverable). This is "
              "--generate-only: the socket is NEVER touched, so the "
              "WORLD-SIDE credential is UNCHANGED -- the server keeps "
              "enrolling the OLD public key. Your current login will "
              "BREAK the moment the old local key is gone, and stays "
              "broken until you send the NEW public key printed below to "
              "the operator and they re-enroll it.")
    else:
        print("Proceeding will OVERWRITE the local private key file (the "
              "old key is destroyed, not recoverable) AND reset the "
              "server-side credential, invalidating the old public key.")
    answer = input("Type 'yes' to proceed: ")
    return answer == "yes"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/bind_client_agent.py",
        description="Self-bind (or reset) THIS agent's own ed25519 "
                     "credential over umwelt's identity-bind socket. Always "
                     "runs as the caller's own OS account -- no root, no "
                     "sudo, no --identity flag. See tools/bind_client.py "
                     "for the human/root path.",
    )
    parser.add_argument(
        "--socket-path", default=_DEFAULT_SOCKET_PATH,
        help=f"path to the identity-bind socket (default: "
             f"{_DEFAULT_SOCKET_PATH}, matching the world server's own "
             f"--bind-socket-path default)")
    parser.add_argument(
        "--key-path", default=_DEFAULT_KEY_PATH,
        help=f"local path to persist the private key (default: "
             f"{_DEFAULT_KEY_PATH}) -- see module docstring's SAFETY "
             f"section: this location is a PLACEHOLDER pending "
             f"confirmation from the login-design seat, not yet a "
             f"ratified convention")
    parser.add_argument(
        "--generate-only", action="store_true",
        help="generate the keypair, persist the private half locally, "
             "print the PUBLIC half, and STOP -- never touch the bind "
             "socket. This is the remote-agent path (umwelt#606): an "
             "agent with no OS account on the world host cannot "
             "self-bind, because the socket authorizes over SO_PEERCRED "
             "and is local-only by construction. Send the printed public "
             "key to whoever runs the world host; they enroll it with "
             "`sudo tools/bind_client.py --identity <you> --kind agent "
             "--public-key <it>`. Your private key never leaves this "
             "machine")
    args = parser.parse_args(argv)

    # --generate-only does NOT derive an identity from the OS account.
    # On a remote host the account is very often not named `agent-<id>`
    # (a personal laptop login, a container's default user), and
    # `_derive_agent_id` exits hard on that shape -- correctly, for the
    # bind path, where guessing wrong would target the wrong row. Here
    # there is no row to target: the identity is named later, by the
    # operator, on the world host. Requiring a local account name we
    # never use would lock out exactly the agents this flag is for.
    agent_id = None if args.generate_only else _derive_agent_id()
    key_path = Path(args.key_path).expanduser()

    if _path_occupied(key_path):
        if not _confirm_reset(args.generate_only):
            print("Aborted. Nothing was sent, and the existing key file "
                  "was left untouched.")
            return 1

    private_key, verifier = _generate_keypair()

    if args.generate_only:
        # No transaction to protect, so stage-then-finalize collapses to
        # a single durable write through the SAME two helpers the bind
        # path uses -- reused rather than reimplemented, so the mode
        # bits, atomic-swap and non-regular-file refusals stay identical
        # on both paths instead of drifting apart.
        staged_path = _stage_private_key(key_path, private_key)
        _finalize_private_key(staged_path, key_path)
        fingerprint = hashlib.sha256(
            base64.b64decode(verifier["public_key"])).hexdigest()[:16]
        print(f"private key written to {key_path} (mode 0600) -- it never "
              f"leaves this machine")
        print()
        print("PUBLIC KEY (send this; it is not a secret):")
        print(f"  {verifier['public_key']}")
        print(f"  fingerprint: SHA256:{fingerprint}")
        print()
        print("The world-host operator enrolls it with:")
        print(f"  sudo python3 <their-umwelt-checkout>/tools/bind_client.py "
              f"\\\n      --identity <your-id> --kind agent "
              f"--public-key {verifier['public_key']}")
        print()
        print("Give them the fingerprint by a second route if you can -- "
              "it is what makes a mangled or swapped paste detectable.")
        return 0

    # Stage the complete, durable private key BEFORE attempting the bind --
    # Mira's #601 second security round: the only recoverable copy of a
    # newly-bound private key must already be safely on disk before the
    # point of no return (the server accepting the new verifier), not
    # after. If staging itself fails here, nothing server-side has changed
    # yet, so there is nothing to unwind.
    staged_path = _stage_private_key(key_path, private_key)

    try:
        resp = _transact(args.socket_path, {
            "op": "bind",
            "agent_id": agent_id,
            "kind": "agent",
            "credential_verifier": verifier,
        })
    except FileNotFoundError:
        # _transact's `s.connect(sock_path)` failed because the socket
        # path doesn't exist at all -- STRICTLY pre-send: no bytes were
        # ever transmitted, so the server cannot have processed anything.
        # Safe to discard.
        _discard_staged_key(staged_path)
        print(f"FAIL: no socket at {args.socket_path!r}. Is the world "
              f"server running, and is that the right --socket-path?")
        return 2
    except (ConnectionRefusedError, PermissionError) as exc:
        # Also from `s.connect()` itself -- refused or denied before any
        # request could be sent. Equally safe to discard.
        _discard_staged_key(staged_path)
        print(f"FAIL: could not connect to {args.socket_path!r}: {exc}\n"
              f"If this is a permission error, your account may not be "
              f"in the socket's OS group -- see bind_socket.py's "
              f"'Socket file mode' docstring section.")
        return 2
    except Exception as exc:
        # Mira's #601 THIRD security round: everything else `_transact`
        # can raise -- TimeoutError/ConnectionResetError/BrokenPipeError
        # during sendall() or the recv() loop, a truncated/malformed
        # response (json.JSONDecodeError), `_transact`'s own "socket
        # closed with no response" RuntimeError, or anything else
        # unanticipated -- happens AFTER `connect()` already succeeded.
        # The request may well have reached the server and been fully
        # processed; this process simply failed to OBSERVE the
        # confirmation. That means acceptance is UNKNOWN, not false, and
        # discarding the staged key here risks the exact credential-
        # orphaning outcome this whole mechanism exists to prevent. So:
        # every failure past the two narrow, PROVEN-pre-send excepts
        # above defaults to PRESERVE, not discard -- this is deliberately
        # a broad `except Exception` (not `except BaseException`: a
        # genuine KeyboardInterrupt is left to propagate normally, and
        # since nothing in this branch deletes the staged file, that is
        # already the safe outcome for it too).
        print(
            f"FAIL: the bind transaction with {args.socket_path!r} did "
            f"not complete cleanly ({exc!r}), and it is NOT KNOWN "
            f"whether the server received and accepted the request "
            f"before this failure occurred. The staged private key has "
            f"been PRESERVED, not discarded, at {staged_path} in case "
            f"the server DID accept it. Do NOT re-run this tool yet: "
            f"first confirm, server-side, whether {agent_id!r} is now "
            f"bound. If it is, recover with `mv {staged_path} {key_path} "
            f"&& chmod 600 {key_path}`. If it is not, delete "
            f"{staged_path} and re-run this tool.")
        return 2

    if resp.get("status") != "ok":
        # Refused, not accepted -- the staged key was never bound to
        # anything; safe to discard.
        _discard_staged_key(staged_path)
        print(f"FAIL: {resp.get('error', resp)}")
        return 1

    # The server has ACCEPTED the new verifier -- only now is the staged
    # key swapped into its final location. Past this point a failure must
    # preserve, never discard, the staged file: see
    # _finalize_private_key's own docstring for why.
    _finalize_private_key(staged_path, key_path)

    print(f"OK -- bound {resp.get('agent_id')!r}: "
          f"bound_at={resp.get('bound_at')!r} "
          f"bound_by={resp.get('bound_by')!r}")
    print(f"bound public key: {verifier['public_key']}")
    print(f"private key written to {key_path} (mode 0600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
