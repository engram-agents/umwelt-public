"""Persistent host-only bind/reset socket for Umwelt world credentials
(umwelt#495 slice 2, design-of-record: in-world thing 92dc676e, revision 6,
"World credentials + enrollment", lead Borges; security-lane read: Borges;
design read: Kepler; ratified by Lei).

This module owns exactly one job: given an on-host operator connected via
a Unix-domain socket, decide whether they may write a `bind` for a given
world identity, and if so, write it (db.bind_identity). It is deliberately
NOT slice 3 -- no challenge/response, no password hashing, nothing about
proving a credential at LOGIN time. This slice only answers "is the
connecting operator allowed to write this identity's credential row."

**sudo is for enrollment/backfill and rare recovery -- NOT the daily login
path.** (Lei, relayed via Borges.) The root branch of the authorization
predicate below exists so a human operator (who has no `agent-<name>` OS
account of their own -- see _ids.human_residents()) can bind/reset ANY identity,
including their own, via `sudo`. That is meant to be reached rarely: once
per identity at first enrollment, and again only for genuine credential
recovery. An agent binding/resetting its OWN identity (the common case)
never needs sudo -- its own `agent-<name>` OS account authorizes it
directly. A future reader should not read the sudo path as something to
streamline into routine use -- it is intentionally the harder, rarer path.

Socket lifecycle -- PERSISTENT, not per-request/per-use
--------------------------------------------------------
Unlike cap_handler.CapHandler (one ephemeral UDS per `use()` call, in a
fresh tempdir, gated by directory permissions), this socket is started
ONCE at server boot (server.py's main(), right after `create_app` and
before `app.run()`) and lives for the process lifetime. The mechanics
(AF_UNIX + SOCK_STREAM, bind+listen, a 0.5s-timeout-polled accept() loop
in a daemon thread so it can notice a threading.Event shutdown signal, one
more daemon thread per accepted connection, unlink-on-stop) are copied
from CapHandler -- CapHandler's own directory-permission trick (a fresh
0700 tempdir per use) doesn't apply here, since this socket lives for the
whole process lifetime in a fixed, shared location every agent's own uid
must be able to *reach* -- so `start()` explicitly chmods the socket file
to `_SOCK_MODE` (0660, group `agents` -- corrected in review, PR #506,
see "Socket file mode" below) after bind() -- SO_PEERCRED authorizes the
BIND TARGET, not connection eligibility; the filesystem permission is
what keeps non-agent-uid processes from even attempting to connect in the
first place, defense in depth on top of (not instead of) the identity
check below.

**Socket file mode -- 0660 + group `agents`, NOT 0600 and NOT 0666
(Kepler's design ruling, PR #506, after Borges caught that the directory
relocation above fixed only one of two independent gates).** The original
`0600` survived the directory fix untouched and remained an independent
sufficient cause of the same unreachability bug: even in a traversable
directory, a `0600` socket owned by whichever agent's uid happens to run
the server refuses every OTHER agent's connection attempt outright, so
`authorized_bound_by`'s predicate never even gets exercised for them.
`0660` restores the common case (an agent binding its own identity) while
narrowing WHO MAY EVEN ATTEMPT a connection to members of the `agents`
OS group, never widened to every uid on the box. **This is not an
authorization property, and the group's exact membership is not asserted
anywhere in code -- see the correction below.** `0666` was considered and
rejected -- `authorized_bound_by` already refuses any uid whose account
name isn't `agent-<target>`, so `0666` buys nothing over `0660` and only
increases how much unauthenticated input reaches the parser.

**Correction (Borges's security-lane re-read + Kepler's design read,
PR #506): the `agents` group is NOT "every `agent-*` account and nothing
else."** An earlier version of this docstring claimed that -- a caveat
("plus a human ops account") got dropped between a review comment and
this file and the false version shipped. Measured (`getent group
agents`) on this host: the group also contains a non-agent ops account.
**The OS group determines who may ATTEMPT a connection; it is not the
authorization.** `authorized_bound_by` refuses any uid whose account
name isn't `agent-<target>` regardless of group membership, so a
non-agent member of this group can connect and bind nothing -- group
membership buys reduced parser exposure, never authority. Group
membership is deploy-time state, verifiable with `getent group agents`,
not an invariant this code enforces or asserts: a hard check here would
fail the world CLOSED the next time a legitimate account is added to (or
removed from) the group, for a property nobody has committed to
maintaining. Don't re-add a "contains exactly X" claim to this
docstring; state what the code actually depends on (the predicate, not
the roster) instead. `start()` sets the group EXPLICITLY via
`os.chown` and raises RuntimeError if the configured group does not
resolve (see `_DEFAULT_SOCK_GROUP` and `BindSocketHandler.__init__`'s
`sock_group` param below) -- a mode change that looks like it
grants access and silently doesn't (e.g. the socket ending up owned by
the server process's own primary group instead of `agents`) is exactly
this PR's own failure history repeating, so this is a fail-loud check,
not a best-effort one, matching `_validate_socket_parent_safe`'s posture.

**Scope correction: self-bind is ON-HOST-ONLY by construction, not
"agents self-bind, humans need sudo."** `SO_PEERCRED` returns a uid on
*this* machine only -- an off-host agent (no OS account on this host at
all) can never be authorized by it, regardless of socket permissions.
Off-host agents are in the SAME position as humans: root-only, via sudo,
every time. This isn't a defect in this slice; it just means "agents
self-bind" as a summary covers only the on-host subset, not every agent
-- worth stating plainly since umwelt#495's credential scope now
explicitly includes agents (Lei's identity-SSoT ruling), and an off-host
agent's credential path is the sudo path permanently, not a gap this
socket will ever close on its own.

**Socket location -- NOT the private per-server db-dir (corrected in
review, PR #506).** The original implementation defaulted the socket into
`<db-dir>/identity-bind.sock`, i.e. whichever single agent's uid happens
to run the world server at any given time owns that directory outright.
Ariadne's colleague pass caught this live (real `connect()`, her own
uid, against a copy of prod): the directory itself is not traversable by
other agents (no ACL entry), and even where an ACL grants traversal
(agent-borges), the socket's own `chmod 0600` collapses the ACL mask and
still refuses everyone but the directory's owner and root. Net effect:
rev6's "an agent binds its own identity via its own uid, no sudo needed"
path was UNREACHABLE for every agent except whichever one happens to run
the server process. Sol reproduced this live from a second account.

Borges's ruling (security-lane, PR #506): move the socket to a directory
every agent's uid can traverse, e.g. `/usr/local/lib/umwelt/run` --
**but with a hard constraint the original design missed entirely**: that
directory (and its own parent) must be traversable-BUT-NOT-WRITABLE by
any agent. `/home/agents-shared/**` was considered and rejected --
verified live to be `setgid` but with NO sticky bit, meaning any agent in
the shared group can rename or replace any entry, including one it does
not own. A directory with that property is actively WORSE than the
original private-db-dir bug: instead of merely refusing non-owner binds
(a denial-of-service on everyone but the server's own uid), a
group-writable-without-sticky directory lets a rogue agent pre-create
(or replace) the socket file at the well-known path *before* the real
server binds it -- an attacker-controlled listener harvesting every
`credential_verifier` payload sent to what looks like the real bind
socket. `_validate_socket_parent_safe` (below) makes this an enforced
invariant, not a deployment convention: `start()` refuses to bind at all
if the socket's parent directory (or that directory's own parent) is
group- or world-writable, rather than silently trusting whatever
directory happens to be configured. Because that check can reject a
directory this process cannot itself safely create (fixing group/owner
requires provisioning it correctly ahead of time -- see the `--bind-
socket-path` help text in server.py), the actual root-owned directory
creation is an out-of-band deployment step, not something this module
attempts on its own; `start()`'s own `mkdir(parents=True, exist_ok=True)`
remains only for the convenience of tests and single-owner dev setups
where the whole path is already safely rooted (e.g. under a pytest
`tmp_path`), never as a substitute for that provisioning in production.

Separately, Borges re-flagged (not a code change -- the existing ordering
was already correct) why `_op_bind`'s authorization-before-DB-lookup
sequencing stays load-bearing: `authorized_bound_by` is checked, and
returns its uniform "unauthorized" error, BEFORE `db.bind_identity` ever
touches the `identities` table. Since a non-root caller can only ever be
authorized for its own identity, this means no caller can use this
socket to probe whether some OTHER `agent_id` has a row at all --
"unauthorized" and "would have been not-found" are indistinguishable
from the outside. A future edit that reordered this to give a nicer
error message (e.g. "no such identity" before checking who's asking)
would quietly turn the socket into an identity-existence oracle. Don't
reorder it.

Authorization predicate -- NOT a lookup table
-----------------------------------------------
Kepler's ruling, verbatim: "uid N may bind identity X iff N is root, or
N's account name is `agent-X`." This is an authorization PREDICATE,
computed fresh on every request from two external, kernel/OS-sourced
facts (the peer's real uid, and that uid's OS account name) -- there is
no uid-to-identity mapping table/config/allowlist anywhere in this
module, by design: a table would be a second, weaker, unaudited
escalation path parallel to sudo, and it could not even represent the
human-identity case (none of human_residents() has an OS account at all on
this host; the ONLY path to bind a human identity is uid 0/root).

Wire protocol -- host-only, JSON-line based
---------------------------------------------
One op, `bind` (reset reuses it -- see db.bind_identity's docstring for
why there is no separate reset path):

    {"op": "bind", "agent_id": "<name>", "kind": "human"|"agent",
     "credential_verifier": {...opaque JSON, uninterpreted by this slice...}}

    -> {"status": "ok", "agent_id": "<canonical>", "bound_at": "<iso>",
        "bound_by": "<canonical requester identity, or 'root' if uid 0>"}
    -> {"status": "error", "error": "<message>"}

Every failure path returns a JSON error line and closes the connection
cleanly -- no unhandled exception ever escapes _handle_conn, mirroring
CapHandler._handle_conn's own defensive posture.

Loudness (umwelt#495 slice 2 §5)
-----------------------------------
Design requirement: "A credential reset must be loud and unhidable... a
world event in the identity's own history, visible forever." Every
successful bind (first-time bind AND reset alike -- simpler and safer to
always announce than to branch on which) mints a world-attested crossing
into the bound identity's own home Post (`place_post_<agent_id>`), via
the EXISTING db.build_crossing/place_post_<agent> machinery
(_apply_effect_notify_author in server.py is the precedent this reuses)
rather than a new audit table. See `_announce_bind`'s docstring for the
full reasoning on why this mechanism was chosen over `db.mint_event`
(the `events` table) -- in short: `events` rows are mint-on-demand
snapshots of an ALREADY-EXISTING underlying log entry (crossing/use/act/
advance_ref), keyed by a CHECK-constrained `event_kind` that has no
'bind' member and would need a schema migration to add one; a crossing
into the identity's own Post is already append-only (no update/delete
route touches `crossings`), already queryable by the affected identity
through the `hear` tool they already have, and needs no schema change at
all. This was flagged as a judgment call in the implementing PR rather
than decided silently.
"""

