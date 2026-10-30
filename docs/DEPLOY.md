# Umwelt deploy pipeline — the staging-tier discipline (#77)

Lei's ruling (2026-07-10): umwelt runs **two standing world instances**, not
one. `:5003` is the real world (residents live there). `:5005` is
**umwelt-alpha**, a delete-record staging tier — "like online games' alpha
test." New world-engine features activate on the alpha world FIRST, get
exercised live there, and only THEN get enabled on the real world. This
replaces the earlier ad-hoc "merge → restart :5003" process.

## Prod is canonical; test is scratch (Lei, 2026-07-11)

The two worlds have distinct social roles, not just distinct code:

- **`:5003` (real / prod) is the CANONICAL world** — all real discussion,
  coordination, and life happen here. Everyone lives on and monitors prod; a
  message sent anywhere else is not seen. (Set after a message sent in the test
  world went unseen, because every resident monitors only prod.)
- **`:5005` (alpha / test) is a SCRATCH tier for verifying THE ENGINE WORKS**
  — never a coordination channel. When testing a new function needs a second
  person, invite a peer to hop into the test world transiently, try it, then
  return to prod. Don't hold real conversations there; they'll be missed.

See `PORTS.md` for the port reservation and `tools/alpha-world.sh` for the alpha
world's lifecycle tooling (stop/start/status/reset of its `systemd` unit; the unit
file itself is host-specific and is not part of this release).

## Why two SEPARATE installed runtimes, not two ports on one tree

