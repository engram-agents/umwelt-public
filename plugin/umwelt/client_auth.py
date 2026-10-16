"""Client-side umwelt login — acquire + cache an ed25519 session token.

The client half of umwelt #495. The server side (challenge issue, signature
verify, session mint, `resolve_actor`) is already built (#600/#605/#601);
this module makes `uw` (the CLI) and `mcp_server` (the agent MCP bridge)
participate: read the private key, run the challenge→sign→login round-trip,
and send `Authorization: Bearer <token>` on every request.

Both consumers import this module; it is deliberately stdlib-only at import
time (only `os`, `json`, `base64`, `urllib`). `cryptography` is LAZY-imported
inside `_load_private_key` — a top-level import would break uw's
"no key → no regression" invariant (uw must not die at import on a
cryptography-missing host; it should reach the no-key branch and fall back to
declared). Only hosts that actually have a key file pay the cryptography
dependency.

Design constraints (security ruling, Mira 2026-08-18; design review,
Ariadne 2026-08-18):

- **In-memory by default; persisted ON DISK for single-shot clients under
  Kepler's 2026-09-18 security ruling (umwelt#805).** The original design
  below ("never persisted to disk") held while every client was long-lived
  enough to amortize one token across many calls. In practice the single-shot
  clients (`uw`, the four plugin hooks) re-run the full challenge→sign→login
  round-trip on EVERY invocation, which composes with the per-agent login
  rate limit (15/60s) to produce a real incident: a busy agent's hooks
  exhaust the limit, the resulting 429 is misreported as a credential
  failure, and the operator-visible message wrongly recommends
  re-enrollment (umwelt#805, Ariadne's incident report + cost measurement).
  Kepler's ruling (2026-09-18) accepts a persisted per-seat token cache as
  net risk-reducing, under five conditions, ALL mandatory:
    1. File mode 0600 inside a 0700 directory (`_TOKEN_CACHE_DIR`), written
       via temp-file + `os.rename`; every READ re-checks owner + mode and
       refuses (treats as a cache miss, triggering a fresh login) on any
       mismatch — same fail-closed posture the identity key already has.
       Addresses the one genuinely new exposure a cache introduces: on the
       shared host `~/.umwelt` itself is group-writable (0775), so a cache
       file written under the default umask would be readable by every
       co-tenant agent, and `resolve_actor` trusts a valid token over any
       declared identity — cross-agent impersonation for the token's full
       life. The cache directory's own 0700 mode is what isolates it despite
       the laxer parent permissions.
    2. Keyed by (world URL, agent id) — see `_cache_file_path`. A token
       minted for one world is never read back for another (`UMWELT_URL`
       overrides exist).
    3. The three existing client rules below (no `expires_at` parsing; a
       401 invalidates and retries once; fail-closed on a present-but-
       failing credential) all EXTEND to the disk copy: an unverified token
       is never written to disk; a 401 drops the on-disk file too (see
       `AgentSession.invalidate`); a 429 during token acquisition is
       distinguished from a hard auth failure in `get_auth_header`'s own
       control flow and gets one jittered retry (a post-rebind mass
       re-login storm across every hook — all cached tokens die on the same
       rebind — is the case this protects; the #709/#773 error-MESSAGE
       rewording is a separate, not-yet-done issue).
    4. Excluded from every archive/sync of the seat. Investigated for this
       PR (umwelt#805): ENGRAM's own sleep-cycle archive tools
       (`backup_claude_projects.py`, `backup_session_logs.py`,
       `backup_knowledge_db.py`) each walk a fixed, hardcoded source
       directory (`~/.claude/projects/`, `~/.engram/`'s own DB/session
       logs) — none of them walk `~/.umwelt/` at all, so `_TOKEN_CACHE_DIR`
       is not reachable by any of them as of this writing. No code change
       was needed there; re-verify if a future ENGRAM archive tool starts
       walking `$HOME` more broadly.
    5. Long-lived clients (`mcp_server.py`, the bell, the interactive
       `mud.py`/`mud_tui.py` REPL) keep the in-memory-only session — no
       file, `get_auth_header`'s new `cache=` parameter defaults to
       `False` and every existing call site is unchanged. Only `uw` and the
       four plugin hooks (`plugin/hooks/umwelt-*-hook.py`) opt in via
       `cache=True`.
  See `_cache_file_path`/`_ensure_cache_dir`/`_read_cached_token`/
  `_write_cached_token`/`_drop_cached_token` for the mechanism.
  `AgentSession` itself is unchanged for every caller that never opts in —
  it still "lives for the process (mcp_server) or one invocation (uw)" in
  memory; the disk file is an additional, opt-in persistence layer on top,
  not a replacement.
- **No `expires_at` parsing.** The server (`db.get_agent_session`) is the
  authority on token validity and checks six things the client cannot know
  (retirement, `credential_generation` bump, window-close, expiry). The
  client trusts the server's 401 as the validity signal: on a 401 the
  transport invalidates the cache and retries exactly once. This sidesteps
  the #612 `expires_at`-naive-parsing hazard entirely.
- **No key → declared fallback (compatibility mode).** While the backfill
  window is deliberately open, an unenrolled client preserves today's
  declared-identity behavior — but the warning states the session is
  UNVERIFIED. This is a temporary compatibility mode: `ALLOW_UNENROLLED_FALLBACK`
  (default True) gates it; flip to False at window-close and the no-key case
  fails closed too. The False/disabled state is tested so the flip is a
  one-line change with confidence.
- **Key present but auth fails → FAIL CLOSED.** A present-but-failing
  credential is a misconfiguration/security signal, not "unenrolled"; the
  client must NOT silently fall back to declared (that would mask it).
  `get_auth_header` raises `AuthFailure` in this case; the transport seam
  surfaces it (uw: UwError; mcp_server: a 401-shaped error dict). Only the
  no-key (unenrolled) case falls back.
- **Key present but token unverified → FAIL CLOSED.** A key present means
  the agent attempted the explicit credential path; an unverified token
  (server identity unbound, backfill window open) must NOT be cached and
  used as though verified — that silently downgrades the credential path.
  This is a boundary invariant: under normal bind/reset flows the state is
  not reachable (db.bind_identity has no unbind transition; a crashed bind
  leaves either a temp-path key the configured path never sees, or a
  configured key still matching a bound verifier — Mira + Borges, 2026-08-18,
  tracing #613's reachability). But it IS reachable by externally/manual
  states — a key copied to a seat whose server identity is unbound, a DB
  rollback/restore that drops the bind, or a future unbind transition — and
  the defence holds regardless of how the state arises. `get_auth_header`
  raises `AuthFailure`; only the no-key path may legitimately use an
  unverified session. (Mira security review, umwelt #619, 2026-08-18.)
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import ipaddress
import json
import os
import random
import socket
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from urllib.parse import urlsplit

from ._ids import DEFAULT_IDENTITY_KEY_PATH, canonical_agent

# umwelt#678, part of #583 (authenticated transport): the single
# world-URL resolution chokepoint. Before this, 45 hardcoded
# http://…:5003|5005|5011 literals across 22 files were resolved by four
# different, independently-drifting conventions -- Mira's R3 names the
# shared risk directly: every one of those is a silent-fallback surface,
# since a miss at cutover doesn't error, it quietly keeps speaking
# plaintext, and if the old port still listens it looks like it worked.
DEFAULT_WORLD_URL = "http://localhost:5003"
_CONFIG_PATH = Path.home() / ".umwelt" / "config.json"

# umwelt#805 (Kepler's security ruling, 2026-09-18, condition #1): the
# persisted-token-cache directory. Deliberately its OWN 0700 subdirectory of
# ~/.umwelt rather than a file dropped straight into ~/.umwelt itself --
# ~/.umwelt is group-writable (0775) on the shared host, so a bare file
# there would inherit that laxer exposure regardless of the file's own
# mode. A 0700 directory is opaque to every non-owner regardless of the
# parent's mode. See `_ensure_cache_dir`.
_TOKEN_CACHE_DIR = _CONFIG_PATH.parent / "token_cache"


class WorldUrlError(RuntimeError):
    """`resolve_world_url()` failed closed (umwelt#699, part of #583): a
    malformed/unusable config, or a plaintext URL to a non-loopback host.
    Both are refused rather than silently downgraded to plaintext -- see
    the function's own docstring for the precedence chain and the
    loopback rule. Mirrors `tls_context.TLSConfigError`'s posture: raise
    uncaught, let the CLI entry point fail loudly with a traceback rather
    than quietly serving/consuming plaintext.
    """


def _is_loopback_host(host: str | None) -> bool:
    """True iff `host` is loopback, decided from parsed IP literals plus
    an explicit `localhost` allowance -- NEVER by resolving an arbitrary
    hostname's DNS answer (umwelt#699 §3, flagged as not-yet-designed
    there: a name that merely *resolves* to loopback must not count,
    since that resolution could be attacker-controlled). This is the
    stated implementation choice: parsed literals + `localhost`, not name
    resolution.
    """
    if not host:
        return False
    host = host.strip().lower().strip("[]")  # urlsplit keeps IPv6 brackets off .hostname, but be defensive
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse_plaintext_non_loopback(url: str) -> str:
    """umwelt#699 §3: refuse a plaintext (`http://`) URL whose host is not
    loopback; allow plaintext to loopback. The risk is a property of the
    *destination*, not the scheme -- plaintext to loopback never reaches a
    wire, plaintext to a LAN/tailnet address does. Applied to whatever URL
    `resolve_world_url` is about to return, from any precedence level, so
    the four automatic plugin hooks (loopback callers on every seat whose
    config was readable at spec time -- #699's own census) are unaffected,
    while a `--url`/env/config value pointing at a real network address
    over `http://` is refused.
    """
    parsed = urlsplit(url)
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise WorldUrlError(
            f"refusing plaintext http:// world URL to a non-loopback host: {url!r} "
            "-- umwelt#699: after the TLS cutover, a LAN/tailnet address must "
            "use https://. Plaintext to localhost/127.0.0.1/::1 is still allowed."
        )
    return url


def resolve_world_url(explicit: str | None = None) -> str:
    """The one place every umwelt client resolves the world server's base
    URL, in one fixed precedence order:

        explicit argument > UMWELT_URL env > ~/.umwelt/config.json's
        world_url > DEFAULT_WORLD_URL

    `explicit` is for a caller with its own override already in hand
    (e.g. an argparse `--url` the user passed) -- pass `None` (the
    default) to fall through to the env/config/default chain; an empty
    or whitespace-only string is treated the same as `None`, never
    returned as-is.

    **Deliberate behaviour change for the four plugin hooks** (umwelt#678):
    they previously read config.json with NO env layer at all, falling
    back straight to a module-local `DEFAULT_WORLD_URL` constant -- under
    this unified order, a seat with `UMWELT_URL` set for its CLI now has
    that value override its hooks' config too. That is the correct
    outcome (one precedence everywhere beats four independently-drifting
    ones), named here rather than left to be discovered, per the design
    spec's own explicit callout.

    **Absent vs malformed config (umwelt#699 §2) -- deliberately NOT the
    same tolerance any more.** An absent config file (or one unreadable
    for an I/O reason -- missing, permission-denied, a directory where a
    file was expected) still falls through to the next precedence level,
    exactly as before: that is the genuine first-run case, unchanged.
    But a config file that IS present and is malformed -- invalid JSON,
    a top-level shape that isn't an object, or a `world_url` key present
    with a value that isn't a usable non-empty string -- now **raises**
    `WorldUrlError` instead of silently falling through. Before #699 this
    fell through like an absent config; after cutover that tolerance is
    itself the defect Mira's R3 names: a typo in `~/.umwelt/config.json`
    would otherwise silently return every client on that seat to a
    plaintext default. A `world_url` key that is simply absent from an
    otherwise-valid config dict is unaffected -- that is "not configured
    yet", not "configured wrong", and still falls through.

    **Plaintext-to-non-loopback refusal (umwelt#699 §3).** Whatever URL
    this function is about to return -- from any precedence level,
    including `DEFAULT_WORLD_URL` itself -- is passed through
    `_refuse_plaintext_non_loopback`: a `http://` URL to a host that
    isn't loopback raises `WorldUrlError`. `https://` URLs are never
    checked here (TLS is #688's concern, not this function's). See that
    helper's docstring for the loopback rule and why it does not perform
    DNS resolution.
    """
    if explicit and explicit.strip():
        return _refuse_plaintext_non_loopback(explicit.strip())
    env = os.environ.get("UMWELT_URL")
    if env and env.strip():
        return _refuse_plaintext_non_loopback(env.strip())
    try:
        raw = _CONFIG_PATH.read_text()
    except OSError:
        config: object = {}
    else:
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as e:
            raise WorldUrlError(
                f"{_CONFIG_PATH} exists but is not valid JSON ({e}) -- "
                "umwelt#699: a present-but-broken config is refused rather "
                "than silently treated as absent."
            ) from e
    if not isinstance(config, dict):
        raise WorldUrlError(
            f"{_CONFIG_PATH} exists and is valid JSON but its top level is "
            f"a {type(config).__name__}, not an object -- umwelt#699: a "
            "present-but-broken config is refused rather than silently "
            "treated as absent."
        )
    if "world_url" in config:
        world_url = config["world_url"]
        if not (isinstance(world_url, str) and world_url.strip()):
            raise WorldUrlError(
                f"{_CONFIG_PATH}'s world_url is present but not a usable "
                f"string: {world_url!r} -- umwelt#699: a present-but-broken "
                "config is refused rather than silently treated as absent."
            )
        return _refuse_plaintext_non_loopback(world_url.strip())
    return _refuse_plaintext_non_loopback(DEFAULT_WORLD_URL)


class AuthFailure(Exception):
    """A key was present but authentication failed — fail closed.

    Raised (not returned-None) so the transport seam can distinguish
    "no key file — fall back to declared" (returns ``{}``) from "key present
    but login failed — do NOT fall back" (raises this). The latter is a
    misconfiguration/security signal; silently acting as declared would mask
    it. (Mira, umwelt #495 security ruling, 2026-08-18.)
    """


# Compatibility-mode flag — see module docstring. Default True while the
# backfill window is deliberately open. At window-close, flip to False (or
# tie to the server's window status): the no-key case then fails closed,
# requiring enrollment. The disabled state is tested (test_client_auth.py).
ALLOW_UNENROLLED_FALLBACK = True


# ── Persisted token cache (umwelt#805, Kepler's 2026-09-18 security ruling) ──
# Opt-in disk layer beneath AgentSession, for single-shot clients only (the
# module docstring's condition list has the full rationale). Every function
# here fails SILENTLY toward "no cache" on any unsafe/unreadable/unwritable
# condition — a cache miss just means "acquire fresh", the same posture a
# cold cache has on first run. Never raises into a caller's control flow.

def _cache_file_path(world_url: str, agent_id: str) -> Path:
    """The on-disk cache-file path for one (world URL, agent id) pair.

    Condition #2: a token minted for one world must never be read back for
    another. The filename is a hash of both the URL and the canonicalized
    agent id (``_ids.canonical_agent`` — the same fold the server itself
    applies) so tokens for different worlds/agents can't collide on disk;
    the file's own contents redundantly carry ``world_url`` too (checked on
    read) as defense-in-depth against a hash collision or a future keying
    mistake, not because a collision is expected from sha256.
    """
    key = f"{world_url}\n{canonical_agent(agent_id)}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return _TOKEN_CACHE_DIR / f"{digest}.json"


def _ensure_cache_dir() -> Path | None:
    """Create (if needed) and verify the 0700 cache directory.

    Returns the directory on success, ``None`` if it cannot be made/kept
    safe -- callers treat that exactly like a cache miss (fine: the cache is
    strictly an optimization, `acquire_token`'s normal round-trip always
    still works).

    Symlink-safety note (found in review, PR #828): ``Path.mkdir(mode=...,
    exist_ok=True)`` decides "already exists" via ``is_dir()``, which
    follows symlinks, and plain ``os.chmod`` has no ``lchmod`` on Linux and
    also follows symlinks -- so the original implementation would silently
    chmod the TARGET of a symlink planted at the cache-dir path to 0700
    before its own (symlink-safe) ``lstat`` check caught it. ``os.mkdir``
    itself never follows a symlink for creation (a symlink entry already
    "exists" there, so it raises ``FileExistsError`` rather than touching
    the target); the fix is to do everything else -- the mode assertion and
    the stat used for verification -- through an fd opened with
    ``O_NOFOLLOW``, which refuses outright (``OSError``, typically
    ``ELOOP``) rather than resolving through a symlink. This mirrors the
    house pattern in `bind_socket.py`'s `_validate_socket_parent_safe`
    (Borges's PR #506 ruling) for the same directory-squatting class, scaled
    down to a graceful cache-miss instead of a hard startup refusal, since
    this path is an optional optimization rather than a required bind.

    Defense-in-depth, not the primary fix: also refuses if the cache dir's
    PARENT (``~/.umwelt``) is group- or world-writable, since a co-tenant
    with write access there can delete and re-plant the cache-dir entry at
    will regardless of the entry's own mode -- this only lowers exposure on
    a laxly-permissioned host; the O_NOFOLLOW fd is what actually closes
    the demonstrated symlink-follow.
    """
    d = _TOKEN_CACHE_DIR
    try:
        pst = d.parent.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(pst.st_mode) or (pst.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        return None
    try:
        os.mkdir(d, mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    try:
        fd = os.open(d, os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        # Found in review round 2 (PR #828): fchmod/fstat were left
        # unguarded here, unlike every other OS call in this function --
        # a foreign-owned or otherwise-uncooperative directory (exactly
        # the case the ownership check just below exists to catch) can
        # raise PermissionError here, which propagated uncaught all the
        # way to a hook's outer `except Exception: pass`, silently
        # no-op'ing the ENTIRE hook invocation rather than degrading to a
        # graceful cache-miss as this function's own docstring promises.
        os.fchmod(fd, 0o700)
        st = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if not stat.S_ISDIR(st.st_mode):
        return None
    if st.st_uid != os.getuid():
        return None
    if stat.S_IMODE(st.st_mode) != 0o700:
        return None
    return d


def _read_cached_token(world_url: str, agent_id: str) -> dict | None:
    """Read a persisted token for (world_url, agent_id), or ``None`` on any
    miss/invalid/unsafe condition.

    Condition #1: re-checks owner + mode on EVERY read and refuses (treats
    as a miss, so the caller falls through to a fresh login) on any
    mismatch -- a file that fails this check could have been tampered with,
    or written under a laxer umask by a bug, and is never trusted either
    way. ``lstat`` (not ``stat``) so a symlink planted at the cache path is
    refused as "not a regular file" rather than followed.
    """
    if _ensure_cache_dir() is None:
        return None
    path = _cache_file_path(world_url, agent_id)
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_uid != os.getuid():
        return None
    if stat.S_IMODE(st.st_mode) != 0o600:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    state = data.get("state")
    # Condition #3, extended to disk: an unverified token is never trusted
    # from the cache even if one somehow got written (defense in depth --
    # `_write_cached_token` also refuses to write one).
    if not token or state != "verified":
        return None
    if data.get("world_url") != world_url:
        return None  # defense-in-depth cross-check, see _cache_file_path
    return {"token": token, "state": state}


def _write_cached_token(world_url: str, agent_id: str, token: str, state: str) -> None:
    """Persist a verified token to disk at 0600 inside the 0700 cache dir.

    Condition #1: temp-file + ``os.rename`` -- a reader (this process or a
    concurrent one) only ever sees either the old complete file or the new
    complete file, never a partial write. Condition #3: refuses to write a
    non-``verified`` token (defense in depth -- callers are only meant to
    reach this after `get_auth_header`'s own verified-state check, same as
    the in-memory `session.token` assignment right next to this call).
    """
    if state != "verified" or not token:
        return
    d = _ensure_cache_dir()
    if d is None:
        return  # cache unavailable -- not fatal, the token still works in-memory
    path = _cache_file_path(world_url, agent_id)
    payload = json.dumps({"world_url": world_url, "agent_id": agent_id,
                           "token": token, "state": state})
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(d), prefix=".tmp-", suffix=".json")
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.rename(tmp_name, path)
        tmp_name = None
    except OSError:
        pass  # cache write failure is not fatal -- token still works in-memory
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _drop_cached_token(world_url: str, agent_id: str) -> None:
    """Delete the on-disk cache file, if any.

    Condition #3, extended to disk: the existing "a 401 invalidates the
    cache and retries once" rule now also drops the persisted copy -- see
    `AgentSession.invalidate`, which calls this when the session was
    populated via the cache-aware path (`get_auth_header(..., cache=True)`).
    """
    try:
        _cache_file_path(world_url, agent_id).unlink()
    except OSError:
        pass


def _sleep_jittered(base_seconds: float = 0.5) -> None:
    """Sleep a small random jittered interval before a single 429 retry
    during token acquisition (condition #3: a post-rebind mass re-login
    storm -- every cached token dies on the same rebind, so every
    cache-aware client re-logins in the same window -- is the case this
    exists for). A separate, monkeypatchable seam (mirrors `_http_post`) so
    tests exercise the retry branch without actually waiting.
    """
    time.sleep(base_seconds * random.uniform(0.5, 1.5))


class AgentSession:
    """In-memory cached session token for one agent against one world.

    Never persisted BY DEFAULT — ``token`` is the bearer string; ``state``
    is the server-reported session state (``"verified"`` | ``"unverified"``).
    Long-lived callers (mcp_server, the bell, mud.py) never touch the disk
    cache: they call `get_auth_header` without ``cache=True``, so
    ``_cache_key`` here stays ``None`` and `invalidate` behaves exactly as
    it always has (in-memory only). Only a caller that opts into
    ``cache=True`` (umwelt#805: `uw`, the four plugin hooks) populates
    ``_cache_key``, and only then does `invalidate` also drop the on-disk
    file -- see `get_auth_header`'s own docstring for the full matrix.
    """

    def __init__(self) -> None:
        self.token: str | None = None
        self.state: str | None = None
        self._cache_key: tuple[str, str] | None = None

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def invalidate(self) -> None:
        self.token = None
        self.state = None
        if self._cache_key is not None:
            world_url, agent_id = self._cache_key
            self._cache_key = None
            _drop_cached_token(world_url, agent_id)


_unenrolled_warned = False


def _warn_unenrolled_once() -> None:
    """Warn (once per process) that the session is unverified — backfill
    compatibility mode, not a verified login. Explicit per the security
    ruling: the fallback must not be silent about its unverified nature."""
    global _unenrolled_warned
    if _unenrolled_warned:
        return
    _unenrolled_warned = True
    print(
        "⚠️ umwelt: no identity key found — using declared identity "
        "(UNVERIFIED session, backfill compatibility mode). "
        "Run tools/bind_client_agent.py to enroll and get a verified session.",
        file=sys.stderr,
    )


def _key_path() -> str | None:
    """The identity-key path, or ``None`` if none is configured/present.

    Precedence: ``UMWELT_IDENTITY_KEY`` env var → ``DEFAULT_IDENTITY_KEY_PATH``.
    Returns ``None`` when the resolved path does not exist (unenrolled) —
    callers use that to choose between declared-fallback and acquire-token.
    """
    env = os.environ.get("UMWELT_IDENTITY_KEY", "").strip()
    path = env or DEFAULT_IDENTITY_KEY_PATH
    if path and os.path.exists(path):
        return path
    return None


def _load_private_key(path: str):
    """Load an ed25519 private key from disk.

    Disk format (matches ``bind_client_agent._stage_private_key``): base64 of
    raw private bytes, single line. Read with
    ``Ed25519PrivateKey.from_private_bytes(base64.b64decode(text.strip()))``.

    ``cryptography`` is LAZY-imported here — see the module docstring for the
    no-key→no-regression invariant this preserves.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    with open(path, encoding="ascii") as fh:
        text = fh.read()
    raw = base64.b64decode(text.strip())
    return Ed25519PrivateKey.from_private_bytes(raw)


# ``socket.timeout`` is an alias of ``TimeoutError`` only from Python 3.10; on
# older interpreters it is a separate OSError subclass, so name both.
_TIMEOUT_ERRORS = (TimeoutError, socket.timeout)


class _DeadlineMixin:
    """Socket mixin: every ``recv_into`` is first re-armed to the time left
    before ``_deadline`` (a ``time.monotonic()`` value).

    ``http.client`` reads the status line, the headers and the body through
    ``sock.makefile("rb")``, whose reader calls ``recv_into`` on THIS object
    (``SocketIO.readinto``), so overriding it bounds all three phases with one
    mechanism. Capping each read at the REMAINING budget, not at the original
    ``timeout``, is the load-bearing part: a read capped at the full ``timeout``
    can start with 0.5 s left and still block for 2 s. The re-arm lives on the
    socket object itself, so it does not matter that ``HTTPConnection`` drops
    its own ``sock`` reference once a ``Connection: close`` response is handed
    to the response object (alpha #3143, the same class).
    """

    _deadline: float = 0.0

    def recv_into(self, buffer, nbytes=0, flags=0):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline exceeded")
        self.settimeout(remaining)
        return super().recv_into(buffer, nbytes, flags)


class _DeadlineSocket(_DeadlineMixin, socket.socket):
    pass


class _DeadlineSSLSocket(_DeadlineMixin, ssl.SSLSocket):
    pass


def _deadline_error(
    phase: str, elapsed: float, budget: float
) -> urllib.error.URLError:
    """The one error a blown deadline raises: ``URLError(TimeoutError)``.

    Same shape ``urlopen`` itself raises for a CONNECT timeout, and an
    ``OSError`` (URLError subclasses it), so it lands in every caller's
    existing "world unreachable" clause -- see the caller table in the PR.

    ``phase`` names where the budget ran out (umwelt#1006), so a stall can be
    classified from the one line the bell prints: ``connect``, ``tls`` (the
    handshake), ``send`` (the request head/body), ``read-headers`` (status line
    and headers) or ``read-body``. ``elapsed`` and ``budget`` are seconds, from
    this attempt's start to now, and the budget THIS ATTEMPT was given. On the
    certifi retry (``urlopen_with_cert_fallback``) that is the LEFTOVER of the
    caller's timeout, not the whole of it, hence "this attempt's" in the message.
    """
    return urllib.error.URLError(
        TimeoutError(
            "umwelt request exceeded its end-to-end deadline "
            f"during {phase} ({elapsed:.2f}s of this attempt's {budget:.2f}s)"
        )
    )


def _connect_within(host: str, port: int, deadline: float) -> socket.socket:
    """``socket.create_connection`` that cannot outlive ``deadline``.

    The stdlib version gives EVERY resolved address a fresh ``timeout``, so a
    name resolving to several dead addresses blocks for N x timeout. Here the
    remaining budget is split across the addresses still to try (a dead first
    address must not starve a working second one, e.g. an unreachable IPv6
    candidate listed ahead of the IPv4 one), and no attempt runs past the
    deadline.

    ``getaddrinfo`` itself is a blocking libc call and is NOT bounded by this
    (see ``urlopen_with_cert_fallback``'s docstring).
    """
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    last_exc: OSError | None = None
    for i, (af, socktype, proto, _canon, sa) in enumerate(infos):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline exceeded")
        sock = _DeadlineSocket(af, socktype, proto)
        sock._deadline = deadline
        try:
            sock.settimeout(remaining / (len(infos) - i))
            sock.connect(sa)
            return sock
        except OSError as exc:
            sock.close()
            last_exc = exc
    if last_exc is None:
        raise OSError(f"getaddrinfo returned no addresses for {host!r}")
    raise last_exc


def _open_once(req: urllib.request.Request, deadline: float, context=None):
    """ONE request attempt against an absolute ``time.monotonic()`` deadline.

    Drives ``http.client`` directly (``urlopen`` exposes no public way to
    re-arm a socket's timeout between reads). Returns a
    ``urllib.response.addinfourl`` over the fully-buffered body for a 2xx;
    raises ``urllib.error.HTTPError`` (readable body) for any other status and
    ``urllib.error.URLError`` for a connection-level failure or a blown
    deadline. The whole body is read INSIDE the deadline, so the returned
    object's ``read()`` can never block.

    This is also the single seam the unit tests substitute to exercise the
    cert-retry logic in ``urlopen_with_cert_fallback``.
    """
    parts = urlsplit(req.full_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise urllib.error.URLError(f"unsupported url: {req.full_url!r}")
    https = parts.scheme == "https"
    port = parts.port or (443 if https else 80)

    # Same header NAMES as urlopen put on the wire: Request.header_items()
    # capitalises ("Content-type"), and urllib's do_open title-cases on send
    # ("Content-Type"), so do the same here.
    headers = {k.title(): v for k, v in req.header_items()}
    lowered = {k.lower() for k in headers}
    # The defaults urlopen added, so a server or proxy that keys on them sees
    # no change.
    if "user-agent" not in lowered:
        headers["User-Agent"] = (
            f"Python-urllib/{sys.version_info[0]}.{sys.version_info[1]}"
        )
    if req.data is not None and "content-type" not in lowered:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Connection"] = "close"

    started = time.monotonic()
    budget = deadline - started

    def blown(phase):
        return _deadline_error(phase, time.monotonic() - started, budget)

    sock = None
    try:
        phase = "connect"
        try:
            sock = _connect_within(parts.hostname, port, deadline)
            if https:
                phase = "tls"
                ctx = context if context is not None else ssl.create_default_context()
                ctx.sslsocket_class = _DeadlineSSLSocket
                sock = ctx.wrap_socket(
                    sock, server_hostname=parts.hostname, do_handshake_on_connect=False
                )
                sock._deadline = deadline
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("deadline exceeded")
                sock.settimeout(remaining)
                sock.do_handshake()
        except _TIMEOUT_ERRORS as exc:
            raise blown(phase) from exc
        except OSError as exc:  # refused, gaierror, ssl.SSLCertVerificationError, ...
            raise urllib.error.URLError(exc) from exc

        conn = http.client.HTTPConnection(parts.hostname, port)
        conn.sock = sock  # already connected (and TLS-wrapped): request() skips connect()
        try:
            try:
                conn.request(
                    req.get_method(), req.selector, body=req.data, headers=headers
                )
            except _TIMEOUT_ERRORS as exc:
                raise blown("send") from exc
            except OSError as exc:
                # A send-phase failure (peer reset mid-body, broken pipe) is a
                # URLError(reason=<the OSError>), exactly as urlopen raised it.
                # Only the SEND is wrapped: errors while reading the response
                # propagate raw, as they did under urlopen.
                raise urllib.error.URLError(exc) from exc
            phase = "read-headers"
            resp = conn.getresponse()
            phase = "read-body"
            body = resp.read()
        except _TIMEOUT_ERRORS as exc:
            raise blown(phase) from exc
        finally:
            conn.close()
        fp = io.BytesIO(body)
        if 200 <= resp.status < 300:
            out = urllib.response.addinfourl(fp, resp.msg, req.full_url, resp.status)
            out.reason = resp.reason
            return out
        raise urllib.error.HTTPError(req.full_url, resp.status, resp.reason, resp.msg, fp)
    finally:
        if sock is not None:
            sock.close()


def urlopen_with_cert_fallback(req: urllib.request.Request, timeout: int):
    """``urllib.request.urlopen`` replacement with a same-request
    TLS-verification retry and an END-TO-END deadline.

    Shared by every umwelt HTTP client (``client_auth._http_post``,
    ``mcp_server._request_once``, the bell, the plugin hooks, the backfill
    tools) so the fallback and the deadline live in exactly one place.

    END-TO-END DEADLINE (umwelt#977). ``timeout`` is a wall-clock budget for
    the WHOLE call: connect, TLS handshake, request, status line, headers and
    the full body, plus the certifi retry (which shares the same budget).
    ``urlopen(req, timeout=N)`` applies N to each socket operation instead, so
    a server that trickles one byte every few seconds, or a name that resolves
    to several dead addresses, could block a caller far past N (measured by
    Aleph: 258.9 s with N=10). A blown deadline raises
    ``urllib.error.URLError(TimeoutError(...))``.

    NOT covered: DNS resolution. ``socket.getaddrinfo`` is a blocking libc
    call with no timeout parameter, so a hung resolver can still hold the
    call past ``timeout`` (the connect and everything after it is bounded;
    the lookup before it is not). Bounding it would need a helper thread or
    process per call, which this function does not do.

    Interface contract (what callers rely on, preserved from ``urlopen``):
    on 2xx returns a context-manager response with ``.read()``, ``.status`` /
    ``.getcode()``, ``.headers`` and ``.url`` over the already-buffered body;
    on any other status raises ``urllib.error.HTTPError`` whose ``.read()``
    returns the body; connection-level failures raise ``URLError`` with the
    original ``OSError`` as ``.reason``. Two deliberate changes from
    ``urlopen``: redirects are NOT followed, and ``*_proxy`` environment
    variables are NOT honoured (see the PR notes for why neither matters for
    the world's routes).

    TLS verification fallback (2026-08-30): some macOS Python.org installs
    ship a default OpenSSL cert bundle that does not include the intermediate
    for a VPN-provisioned (Let's Encrypt) certificate chain, even though the
    macOS system keychain trusts it fine. The underlying
    ``ssl.SSLCertVerificationError`` reaches the caller wrapped in a
    ``urllib.error.URLError`` (the raw SSL exception is never raised
    directly), so this checks ``URLError.reason`` rather than catching
    ``ssl.SSLCertVerificationError`` itself -- catching the latter is a
    plausible-looking mistake that never fires, verified live against this
    exact failure (measured 2026-08-30). On that specific reason -- never any
    other ``URLError`` cause (DNS, connection refused, timeout) -- retry once
    against ``certifi``'s bundle if importable, with whatever is LEFT of the
    original deadline (never a fresh one). This never touches the default
    path for a caller whose verification already succeeds (plaintext-only
    agents, or any Python install with a working system cert store), and
    re-raises the original error for every other failure shape, including a
    missing ``certifi``.

    Caller owns ``HTTPError`` / other ``URLError`` handling; this only adds
    the one extra branch.
    """
    deadline = time.monotonic() + timeout
    try:
        return _open_once(req, deadline)
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ssl.SSLCertVerificationError):
            raise
        try:
            import certifi
        except ImportError:
            raise e from None  # no certifi -- surface the original URLError, not ImportError
        ctx = ssl.create_default_context(cafile=certifi.where())
        return _open_once(req, deadline, ctx)


def _http_post(url: str, payload: dict, timeout: int = 15) -> dict:
    """urllib POST → ``{_status, **body}``.

    Monkeypatchable for in-process tests: replace this to route through
    ``app.test_client()`` instead of a real socket, avoiding the host-global
    bind-socket hazard entirely (Ariadne, 2026-08-18). Raises on a
    connection-level failure; an HTTP error *response* (4xx/5xx with a body)
    is returned normally so callers can branch on ``_status``.
    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urlopen_with_cert_fallback(req, timeout) as r:
            body = r.read().decode()
            return {"_status": r.status, **(json.loads(body) if body else {})}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"_status": e.code, **(json.loads(body) if body else {})}


def _acquire_token_raw(
    world_url: str, agent_id: str, key_path: str
) -> tuple[dict | None, int | None]:
    """Run the challenge→sign→login round-trip; returns
    ``(token_dict_or_None, last_http_status)``.

    ``last_http_status`` is the HTTP status of the most recent HTTP call this
    attempt reached (the challenge call, or the login call once the
    challenge succeeded), or ``None`` if no HTTP call was even reached (e.g.
    the key failed to load). Split out from `acquire_token` (umwelt#805) so
    a cache-aware caller can distinguish a 429 (rate limited — the ruling's
    condition #3 case: a post-rebind mass re-login storm) from any other
    failure shape, without changing `acquire_token`'s own public contract —
    every existing caller of `acquire_token` is unaffected by this refactor.
    """
    # 1. load key
    try:
        priv = _load_private_key(key_path)
    except Exception:
        return None, None
    # 2. challenge
    try:
        chal = _http_post(f"{world_url}/umwelt/login/challenge", {"agent_id": agent_id})
    except Exception:
        return None, None
    chal_status = chal.get("_status")
    if chal_status != 200:
        return None, chal_status
    challenge = chal.get("challenge")
    if not challenge:
        return None, chal_status
    # 3. sign
    try:
        sig = priv.sign(challenge.encode("utf-8"))
        signature = base64.b64encode(sig).decode("ascii")
    except Exception:
        return None, chal_status
    # 4. login
    try:
        resp = _http_post(
            f"{world_url}/umwelt/login",
            {"agent_id": agent_id, "challenge": challenge, "signature": signature},
        )
    except Exception:
        return None, chal_status
    login_status = resp.get("_status")
    if login_status != 200:
        return None, login_status
    token = resp.get("token")
    if not token:
        return None, login_status
    return {
        "token": token,
        "state": resp.get("state"),
        "expires_at": resp.get("expires_at"),
    }, login_status


def acquire_token(world_url: str, agent_id: str, key_path: str) -> dict | None:
    """Run the challenge→sign→login round-trip.

    Returns ``{"token", "state", "expires_at"}`` on success (the session may
    be ``verified`` OR ``unverified`` — an unbound identity with the window
    still open mints an unverified token, which the server accepts). Returns
    ``None`` on any failure (network, 401, bad key, malformed response). Never
    raises into the caller's transport path — the caller distinguishes the
    failure modes at a higher layer (``get_auth_header``): no-key → declared
    fallback; key-present-but-``None`` → fail closed.
    """
    tok, _status = _acquire_token_raw(world_url, agent_id, key_path)
    return tok


def get_auth_header(
    world_url: str,
    agent_id: str,
    session: AgentSession,
    *,
    cache: bool = False,
) -> dict[str, str]:
    """Return the ``Authorization`` header dict to merge into request headers.

    Decision matrix (Mira security ruling, 2026-08-18; extended by Kepler's
    2026-09-18 security ruling, umwelt#805, for the ``cache=True`` case):

    - cached token present (in-memory, ``session.token``) → return it.
    - ``cache=True`` and no in-memory token → try the on-disk cache
      (condition #1: re-verified on every read) for (world_url, agent_id);
      on a hit, populate the session from it and return the header — no HTTP
      round trip. A miss (absent, unsafe mode/owner, unverified, wrong URL)
      falls through to the matrix below exactly as a cold cache would.
    - no key + ``ALLOW_UNENROLLED_FALLBACK`` → ``{}`` (declared fallback;
      warns once the session is unverified — backfill compat mode).
    - no key + fallback disabled → raise ``AuthFailure`` (enrollment required).
    - key + acquire hits 429 (only when ``cache=True``) → ONE jittered retry
      (condition #3: distinguishes a rate-limit throttle from a genuine
      credential fault — the case Kepler's ruling names is a post-rebind
      mass re-login storm, where every cache-aware client's token dies on
      the same rebind). Still 429 after the retry (or a single 429 attempt
      when ``cache=False``, which does not retry) raises a
      throttling-specific ``AuthFailure`` — no credential/re-enrollment
      language — rather than the generic message below. Any OTHER failure
      shape still gets that generic message; only the 429-vs-everything-else
      split is implemented here. Distinguishing failure causes further than
      that (the full multi-way error taxonomy) is umwelt#709/#773's
      separate, not-yet-done job.
    - key + acquire succeeds + state verified → cache in-memory + return
      header; if ``cache=True``, ALSO persist to disk (condition #1/#3) and
      remember the (world_url, agent_id) key on the session so a later
      ``session.invalidate()`` (the 401 path) drops the disk copy too.
    - key + acquire succeeds + state unverified → raise ``AuthFailure``
      (FAIL CLOSED; a key present means the explicit credential path was
      attempted — an unverified token means the identity is unbound while
      the backfill window is open. Not reachable under normal bind/reset
      flows, but reachable by externally/manual states — a copied key, a DB
      rollback/restore that drops the bind, a future unbind transition — and
      the defence is a boundary invariant regardless. Caching it (in memory
      OR on disk) would silently downgrade the credential path. Only the
      no-key path may legitimately use an unverified session. Mira security
      review, umwelt #619, 2026-08-18.).
    - key + acquire fails (non-429, or still failing after the 429 retry) →
      raise ``AuthFailure`` (FAIL CLOSED; a present-but-failing credential is
      NOT "unenrolled" — do not fall back to declared, that masks the
      failure).

    Never parses ``expires_at`` — the server's 401 is the validity signal,
    for the in-memory session and (condition #3) for the on-disk cache
    alike: `AgentSession.invalidate()` drops the disk file on a 401, exactly
    as it always cleared the in-memory token.
    """
    if session.token:
        return {"Authorization": f"Bearer {session.token}"}
    if cache:
        cached = _read_cached_token(world_url, agent_id)
        if cached is not None:
            session.token = cached["token"]
            session.state = cached["state"]
            session._cache_key = (world_url, agent_id)
            return {"Authorization": f"Bearer {session.token}"}
    kp = _key_path()
    if kp is None:
        if ALLOW_UNENROLLED_FALLBACK:
            _warn_unenrolled_once()
            return {}
        raise AuthFailure(
            "no identity key found and unenrolled fallback is disabled "
            "(window closed) — run tools/bind_client_agent.py to enroll"
        )
    tok, status = _acquire_token_raw(world_url, agent_id, kp)
    if tok is None and status == 429 and cache:
        # Condition #3: a 429 during token acquisition is throttling, not a
        # credential fault. One jittered retry covers the post-rebind mass
        # re-login storm this ruling names; still-429 (or any other
        # failure) after the retry falls through below.
        _sleep_jittered()
        tok, status = _acquire_token_raw(world_url, agent_id, kp)
    if tok is None:
        if status == 429:
            # Found in review (Mira's security read, PR #828): the pre-fix
            # code discarded `status` here and always raised the
            # credential-diagnosis message below, even when the FINAL
            # attempt (post-retry, or the only attempt when cache=False)
            # was itself a 429 -- exactly the misdiagnosis umwelt#805 was
            # opened to stop: a 0.25-0.75s jittered retry cannot clear an
            # already-exhausted 60s rate-limit bucket, so a persistent 429
            # reaching here is a real, non-theoretical case, not just the
            # cache=False path. This message only distinguishes throttling
            # from a credential fault (condition #3's explicit requirement)
            # -- it does not attempt the full eight-way error taxonomy
            # umwelt#709/#773 owns.
            raise AuthFailure(
                f"login for agent {agent_id!r} at {world_url} is being "
                f"throttled (HTTP 429) -- this is NOT a credential problem, "
                f"do not re-enroll; the per-agent login rate limit is "
                f"exhausted, retry once it clears"
            )
        raise AuthFailure(
            f"identity key present at {kp} but authentication failed for "
            f"agent {agent_id!r} — not falling back to declared identity "
            f"(check that the key matches the bound identity)"
        )
    # Mira security review (umwelt #619, 2026-08-18): a key present means the
    # agent attempted the explicit credential path. An unverified token here
    # means the server identity is unbound while the backfill window is open.
    # Not reachable under normal bind/reset flows (db.bind_identity has no
    # unbind transition; a crashed bind leaves a temp-path key the configured
    # path never sees, or a configured key still matching a bound verifier),
    # but reachable by externally/manual states — a copied key, a DB
    # rollback/restore that drops the bind, a future unbind transition — and
    # the defence is a boundary invariant regardless of how the state arises.
    # Caching+proceeding would silently downgrade the explicit credential
    # path to an unverified session. Fail closed instead — same class as
    # key-present-but-auth-fail. Only the no-key path may legitimately use an
    # unverified session (the declared compatibility fallback above).
    if tok.get("state") != "verified":
        raise AuthFailure(
            f"identity key present at {kp} but the server returned an "
            f"unverified session for agent {agent_id!r} (identity unbound + "
            f"backfill window open) — not falling back to declared identity "
            f"and not caching an unverified token; run "
            f"tools/bind_client_agent.py to complete enrollment"
        )
    session.token = tok["token"]
    session.state = tok.get("state")
    if cache:
        session._cache_key = (world_url, agent_id)
        _write_cached_token(world_url, agent_id, session.token, session.state)
    return {"Authorization": f"Bearer {session.token}"}