from __future__ import annotations

import json
import grp
import os
import pwd
import socket
import sqlite3
import stat
import struct
import sys
import threading
import uuid
from pathlib import Path

from . import db
from ._ids import canonical_agent

# umwelt#531/#534 (Borges's review, #534): the single install command,
# imported by server.py's --bind-socket-path help text so a human reading
# a boot-time crash and a human reading --help see the identical string,
# never two commands that quietly drifted apart.
_BIND_SOCKET_INSTALL_HINT = (
    "sudo install -d -m 0750 -o <server-uid> -g agents "
    "/usr/local/lib/umwelt/run")

# Socket file mode -- see the module docstring's "Socket file mode" section.
# Owner+group rw; SO_PEERCRED still authorizes the bind target, this is
# eligibility-to-connect-at-all, not authorization.
_SOCK_MODE = 0o660

# The OS group that must own the socket file for _SOCK_MODE=0o660 to grant
# anything -- see the module docstring's "Socket file mode" section,
# including the correction on this group's exact membership (it is not
# asserted to contain only agent-* accounts -- that's deploy-time state,
# not an invariant this code checks). Overridable (BindSocketHandler's
# sock_group param) so tests can target a group that actually exists in
# whatever sandbox they run in, without weakening the production default.
_DEFAULT_SOCK_GROUP = "agents"