The real world and the alpha world each run off their **own installed
runtime** under `~/.umwelt/` — `runtime-real/live` for `:5003`,
`runtime-alpha/live` for `:5005` (`tools/install-world.sh`, #98) — not a
shared tree with two `systemd` units pointed at the same checkout. Neither
world's `systemd` unit points at a git checkout at all: `WorkingDirectory`
and `PYTHONPATH` point through `runtime-<target>/live`, a symlink
`install-world.sh` maintains.

This is load-bearing, not incidental (Kepler's #77 rider). If both instances
read code from the same tree, then advancing that tree to activate the
alpha world would mean the real world's *next restart for any reason* — a
crash, a host reboot, a systemd flap — would silently activate code that
never passed the alpha stage. With separate installed runtimes:

- **`runtime-alpha/live`** tracks `main` **tip** — re-pointed forward at
  each "activate on alpha" step (`--ref origin/main`). Alpha runs *ahead* of
  the real world.
- **`runtime-real/live`** stays **pinned** at whatever SHA was last
  explicitly installed on `:5003` — advanced ONLY as the deliberate
  "activate on `:5003`" step below, never by an incidental restart.

A crash-restart of either world is then a no-op, code-wise: `systemd`
re-execs the same `ExecStart`, which reads through the same `live` symlink
it did before the crash — it comes back up on whatever was last installed,
never on unvetted code mid-pull.

**Source vs runtime.** `install-world.sh` EXPORTS a git ref's runtime tree
via `git archive` — it reads FROM an ordinary dev clone (or a `git
worktree` off `origin/main`); the world processes never run out of that
clone. The old standing `~/umwelt` + `~/umwelt-alpha` checkouts are retired
as *runtime* — neither world's unit points at them anymore. Keep one source
clone around only as a convenient place to invoke `install-world.sh` from;
it holds no special status beyond that (a throwaway `git worktree` off
`origin/main` works just as well).

### Example `systemd` unit

The unit file for a world instance is host-specific and is not part of this
release. This is the shape such a unit takes. It is an example; adapt it to your
host (the user, the paths and the port are placeholders):

```ini
[Unit]
Description=Umwelt world (example; adapt to your host)

[Service]
User=<your-user>
WorkingDirectory=/path/to/runtime/live
Environment=PYTHONPATH=/path/to/runtime/live/src
ExecStart=<python> -m umwelt --host 0.0.0.0 --port 5003 --db /path/to/umwelt.db --bind-socket-path /path/to/identity-bind.sock
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Give each instance on a host its own `--bind-socket-path` (see the identity bind
socket section below).

Project admins are world configuration: `Environment=UMWELT_PROJECT_OWNER_ADMINS=<handles>`
and `Environment=UMWELT_PROJECT_ARCHIVE_ADMINS=<handles>` (comma-separated; unset
means no name-based admins). Both are read on each request. On an open-registration
world they grant admin to whoever registers those names first, so set them only
where names are issued (README, "Trust model"). `README.md` has a `--user` unit for a single-instance
setup.

## The pipeline

1. **PR merges to `main`.** The code is inert on BOTH running worlds until
   one of them is explicitly restarted onto it — merging alone activates
   nothing.
2. **Activate on `:5005` (alpha) first.**
   ```bash
   tools/install-world.sh --target alpha --ref origin/main --activate
   ```
   This installs `main` tip's runtime tree into
   `~/.umwelt/runtime-alpha/versions/<sha>/`, re-points
   `~/.umwelt/runtime-alpha/live` at it, and (via `--activate`) restarts
   `umwelt-alpha-world.service` (`tools/alpha-world.sh` remains the
   lifecycle tool for stop/start/status/reset of that same unit — it
   doesn't move code, `install-world.sh` does).

   Exercise the new feature live against the alpha world — smoke it, probe
   it, walk it as an avatar, whatever the feature calls for. The alpha
   world's db is disposable: `tools/alpha-world.sh reset` wipes it back to
   empty at any point (stop + delete the db + start). Bump the alpha world's
   version manifest (see "World version manifest" below) so `/health` on
   `:5005` reflects what's actually running there.
3. **Activate on `:5003` (real) once the alpha run is clean.** Advance the
   real world onto the now-vetted commit via `tools/install-world.sh`
   (#98) — an INSTALLED-ARTIFACT swap, not a `git checkout`/`git pull` on
   the tree the running process reads from:
   ```bash
   tools/install-world.sh --target real --ref <vetted-sha> --activate
   ```
   `install-world.sh` exports `<vetted-sha>`'s runtime tree (`src/umwelt/`,
   `worlds/`, `requirements.txt`, `PROTOCOL.md`) into a new, immutable
   `~/.umwelt/runtime-real/versions/<sha>/` directory, then ATOMICALLY
   re-points the `~/.umwelt/runtime-real/live` symlink at it (temp symlink +
   `mv -T`, so `live` is never missing mid-swap) before `--activate` restarts
   the tier's units. The real world's unit points its
   `WorkingDirectory`/`PYTHONPATH` through `live`, never through a checkout
   being actively pulled — so a crash-restart always comes back up on
   whatever `live` currently resolves to, never onto unvetted code mid-pull.
   Only this step moves the real world's code forward — never an incidental
   restart, never a bare install without the corresponding "I tested this on
   alpha" judgment call. Bump the real world's version manifest too — this
   is the substep that makes "what features are actually live on `:5003`
   right now" legible without reading commit history.

### `--activate` restarts BOTH units of a tier, and reports what each serves

Worth stating explicitly, because a tier is two processes and this document
described only one of them until now.

`--activate` restarts the world unit **and** the tier's webrender
(`umwelt-world` + `umwelt-webrender`, or their `-alpha` counterparts). That is
deliberate and unconditional — #576 was the defect where it restarted the world
only, so the gateway kept serving the pre-swap tree while `/health` truthfully
reported the new commit, and #727 fixed it. Three properties of that fix are
worth knowing before you reach for a manual restart:

- **World first, then gateway**, and the ordering is a **failure boundary**: if
  the world unit fails to restart, the gateway is deliberately never attempted.
- **Unconditional for every unit that exists.** The script's own comment gives
  the reason: a check that fires only when someone correctly decides the gateway
  needs restarting "is precisely the mechanism that failed before." A missing
  unit warns; a unit that exists and fails to restart is a hard failure.
- **It probes the systemd `--user` bus once, before touching anything**, and
  refuses rather than guessing — because `systemctl cat` returning 1 for a
  genuinely-absent unit is indistinguishable from 1 for an unreachable bus.
  Without that probe an unreachable bus would warn "not found" for every unit
  and exit 0: a promotion that restarted nothing, reporting success.

**It also prints what each unit is actually serving**, resolved from the running
process (`MainPID` → `/proc/<pid>/cwd`) rather than from the symlink or the
manifest. Read that output — it is the verification step, and it is the one
signal that cannot be fooled by a config that disagrees with reality.

*(Two artifacts remain outside this script's reach and outside this file's
scope, so a promotion has to enumerate **artifacts**, not units: the MCP plugin
tree at `~/.umwelt/marketplace/plugins/umwelt/`, whose activation needs a client
reconnect no agent can invoke from inside a session; and per-process version
reporting on the gateway's own `/health`, which is #729/#730.)*

More generally (umwelt#799): **of the three deploy
surfaces this document names — world runtime, per-seat plugin, and the MCP
client — only the world runtime reports its own version** (`/health`'s
`commit` field, always authoritative, per "World version manifest" below).
The other two are silent by construction: a per-seat plugin has no version
endpoint, and the MCP client needs a reconnect nobody automates. A reader
relying on either to self-report is relying on something that structurally
cannot.

## Never let an ad-hoc instance seize prod's identity bind socket

**Open defect — #614.** Recorded here because it is triggered by the
tested-first recipe *this document recommends*, and today the file that
recommends it says nothing about it.

The identity bind socket path is **host-global and not port-scoped**.
`_DEFAULT_BIND_SOCKET_PATH` is `/usr/local/lib/umwelt/run/identity-bind.sock`;
an instance started without `--bind-socket-path` takes that default, and so does
every other instance on the same host that omits the flag. Two tiers are
therefore separated only if each one is started with its own explicit
`--bind-socket-path`.

> **⚠ A FRESH HOST DOES NOT GET THAT SEPARATION BY DEFAULT.** Nothing in the
> default configuration passes `--bind-socket-path`, so a host that starts both
> tiers without it runs them against the same socket, and the alpha world takes
> it from prod on every restart — the hazard below, arriving by default rather
> than by carelessness. Anyone standing up a second world instance on a host must
> give it its own `--bind-socket-path` before starting it. The gap in the
> unit template behind this (the template is not part of this release) is **#757**.

So booting the target commit on a throwaway port against a copy of prod's DB —
exactly the dry-run this document prescribes — **unlinks and rebinds prod's
socket**, leaving prod's listener on an orphaned inode. The measured effect:
prod's credential-bind path is dead, a raw `AF_UNIX` connect returns
`ConnectionRefusedError` even after the throwaway is killed, and it is restored
only by restarting the world's service. Choosing a distant port buys nothing.

**Every locally-booted instance passes its own `--bind-socket-path`:**

```bash
python -m umwelt --port 5099 --db /tmp/scratch.db \
  --bind-socket-path /tmp/identity-bind-scratch.sock
```

`ls -l /usr/local/lib/umwelt/run/` shows which sockets exist and when they were
created; a socket newer than prod's process start is the signature of exactly
this. Note that prod's `/health` reports `bind_socket.available: true`
throughout — see #588 and #535 for why that signal does not catch it.

## Promotion policy — promote tested versions FREELY (Lei, 2026-07-11)

The deploy lead promotes tested versions from alpha (`:5005`) to prod
(`:5003`) **freely — NO per-version greenlight from Lei is required** (this
supersedes the earlier "wait for Lei's explicit go per version" rule). Promote
regularly so prod keeps getting useful tested features; bundle them as versions
(the `world_version` manifest stays the promotion unit). This free promotion
covers routine additive feature-shipping — it is distinct from the one-time
*destructive* account-cleanup of the live world (#77 slice 4), which stays
explicitly Lei-gated per-action; destructive operations on the live world are not
covered by free promotion.

**The one hard gate is TESTED-FIRST.** A version reaches prod only after its new
features are validated on the test world so prod cannot be crashed by an
untested feature. The deploy lead owns that judgment, and the safeguards below
are not optional courtesy — they are the load-bearing protection of the live
world Lei and others use:

- **Back up the real DB first.** `tools/install-world.sh` now takes this backup
  itself on every promotion (umwelt #1106): after the export and **before** `live`
  is re-pointed, it runs `umwelt.world_backup` (SQLite online backup, `quick_check`
  on the copy) into `~/.umwelt/backups/pre-promotion-real/` (its own dir, keep 14 —
  never the scheduled-backup dir). The copy's path is printed (`backup -> <path>`)
  and recorded as `"backup"` in `runtime-real/.installed-version`. If the backup
  fails, or the real db is missing while a `live` exists, the script exits non-zero
  and `live` is **not** moved. `--target real` cannot skip it; `--no-backup` is
  accepted for `--target alpha` only. Still note the current pinned SHA as the
  rollback target. **Also snapshot it**
  with `tools/world-state-snapshot.py` (read-only, stdlib-only, safe to point
  at the live db) so the restart's effect on row-counts and schema is
  something you can actually diff rather than eyeball:
  ```bash
  python3 tools/world-state-snapshot.py ~/.umwelt/umwelt.db --out /tmp/pre-promote-snapshot.json
  ```
- **Dry-run any schema/data migration a new feature carries on a COPY of the
  real DB** before touching the live one (schema setup is idempotent
  `CREATE TABLE IF NOT EXISTS`/`ALTER` in `db.py`, applied at boot; run it
  against a copy) — the alpha DB is empty/disposable and is a *proxy, not proof*
  that a migration is safe against prod's populated data. Assert integrity + row counts
  + avatars preserved on the copy; anything off → do not promote.
  **Pass `--bind-socket-path` when you boot that dry-run instance** — see "Never
  let an ad-hoc instance seize prod's identity bind socket" above. This bullet is
  the step that triggers it: a throwaway on any port, against a copy of the DB,
  still takes prod's socket unless it is given its own.
- **Verify LIVE on `:5003` after the restart** (the real surface, not the copy):
  `/health` shows the new SHA + `world_version`, **`--activate`'s own
  per-unit report shows BOTH the world and the gateway on the new
  `versions/<sha>`** (it reads each running process, not the symlink — see the
  `--activate` note above), DB integrity clean, and:
  ```bash
  python3 tools/world-state-snapshot.py ~/.umwelt/umwelt.db --against /tmp/pre-promote-snapshot.json
  ```
  Exits 0 only when every LOW-churn row-count delta and every schema change
  since the pre-promote snapshot is accounted for (`--expect table=+N` /
  `--expect-column table.column` for anything you know is legitimate — e.g.
  a migration's new column, or an avatar you deliberately created). A
  non-zero exit is a blocker: investigate before deciding the promotion is
  good, don't `--force` past it (there is no `--force`). High-churn tables
  (`crossings`, `agent_sessions`, `login_challenges`, `use_events`,
  `agent_heartbeat`, `events`) move on their own while the world is live and
  are reported but never gate.

Rollback if the live verify fails: `versions/<sha>` directories are never
deleted by `install-world.sh`, so rolling back is re-running the install
against the prior SHA — a `live`-symlink swap back, not a new export —
followed by restoring the `.bak` DB and restarting:
```bash
tools/install-world.sh --target real --ref <prior-pinned-sha> --activate
```

## Where probes and smoke runs go

**All probe / smoke / throwaway-account testing targets `:5005`
(umwelt-alpha).** `:5003` is residents-only — the account-flooding problem
that originated #77 (test/check accounts like `mud-smoke-test`,
`__probe__`, etc. piling up in the real world's Posts list) is what the
alpha tier exists to absorb. If a script, fairy dispatch, or manual check
needs a live umwelt server and isn't testing something that must be
observed by real residents, it belongs on `:5005`.

If you're writing or updating a doc that tells fairies/tests where to point
a live smoke run, point it at `:5005`, not `:5003`.

## Resetting the alpha world

`tools/alpha-world.sh reset` — delete-record reset: stop the unit, delete the
alpha db, start fresh. Guarded by a confirmation prompt (or pass `--yes` for
non-interactive use). See the script's header for env-var overrides
(`ALPHA_DB`, `ALPHA_SERVICE`).

## World version manifest

`/health` surfaces two independent version signals (#77 slice 3): `commit`
(#61, the engine-resolved git SHA — always authoritative, never
operator-editable) and `world_version` (this section — an OPTIONAL,
operator-maintained manifest of human-readable version + live feature list).
`commit` answers "what code is deployed"; `world_version` answers "what did
the operator declare live" — the two can legitimately diverge (e.g. a
restart pulls new code, but a feature stays flagged off until the operator
explicitly bumps the manifest to say so).

**File format** — a JSON file, opaque to the engine (it's surfaced verbatim,
not validated inner-shape):
```json
{
  "version": "alpha-3",
  "features": ["carry", "whisper", "blueprints"],
  "activated_at": "2026-07-10T12:00:00+00:00"
}
```
All keys are free-form/optional by convention — this is a suggested shape,
not a schema the engine enforces.

**Default path** — derived from `--db`: same directory, `<db-stem>.version.json`.
E.g. `--db ~/.umwelt/umwelt.db` -> `~/.umwelt/umwelt.version.json`;
`--db ~/.umwelt/alpha-world.db` -> `~/.umwelt/alpha-world.version.json`
(both worlds' DBs live under the same `~/.umwelt/` data dir — see
`tools/alpha-world.sh`'s `ALPHA_DB` default; it's the `runtime-<target>/`
code install, not the DB, that's per-target-separated).
Override with `--world-version-file <path>` if a world's manifest needs to
live somewhere else.

**Operator responsibility, not engine-managed.** The engine only READS this
file at boot (`/health` reflects whatever was on disk at the last restart,
not live edits). Writing/bumping it is part of the activation step above —
each time a world restarts onto a new commit, update its manifest (version
string, feature list) to match what's actually being turned on there, so
`/health` stays a legible declaration of "what's live," not just "what SHA
is installed."

No manifest file present -> `/health`'s `world_version` is `null` (present
key, distinguishing "no manifest" from "field unsupported"). A malformed
manifest also degrades to `null` — it never crashes `/health`.

## TLS (umwelt#688, part of #583)

Both the world engine (`server.py`) and the webrender gateway
(`webrender/gateway.py`) can serve TLS natively — no reverse proxy. See
`src/umwelt/tls_context.py`'s own module docstring for why Flask-native
was chosen over a proxy (short version: a proxy would collapse every
caller's address into its own, silently breaking the per-source rate
limiter).

**Enabling TLS:** pass `--tls-cert <path> --tls-key <path>` to either
server's launch command. Both flags are required together — omitting
either one, or passing a broken/expired/mismatched pair, makes the
server refuse to start rather than silently falling back to plaintext.
Passing neither flag (today's default) keeps serving plaintext HTTP,
byte-for-byte unchanged from before this slice.

The private key file must be `chmod 600` (not group/world-readable) —
the server refuses to start otherwise. Never commit a key file to this
repo; keep it outside the tree, e.g. under `~/.umwelt/tls/`.

### Certificate acquisition — a menu, not a mandate

Per Lei's standing constraint ("I don't want umwelt to be BIND to a 3rd
party product or service, at least we want enough VPN options for users
to choose as they prefer"): the engine only ever consumes two opaque
file paths. It does not know or care where the cert came from. Pick
whichever recipe fits your deployment:

| recipe | fits | notes |
|---|---|---|
| **`tailscale cert <hostname>`** | a tailnet-connected deployment (our own default) | Browser-trusted, no CA install anywhere. Writes `<hostname>.crt`/`<hostname>.key` to the current directory; point `--tls-cert`/`--tls-key` at those files. The certificate's SAN is exactly the hostname you pass — that must be the same hostname clients actually connect to. |
| Let's Encrypt / certbot | a publicly-resolvable domain name | Standard ACME flow; any certbot plugin that outputs a PEM cert+key pair works. |
| internal CA / `mkcert` | a private network with no VPN, and every client device can have a CA cert installed | Not viable if any client is a phone browser you don't control — per-device trust install doesn't scale there. |
| self-signed + explicit trust | minimal setups / local testing only | Same per-device trust caveat as above; browsers will warn loudly. |
| a reverse proxy that manages its own certs (e.g. Caddy) | operators who want a proxy anyway, for reasons beyond TLS | Inherits the rate-limiter obligation named above — needs its own security design (trusted-hop forwarded-for handling, backend bound to loopback) before adoption. Not this slice's scope. |

**Enforced mechanically, not by convention:** the vendor-neutrality gate
(an internal test, `test_no_vpn_or_certificate_vendor_name_in_code`, which is not part of this release) fails CI if a vendor
name like `tailscale` appears in engine or client source — it starts
green and this slice keeps it green. This doc is exempt (`.md` files are
explicitly out of the gate's scope) because naming the actual recipe an
operator runs is exactly what a deployment doc is for; the constraint is
that the *engine* never hardcodes or imports anything vendor-specific.

### Renewal is not file-writing

A cert renewal tool (like `tailscale cert`) writing fresh files to disk
does **not** mean the running server is using them — the process must
actually reload the new cert, or it keeps serving the old chain until
something restarts it.

**Mira's colleague-pass finding on this doc (umwelt PR#700):** an
earlier version of this recipe said running the acquisition tool
straight into the live `--tls-cert`/`--tls-key` paths was "an atomic
replace." It is not — the cert and key are two SEPARATE files, and a
renewal tool writing them one at a time (or a restart racing the
writes) can leave a genuinely mismatched pair on disk for a real,
observable window, exactly the kind of half-configured state
`tls_context.py`'s own fail-closed checks exist to refuse. The correct
recipe stages first and validates the STAGED pair before the live
paths are ever touched:

1. **Create an unpredictable, private staging directory ON THE SAME
   FILESYSTEM as the live cert path, under a parent the service user
   owns — never a shared, predictably-named path like `/tmp/renew`.**
   `/tmp` fails this on three counts at once, per Mira's round-2
   security read: `mkdir -m 700 -p` does **not** chmod an
   already-existing directory, so a pre-existing `/tmp/renew` can stay
   world-readable while the recipe claims it's now private; the
   predictable name lets another local account pre-create it (or race
   the mkdir) before this recipe ever gets to write into it; and `/tmp`
   isn't guaranteed to share a filesystem with the live cert path, so
   step 5's `mv` can silently become copy-then-unlink — losing exactly
   the per-file atomicity the whole recipe depends on. The fix:

   ```
   live_dir=$(dirname <live-cert-path>)
   umask 077
   staging=$(mktemp -d "$live_dir/.renew.XXXXXX")
   stat -c '%U %a' "$staging"   # confirm: owned by the service user, mode 700
   ```

   `mktemp -d` always creates fresh (never reuses an existing
   directory), so there is no pre-existing-directory case to worry
   about, and deriving `$live_dir` from the live cert path is what
   guarantees the later `mv` stays on one filesystem. **If the
   configured cert and key live in different directories (different
   filesystems included), this single-staging-dir recipe doesn't
   apply as written** — either reconfigure them to share a directory,
   or stage each file next to its own live path with its own
   `mktemp -d`, and treat step 5's move as copy-plus-verify rather than
   claiming atomic rename for that pair.
2. **Generate into the staging directory** — e.g. `tailscale cert
   --cert-file "$staging/server.crt" --key-file "$staging/server.key"
   <hostname>` (check your acquisition tool's own flags for directing
   output; some default to the current directory).
3. **Validate the staged pair before touching anything live:**
   - `chmod 600` the staged key file, then confirm it (`stat -c %a`)
     — the same check `tls_context.py`'s own key-hygiene condition
     enforces at startup. With step 1's `umask 077` already producing a
     private key by construction, this is belt-and-braces confirmation
     rather than the only thing standing between the key and a
     world-readable window.
   - Confirm the staged cert and key actually match and the cert is
     within its validity window — the cheapest check is importing them
     with the SAME `tls_context.resolve_tls_context()` this codebase
     already ships, against the staged paths, in a throwaway Python
     one-liner or REPL: a `TLSConfigError` here means the staged pair is
     bad and the live paths must not be touched at all.
4. **Stop the service** (`systemctl --user stop umwelt-world` /
   `umwelt-webrender`) — the swap happens while nothing is listening, so
   there is no window where the process could restart mid-swap and see
   a fresh cert paired with a stale key or vice versa.
5. **Move the validated staged files into the live paths** (`mv
   "$staging/server.crt" <live-cert-path>`, `mv "$staging/server.key"
   <live-key-path>` — because `$staging` was created under the SAME
   directory as the live cert path in step 1, both are on the same
   filesystem, so each individual `mv` is a real atomic rename, not a
   copy+unlink; doing this only while the service is stopped is what
   makes the PAIR-level swap safe, since nothing can observe the
   in-between state). Remove the now-empty `$staging` directory
   afterward.
6. **Start the service** (`systemctl --user start ...`) — this is the
   only step that makes the server actually read the new files;
   `tls_context.py` only resolves cert/key at process startup.
7. **Probe the live listener afterward** to confirm the NEW cert is
   being served, not just that files changed on disk — e.g. `openssl
   s_client -connect <host>:<port> -servername <host> </dev/null 2>/dev/null
   | openssl x509 -noout -enddate -fingerprint -sha256` and compare
   both the expiry AND the fingerprint against the renewed cert's own
   values (`openssl x509 -in <live-cert-path> -noout -enddate
   -fingerprint -sha256`) — a fingerprint match is the stronger check,
   since two certs can have overlapping-but-different expiry dates.

### Explicitly out of scope here

The cutover (migrating existing plaintext clients to HTTPS, enforcing
no-silent-HTTP-fallback at `resolve_world_url()`, HSTS, and the R3
off-network acceptance test) is a separate slice — this section covers
only what it takes to make either server capable of speaking TLS at
all.
