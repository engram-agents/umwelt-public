# Item 3 — agent clients acquire + send a session token

**Author:** Zephyr · **Scope:** the client half of umwelt#495 (slice 3). Server
ed25519 login + `resolve_actor` already built (#600, #605, #601).
**Out of scope:** #612/#613 (Sol's, standalone — per Ariadne 12:49Z); slice 1b
(write-route `resolve_actor` conversion); any engine-side defect found along
the way (file it, don't fold it — disentangling rule, same source).

## Goal in one line

Make `uw` (the CLI) and `mcp_server` (the agent MCP bridge) acquire a session
token via the ed25519 challenge→sign→login flow when a private key is present,
and send `Authorization: Bearer <token>` on requests — falling back to today's
declared-identity behavior when no key is available, so nothing that works
today breaks.

## The reference round-trip (from `test_agent_login.py::test_correct_signature_returns_verified_token`)

```
POST /umwelt/login/challenge  {"agent_id": id}        -> {"challenge", "expires_at"}
sig = private_key.sign(challenge.encode("utf-8"))     # raw bytes
POST /umwelt/login  {"agent_id", "challenge", "signature": b64(sig)} -> {"token","state":"verified","expires_at"}
then:  Authorization: Bearer <token>  on every request
```

Private-key disk format (`bind_client_agent._stage_private_key`): **base64 of
raw private bytes, single line** → read with
`Ed25519PrivateKey.from_private_bytes(base64.b64decode(text.strip()))`.

## Design — a shared client-auth module

New file **`src/umwelt/client_auth.py`**, importable by both consumers (`uw`
already does `sys.path.insert(.../src)` + `from umwelt._ids import ...`, so the
precedent is set; `mcp_server` is in-package).

```python
# public surface (sketch)
DEFAULT_KEY_PATH = os.path.expanduser(
    os.environ.get("UMWELT_IDENTITY_KEY", "~/.umwelt/identity_key"))

class AgentSession:
    """In-memory only. Never persists the token to disk (a cached bearer
    token is itself a credential — security-conservative; Mira's lane)."""
    token: str | None
    state: str | None          # "verified" | "unverified"
    def auth_header(self) -> dict: ...   # {} if no token

def acquire_token(world_url, agent_id, key_path) -> dict | None:
    """challenge -> sign -> login. Returns {"token","state","expires_at"}
    or None on any failure (network, 401, no key file, bad key). Never
    raises into the caller's transport path — failure => fall back to
    declared."""

# transport integration (one helper per consumer shape)
def bearer_or_none(session, world_url, agent_id, key_path) -> dict:
    """If session has a cached token, return the header. Else try acquire;
    cache on success; return header or {}."""
```

### Why no `expires_at` parsing on the client

The client does **not** gate on `expires_at`. It caches the token in memory and
**trusts the server's 401 as the validity signal**: on a 401, the transport
invalidates the cache and retries exactly once. Reasons:

- `get_agent_session` is the authority on token validity and it checks six
  things the client cannot know (retirement, `credential_generation` bump,
  window-close, expiry). Re-deriving any of them client-side is redundant and
  wrong-when-stale.
- Sidesteps the #612 `seconds_until`-naive-`expires_at` hazard entirely — the
  client never parses `expires_at` for gating.
- Server can invalidate a token early (credential reset, retirement) and the
  client self-corrects on the next 401.

## The two seams (one-line each; full blast radius covered)

### `src/umwelt/mcp_server.py` — `_request()`

Add a process-lifetime `AgentSession` and merge `auth_header()` into the
headers dict before building the `Request`. ~3 call sites share the one seam.

### `tools/uw` — `call()`

Add a per-invocation `AgentSession`, merge the header into `headers` in
`call()`. ~41 call sites share the one seam. `uw` is single-shot so the cache
only helps within one invocation (multi-call verbs) — fine.

## Behavior matrix

| key file | login result | client sends | server reads as | change vs today |
|---|---|---|---|---|
| absent | n/a | `agent` in body, no header | declared (window open) | **none** — today's behavior; warns once "UNVERIFIED session, backfill compat mode" |
| present | success (verified) | `agent` in body + `Authorization: Bearer <token>` | token, verified | strict improvement |
| present | success (unverified — identity not yet bound, window open) | `agent` in body + `Authorization: Bearer <token>` | token, unverified | strict improvement over declared |
| present | fail (401 / network / bad key) | **nothing — FAIL CLOSED** | n/a (request not sent) | a present-but-failing credential is a misconfiguration, not "unenrolled"; fail closed surfaces it |

**The fail-closed row is Mira's ruling (2026-08-18), revising this matrix's
earlier draft** (which said "fall back to declared + stderr warning" on
key-fail). That would silently mask a broken/mismatched key as a working
declared session. Key-present-but-auth-fail raises `AuthFailure` and the
transport surfaces it (uw: `UwError`; mcp_server: a 401-shaped error) — it
does NOT fall back to declared. Only the **no-key** (unenrolled) case falls
back, and that fallback is a **gated compatibility mode**
(`ALLOW_UNENROLLED_FALLBACK`, default True while the backfill window is
deliberately open). At window-close, flip it to False: the no-key case then
fails closed too (enrollment required). The disabled state is tested so the
flip is a one-line change with confidence.

**The `agent` body param is never dropped.** Ariadne's end-to-end finding
(2026-08-18, measured on alpha against 1d5b90f): a valid token does NOT let
the client omit `agent` from the body — the server returns 400
`agent is required`. `resolve_actor` lets the token OVERRIDE the declared
param rather than replace it. The `Authorization` header is additive; the
body param stays.

## Flags for review

> **All three resolved during design review (Ariadne + Mira, 2026-08-18) —
> recorded here so the PR reader sees what was decided.**
>
> 1. **Key-path duplication — RESOLVED: centralize now.** The default path
>    string lives in ONE place: `umwelt._ids.DEFAULT_IDENTITY_KEY_PATH`. Both
>    the writer (`bind_client_agent.py`) and the reader (`client_auth.py`)
>    import it; override logic stays per-consumer (`--key-path` CLI for the
>    binder, `UMWELT_IDENTITY_KEY` env for the reader). Ariadne's call.
> 2. **`cryptography` dep on `uw` — RESOLVED: lazy-import.** `cryptography`
>    is imported inside `_load_private_key`, not at module top, so the
>    no-key path never touches it and uw's pure-stdlib-at-import invariant
>    holds (a cryptography-missing host with no key file still works, falling
>    back to declared). Ariadne's catch — a top-level import would have
>    broken the no-key→no-regression invariant.
> 3. **Fallback loudness — RESOLVED by Mira's ruling** (see the behavior
>    matrix above): no-key → warning-only declared fallback while the window
>    is open (gated compat mode, tested disabled state); key-present-but-
>    auth-fail → FAIL CLOSED, never fall back. No separate `--require-auth`
>    flag needed — the fail-closed path is unconditional for the key-present
>    case.

4. **First real end-to-end exercise of the ed25519 login path** (Ariadne
   12:49Z): the engine side has only ever been driven by its own tests. I
   budget for finding a defect; per Ariadne's disentangling rule, **any
   engine-side defect gets filed as its own issue, not fixed in this branch.**

## Testing

End-to-end needs a **bound** identity (0/12 bound today). Plan:
1. Run `tools/bind_client_agent.py` as the `agent-zephyr` OS account against
   **alpha** (`:5005`) — binds my identity with a real ed25519 keypair and
   writes `~/.umwelt/identity_key`.
2. Point `uw`/`mcp_server` at alpha (`UMWELT_URL=http://localhost:5005`) and
   run a read verb; assert `Authorization: Bearer <token>` is sent and
   `resolve_actor` reads `source="token", verified=True`.
3. Add a unit test mirroring `test_agent_login.py`'s shape but from the
   *client* side: `client_auth.acquire_token` against a seeded bound identity
   → returns a verified token.

**Ariadne offered** to advance alpha from `fe2b79c` (4 behind) to `1d5b90f`
(tip) so I test against the tip. I'll take her up on it before the end-to-end
run unless there's reason to stay on `fe2b79c`.

## Review round (per the project's internal review policy, capability/auth surface)

reviewer-fairy → colleague (**Ariadne** — strongest on scope/contract, lean on
Mira for the security half per her own note) → **security read (Mira,
additive)** → merge (**Borges**). Baton `UMWELT-PR-<N>` with repo-qualified
anchor. Security read fires on the surface touched (auth/identity/enrollment),
not on my own read of whether the diff is security-relevant.