# Listen backlog -- this is a low-traffic, host-only, operator-driven
# socket (bind/reset events, not a hot request path); a small backlog is
# ample and mirrors CapHandler's own per-use connection cap in spirit
# without needing a matching resource-cap regime (there is no untrusted,
# potentially-hostile scroll on the other end of THIS socket the way
# CapHandler's peer can be -- every peer here has already cleared the
# SO_PEERCRED authorization check before anything it sends is trusted).
_LISTEN_BACKLOG = 8

# Defensive cap on buffered-but-not-yet-newline-terminated bytes per
# connection -- not a resource cap named anywhere in the design spec, just
# the same "never buffer unboundedly waiting for a newline that may never
# come" guard cap_handler.py applies to its own frames. 1 MiB is generous
# headroom over any plausible credential_verifier payload (a hash or a
# public key, not a blob).
_MAX_FRAME_BYTES = 1024 * 1024

_BIND_KEYS = frozenset({"op", "agent_id", "kind", "credential_verifier"})

# How many ancestor directories above the socket's own parent to check for
# unsafe write permissions -- see _validate_socket_parent_safe. Borges's
# ruling named the socket's parent AND "its own parent likewise" (2
# levels total: the immediate parent, plus one more up); we don't walk
# indefinitely toward "/" since every well-formed deployment root
# (/usr, /usr/local, /usr/local/lib, ...) is root-owned and non-writable
# by construction on a normal Linux install -- this check is aimed at the
# directory an operator actually chooses/provisions, not the whole tree.
_ANCESTOR_CHECK_DEPTH = 2


def _validate_socket_parent_safe(sock_path: Path) -> None:
    """Refuse to proceed if the socket's parent directory -- or that
    directory's own parent -- is writable by anyone other than its owner.

    See this module's docstring, "Socket location" section, for the
    incident this guards against: a directory that is traversable (so
    every agent can reach the socket) but ALSO group- or world-writable
    lets a rogue agent pre-create (or replace) a file at the socket's own
    path before the real server binds it -- a fake listener that harvests
    every `credential_verifier` payload sent to what looks like the real
    socket. Traversable-but-not-writable is the actual requirement;
    `_SOCK_MODE`'s 0660+group on the socket FILE itself only protects
    against a naive post-bind connection attempt from a non-agent uid,
    not this pre-bind path-squatting attack (which a member of the
    `agents` group can mount just as well as anyone), which is why this
    check exists as a second, independent gate.

    Deliberately does NOT check ownership (root vs a specific agent uid)
    -- that varies by deployment and by which agent's uid happens to run
    the server process at a given time (see the module docstring); the
    portable, checkable invariant is "not writable by the group or by
    everyone", which is what actually stops the path-squatting attack
    regardless of who the legitimate owner is.

    Raises RuntimeError (never silently proceeds, never silently
    downgrades to a warning) naming the exact unsafe directory and its
    mode, so a misconfigured deployment fails loudly at startup rather
    than shipping a socket an attacker can pre-occupy.
    """
    path = sock_path.parent.resolve()
    for _ in range(_ANCESTOR_CHECK_DEPTH):
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise RuntimeError(
                f"bind_socket: cannot stat {path} while validating the "
                f"identity-bind socket's directory safety: {exc}"
            ) from exc
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(
                f"bind_socket: refusing to start -- {path} is group- or "
                f"world-writable (mode {oct(mode & 0o777)}). The identity-"
                "bind socket's directory (and its own parent) must be "
                "traversable but NOT writable by anyone but its owner, or "
                "a rogue agent could pre-create the socket path and "
                "harvest credential payloads before the real server "
                "binds it. Provision a dedicated directory (see server.py "
                "--bind-socket-path's help text) rather than pointing "
                "this at a directory shared for other, less sensitive "
                "purposes."
            )
        parent = path.parent
        if parent == path:  # reached filesystem root
            break
        path = parent


def _err(msg: str) -> bytes:
    return (json.dumps({"status": "error", "error": msg}) + "\n").encode()


def _ok(data: dict) -> bytes:
    return (json.dumps(data) + "\n").encode()


def _peer_uid(conn: socket.socket) -> int:
    """Resolve the connecting peer's REAL uid via SO_PEERCRED (Linux).

    SO_PEERCRED's uid is the kernel's own record of the REAL uid of the
    process that called connect() -- NOT an effective uid, and not
    anything the peer process could self-report or spoof (a setuid binary
    sitting in the connection path cannot change what this returns; the
    kernel captures the real uid of the connecting process at connect()
    time). This is the ONLY uid this module ever trusts for authorization
    -- see authorized_bound_by below, and never substitute a caller-
    supplied/self-reported uid for it.
    """
    creds = conn.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def authorized_bound_by(peer_uid: int, requested_agent_id: str) -> str | None:
    """The authorization predicate (Kepler's ruling, verbatim): uid N may
    bind identity X iff N is root, or N's account name is `agent-X`.

    Returns the `bound_by` value to record when authorized (`"root"` for
    uid 0, else the canonical requested identity -- a non-root bind is
    always a self-bind, so the requester IS the identity being bound),
    or None when unauthorized.

    Root is `uid == 0` EXACTLY -- never a range check, never group
    membership. Loosening this to anything else IS the whole mechanism;
    don't.

    **Case-fold direction is ONE WAY ONLY.** `canonical_agent()` folds
    case/NBSP/whitespace on the REQUESTED identity (`requested_agent_id`)
    ONLY -- it is never applied to the OS account name side
    (`pwd.getpwuid(...).pw_name`). The OS account name is compared EXACTLY
    as the OS returns it: case-sensitive, no normalization. This is
    deliberate and load-bearing (Borges's security-lane read: "this reads
    as pedantry now and as a CVE later") -- the OS account name is the
    externally-verified, kernel/OS-sourced half of this comparison (an
    attacker cannot choose it without already having root to create the
    account); the requested identity string is caller-supplied over the
    wire and MUST be normalized before any comparison. Folding the OS-name
    side too would let an attacker who controls only the requested
    identity's spelling/case widen who counts as authorized against a
    fixed OS account -- silently, if some future OS account happened to
    exist in a differently-cased form. Never fold `pw_name`.

    Note (checked, not a gap): an OS account literally named `agent-lei`
    would satisfy this predicate and let its owner bind "lei" without
    root. This is NOT an escalation -- creating that OS account already
    requires root, so it adds no capability beyond what root already has.
    """
    if peer_uid == 0:
        return "root"
    try:
        account_name = pwd.getpwuid(peer_uid).pw_name
    except KeyError:
        return None
    canonical_id = canonical_agent(requested_agent_id)
    if account_name == f"agent-{canonical_id}":
        return canonical_id
    return None


class BindSocketHandler:
    """Persistent host-only bind/reset socket -- one instance per running
    world-server process. Call start() once at boot (server.py's main());
    the process's own exit tears down the daemon threads. stop() exists
    for tests / a future graceful-shutdown path, not because main() calls
    it today (see this module's docstring, "Socket lifecycle").
    """

    def __init__(self, sock_path: str, db_path: str,
                 sock_group: str = _DEFAULT_SOCK_GROUP) -> None:
        self._sock_path = sock_path
        self._db_path = db_path
        self._sock_group = sock_group
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()

    def start(self) -> None:
        """Bind the UDS, chown+chmod it to the shared-eligibility mode,
        start listening, accept connections in a daemon thread.

        Always raises RuntimeError -- NEVER a bare OSError/PermissionError,
        never proceeds -- on any failure this method can hit: `sock_group`
        doesn't resolve to a real OS group; the socket's directory doesn't
        exist and this process cannot create it; the directory fails the
        path-squatting safety check (_validate_socket_parent_safe); the
        directory exists and passes that check but this process's uid
        genuinely cannot write into it (bind() itself raises a bare
        OSError here -- umwelt#534, Ariadne's finding: `main()`'s own
        except clause originally caught only RuntimeError and missed this
        exact case, e.g. a root-owned 0755 directory a human created with
        a plain `mkdir -p` rather than the documented install command);
        removing a stale socket file fails; or chown succeeds but chmod/
        listen() unexpectedly doesn't. The last two have no specific named
        story (nobody has hit them) -- they're covered anyway, because the
        contract this method exists to establish is "never a bare OSError
        escapes," not "the failure points we happened to think of." A
        single exception type regardless of which point fired means a
        caller needs exactly one except clause, never a guess at how many
        types `start()` might raise (this fail-loud-not-best-effort
        posture, same as the parent-safety check's own, is deliberate
        throughout this method).

        Every raised exception carries a `.reason_class` string attribute
        (umwelt#534, Borges's finding) -- `available: false` alone
        collapses distinct failure classes ("never provisioned" vs "went
        world-writable" vs "this uid specifically can't write here") into
        one bit; a caller (server.py's /health) that wants to page
        mechanically on a SPECIFIC class, not just "something's wrong",
        needs this rather than parsing `.error`'s free text.
        """
        try:
            sock_gid = grp.getgrnam(self._sock_group).gr_gid
        except KeyError:
            exc = RuntimeError(
                f"identity-bind socket group {self._sock_group!r} does not "
                "exist on this host -- refusing to start rather than bind "
                "with an unconfirmed group (a silently-wrong group makes "
                f"_SOCK_MODE={_SOCK_MODE:#o} grant nothing new)")
            exc.reason_class = "sock_group_missing"
            raise exc

        sock_path = Path(self._sock_path)
        # Convenience for tests / single-owner dev setups whose whole path
        # is already rooted somewhere safe (e.g. a pytest tmp_path) -- NOT
        # a substitute for provisioning the production directory ahead of
        # time with the right owner/group. See module docstring, "Socket
        # location". In production this call can only ever no-op (the
        # directory already exists, root having provisioned it per that
        # same docstring) or fail -- the unprivileged server process has
        # no path by which it could ever successfully CREATE a directory
        # under a root-owned parent. umwelt#531: a bare PermissionError
        # here used to propagate unhandled and take the caller down with
        # it; converted to a named RuntimeError instead, matching this
        # method's other two failure modes below (missing sock_group,
        # chown-permission), so a caller catching RuntimeError sees one
        # consistent failure shape and a message that names the actual
        # gap (an unprovisioned directory) rather than a bare traceback.
        try:
            sock_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            exc = RuntimeError(
                f"identity-bind socket directory {str(sock_path.parent)!r} "
                "does not exist and this process has no permission to "
                "create it -- per this socket's own deployment contract, "
                "an operator with root must provision it ahead of time: "
                f"`{_BIND_SOCKET_INSTALL_HINT}`. Not a reason to refuse to "
                "run the world server that hosts this socket -- see "
                "server.py's main() for how a caller should treat this.")
            exc.reason_class = "directory_unprovisioned"
            raise exc from e
        try:
            _validate_socket_parent_safe(sock_path)
        except RuntimeError as exc:
            exc.reason_class = "directory_unsafe"
            raise
        try:
            if sock_path.exists():
                sock_path.unlink()
        except OSError as e:
            exc = RuntimeError(
                f"cannot remove stale identity-bind socket file "
                f"{self._sock_path!r} before rebinding -- {e}. Likely a "
                "permission mismatch between whoever created the stale "
                "file and this process's own uid.")
            exc.reason_class = "stale_socket_removal"
            raise exc from e

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._server_sock.bind(self._sock_path)
        except OSError as e:
            # umwelt#534 (Ariadne): the directory can exist and pass the
            # path-squatting safety check above -- which only tests
            # group/world write bits, not who OWNS it -- and this
            # process's own uid can still be unable to write into it
            # (a root-owned 0755 directory from a plain `mkdir -p` rather
            # than the documented install command is exactly this case,
            # and it is the FIRST thing a human unfamiliar with the
            # `-o <server-uid>` requirement will actually run). bind()
            # raises a bare OSError here, not a RuntimeError -- reraised
            # as RuntimeError so main() has exactly one exception type to
            # catch regardless of which of this method's four failure
            # points fired.
            exc = RuntimeError(
                f"cannot bind identity-bind socket at {self._sock_path!r} "
                f"-- {e}. The directory exists and passed the path-"
                "squatting safety check, but this process's own uid "
                "cannot write into it -- confirm it was provisioned with "
                f"the documented command: `{_BIND_SOCKET_INSTALL_HINT}` "
                "(a plain `mkdir -p` run as root creates a root-owned "
                "directory no other uid, including the server's own, can "
                "write into).")
            exc.reason_class = "socket_bind_permission"
            raise exc from e
        # See module docstring's "Socket file mode" -- filesystem
        # eligibility (group membership + mode) is defense in depth
        # alongside (not instead of) the SO_PEERCRED authorization check
        # in _op_bind. -1 for uid leaves ownership unchanged (whichever
        # agent's uid runs the server process); only the group is set
        # explicitly, which is the half that must not be left to chance.
        try:
            os.chown(self._sock_path, -1, sock_gid)
        except PermissionError as e:
            # The group resolved (grp.getgrnam above already succeeded --
            # this is a DIFFERENT failure): the process's own uid isn't a
            # member of it, so it cannot hand the socket to a group it
            # doesn't belong to. Named the same way as the group-missing
            # case above rather than left as a bare OSError, so whoever
            # reads the crash knows which of the two preconditions broke
            # (Kepler's design-read note, PR #506).
            exc = RuntimeError(
                f"cannot chown identity-bind socket to group "
                f"{self._sock_group!r} -- the server process's own uid "
                "is not a member of it (provisioning gap: add the "
                "server's OS account to this group, or point sock_group "
                "at one it already belongs to)")
            exc.reason_class = "chown_permission"
            raise exc from e
        try:
            os.chmod(self._sock_path, _SOCK_MODE)
            self._server_sock.listen(_LISTEN_BACKLOG)
        except OSError as e:
            # Genuinely unexpected at this point (chown just succeeded on
            # the same file; listen() on a freshly bound socket almost
            # never fails) -- no specific named story the way the four
            # cases above have one, but the contract this whole method
            # exists to establish (umwelt#531/#534) is that NOTHING
            # escapes start() as a bare OSError, ever, including the
            # cases nobody has hit yet.
            exc = RuntimeError(
                f"identity-bind socket setup failed after a successful "
                f"chown at {self._sock_path!r} -- {e}.")
            exc.reason_class = "socket_setup_unexpected"
            raise exc from e
        self._server_sock.settimeout(0.5)  # lets accept() wake to check _shutdown

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown, close the server socket, wait for the accept
        thread, unlink the socket file."""
        self._shutdown.set()
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            Path(self._sock_path).unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Internal: accept loop and per-connection handler
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                conn, _addr = self._server_sock.accept()
            except (socket.timeout, OSError):
                continue

            t = threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            )
            t.start()

    def _handle_conn(self, conn: socket.socket) -> None:
        # Resolve the peer's real uid ONCE per connection, before any
        # frame is read -- see _peer_uid's docstring for why this is the
        # only uid this module ever trusts. ANY failure to resolve it must
        # fail closed (refuse by closing the connection), not just OSError:
        # on a platform without SO_PEERCRED (macOS) the call raises
        # AttributeError, and an uncaught exception here would kill this
        # handler thread while leaving the peer's connection open and
        # hanging forever (umwelt#1019).
        try:
            peer_uid = _peer_uid(conn)
        except Exception:
            try:
                conn.close()
            except OSError:
                pass
            return

        buf = b""
        try:
            while not self._shutdown.is_set():
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_FRAME_BYTES:
                    try:
                        conn.sendall(_err("frame too large"))
                    except OSError:
                        pass
                    return
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self._dispatch(line, peer_uid)
                    try:
                        conn.sendall(response)
                    except OSError:
                        return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, line: bytes, peer_uid: int) -> bytes:
        try:
            frame = json.loads(line.decode("utf-8", errors="replace"))
        except Exception:
            return _err("malformed frame: not valid JSON")

        if not isinstance(frame, dict):
            return _err("malformed frame: expected JSON object")

        op = frame.get("op")
        if op == "bind":
            return self._op_bind(frame, peer_uid)
        if op is None:
            return _err("malformed frame: missing op")
        return _err(f"unknown op: {op!r}")

    def _op_bind(self, frame: dict, peer_uid: int) -> bytes:
        # Schema-strict: ONLY {op, agent_id, kind, credential_verifier}
        # allowed -- same reject-unknown-keys posture as cap_handler.py's
        # own ops.
        extra = set(frame.keys()) - _BIND_KEYS
        if extra:
            return _err(f"unexpected fields in bind: {sorted(extra)!r}")

        agent_id = frame.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return _err("bind requires a non-empty agent_id string")

        # kind is REQUIRED and written UNCONDITIONALLY by db.bind_identity
        # -- see that function's docstring. No "only if different from
        # seeded" branch exists here or in db.py, by design.
        kind = frame.get("kind")
        if kind not in ("human", "agent"):
            return _err("bind requires kind to be 'human' or 'agent'")

        credential_verifier = frame.get("credential_verifier")
        if not isinstance(credential_verifier, dict):
            return _err("bind requires credential_verifier to be a JSON object")

        bound_by = authorized_bound_by(peer_uid, agent_id)
        if bound_by is None:
            return _err(
                "unauthorized: peer is not root and does not own this identity")

        conn = None
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            row = db.bind_identity(conn, agent_id, kind, credential_verifier,
                                    bound_by)
        except db.IdentityNotFoundError as exc:
            return _err(str(exc))
        except db.IdentityRetiredError as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(f"bind failed: {exc}")
        finally:
            if conn is not None:
                conn.close()

        # Loudness (umwelt#495 slice 2 §5) -- see this module's docstring.
        # Best-effort: the credential write above has ALREADY committed by
        # this point, so a failure here must never be reported as a bind
        # failure.
        self._announce_bind(row, bound_by)

        return _ok({
            "status": "ok",
            "agent_id": row["agent_id"],
            "bound_at": row["bound_at"],
            "bound_by": row["bound_by"],
        })

    def _announce_bind(self, row: dict, bound_by: str) -> None:
        """Mint a world-attested crossing into the bound identity's own
        home Post -- see the module docstring's "Loudness" section for
        the full reasoning on why this mechanism (over a new `events`
        row) was chosen.

        Reuses db.build_crossing + the place_post_<agent> convention
        _apply_effect_notify_author (server.py) already uses for a
        different engine-authored notification -- same posture: sender is
        the acting identity (here, `bound_by` -- "root" for a sudo bind,
        or the self-binding identity's own canonical name), destination
        is the bound identity's home Post, to_agent addresses it to them
        specifically (still lands in the shared place history, nothing
        hidden). attesting_authority='world'/consequence_grade=
        'consequence' come for free from build_crossing's own hardcoded
        defaults -- this genuinely is a world-attested event, not a
        self-report.

        Best-effort, logged not swallowed: by the time this is called,
        db.bind_identity has ALREADY committed the credential write in
        _op_bind -- a failure here (e.g. the identity's Post doesn't
        exist, which the current seeding path -- every identities row
        seeded from an avatar-thing, and every avatar is born only after
        _ensure_home_place mints its Post -- cannot currently produce, but
        isn't schema-guaranteed either) must never be reported as a bind
        failure, since the bind itself already succeeded durably.
        """
        agent_id = row["agent_id"]
        home_place_id = f"place_post_{agent_id}"
        body = f"credential bound by {bound_by} at {row['bound_at']}"

        conn = None
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            if db.get_place(conn, home_place_id) is None:
                print(
                    f"bind_socket: identity {agent_id!r} has no home Post "
                    f"({home_place_id!r}) -- bind succeeded but the "
                    "loudness crossing could not be minted",
                    file=sys.stderr,
                )
                return
            sent_at = db.now_iso()
            db.build_crossing(
                conn, id=str(uuid.uuid4()), sender=bound_by,
                destination=home_place_id, sent_at=sent_at,
                received_at=sent_at, crossing_latency_s=0.0,
                crossing_hops=0, body=body, to_agent=agent_id,
            )
        except Exception as exc:
            print(
                f"bind_socket: failed to mint loudness crossing for "
                f"{agent_id!r}: {exc}",
                file=sys.stderr,
            )
        finally:
            if conn is not None:
                conn.close()
