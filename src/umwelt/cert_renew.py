"""Tailnet cert auto-renew (umwelt #1108).

    python -m umwelt.cert_renew [--dry-run] [--host H] [--live-dir D] ...
    python -m umwelt.cert_renew --alarm-failed [--exit-code N] [--url U]

NO HOST IDENTITY LIVES IN THIS CODE. The host, the certificate-fetch command,
its working directory, the per-port probe addresses and (for `--alarm-failed`)
the alarm recipient and place come ONLY from an install-time systemd drop-in
(`Environment=` lines, written by deploy/install-cert-renew.sh) or the
environment:

    UMWELT_CERT_HOST         host; live files are <live_dir>/<host>.crt|.key
    UMWELT_CERT_FETCH_ARGV   the fetch command, one string (shlex-split);
                             `{host}` in it is replaced by the host; no
                             argument can contain a space (quotes are not
                             allowed), so a program path with a space must be
                             reached through a wrapper script or a symlink
                             without spaces
    UMWELT_CERT_FETCH_CWD    working directory of the fetch command
    UMWELT_CERT_PROBES       PORT=ADDRESS,... covering every default port
    UMWELT_CERT_ALARM_TO / _PLACE   --alarm-failed only: who hears it, where
    optional: UMWELT_CERT_LIVE_DIR (default ~/.umwelt/tls),
              UMWELT_CERT_ALARM_FROM (default "cert-renew")

With a required key unset, empty or malformed the entry point refuses: exit 1,
the message names the key(s) and the drop-in path, and nothing was fetched,
staged, restarted, locked or posted.

Fetches the renewed pair with the configured command (key and chain on
stdout),
validates it, swaps it into the live dir, restarts the world units, probes the
listeners, and rolls back on any failure. Contract pinned by
tests/test_cert_renew.py (C1-C20); units in deploy/umwelt-cert-renew*.

Exit codes (C19): 0 renewed or already current, 1 refused before touching the
live pair, 2 swap/restart/probe failure rolled back, 3 rollback itself failed
(the backup dir is kept), 4 another run holds the lock.

* Nothing under the live dir is replaced until the staged pair passed every
  check (SAN, key match, notAfter strictly later, the servers' own loader).
* The fetched key lives only in memory and in mode-0600 files created
  O_EXCL inside a 0700 staging dir under the live dir. It is never in argv, an
  env var, or any message (every printed line is redacted).
* Swap: key first, cert last, each by atomic rename from beside the target.
  tls-front (:5014) is probed but never restarted by us; a probe failure there
  is a rollback trigger like the others.

The independent expiry alarm is `umwelt.cert_alarm`. It shares no logic with
this module and imports nothing from it (pinned by a test); both post through
world_backup.alarm, so a break in that posting path silences both.
`--alarm-failed` lives HERE, not there: it is this renewer's own OnFailure
crossing.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization

DEFAULT_LIVE_SUBDIR = ".umwelt/tls"
DEFAULT_PORTS = (5006, 5007, 5008, 5009, 5014)
DEFAULT_UNITS = ["umwelt-alpha-world", "umwelt-alpha-webrender",
                 "umwelt-world", "umwelt-webrender"]
DEFAULT_RESTART_ARGV = ["systemctl", "--user", "restart"]
DEFAULT_ALARM_FROM = "cert-renew"
LOCK_NAME = ".cert-renew.lock"

ENV_HOST = "UMWELT_CERT_HOST"
ENV_FETCH_ARGV = "UMWELT_CERT_FETCH_ARGV"
ENV_FETCH_CWD = "UMWELT_CERT_FETCH_CWD"
ENV_PROBES = "UMWELT_CERT_PROBES"
ENV_ALARM_TO = "UMWELT_CERT_ALARM_TO"
ENV_ALARM_PLACE = "UMWELT_CERT_ALARM_PLACE"
ENV_LIVE_DIR = "UMWELT_CERT_LIVE_DIR"
ENV_ALARM_FROM = "UMWELT_CERT_ALARM_FROM"

# The drop-in a refusal points at: the unit that runs the entry point.
UNIT_RENEW = "umwelt-cert-renew"
UNIT_FAILED = "umwelt-cert-renew-failed"
DROPIN_FMT = "~/.config/systemd/user/{unit}.service.d/identity.conf"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ROLLED_BACK = 2
EXIT_ROLLBACK_FAILED = 3
EXIT_LOCKED = 4

# Probe retry window after a restart (a service needs a moment to bind).
PROBE_ATTEMPTS = 8
PROBE_DELAY_S = 1.5
FETCH_TIMEOUT_S = 120
RESTART_TIMEOUT_S = 120


@dataclass(frozen=True)
class Probe:
    label: str
    address: str
    port: int



@dataclass(frozen=True)
class Config:
    host: str
    live_dir: str
    fetch_argv: list
    fetch_cwd: str
    units: list
    probes: list
    lock_path: str
    restart_argv: list = field(default_factory=lambda: list(DEFAULT_RESTART_ARGV))


class RenewRefused(Exception):
    """Validation refused; `.reason` is a short string (never key material)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ConfigRefused(Exception):
    """A required config key is missing/empty/malformed. `.problems` is a list
    of (key, why); `.unit` names the unit whose drop-in should carry the key."""

    def __init__(self, unit: str, problems: list):
        self.unit, self.problems = unit, problems
        keys = ", ".join(k for k, _ in problems)
        super().__init__(f"config refused: {keys}")

    def message(self, prog: str = "cert_renew") -> str:
        detail = "; ".join(f"{k} {why}" for k, why in self.problems)
        return (f"{prog}: refusing to run: {detail}. Set it in "
                f"{DROPIN_FMT.format(unit=self.unit)} (written by "
                "deploy/install-cert-renew.sh) or the environment.")


def parse_probes(text: str) -> list:
    """`PORT=ADDRESS,PORT=ADDRESS,...` -> [Probe(label=str(port), ...)].
    Raises ValueError with a short reason."""
    probes, seen = [], set()
    for item in text.split(","):
        port_s, sep, address = item.strip().partition("=")
        if not sep or not address.strip():
            raise ValueError(f"{item.strip()!r} is not PORT=ADDRESS")
        try:
            port = int(port_s)
        except ValueError:
            raise ValueError(f"{item.strip()!r}: the port is not a number") from None
        if not 0 < port < 65536:
            raise ValueError(f"{item.strip()!r}: the port is out of range")
        if port in seen:
            raise ValueError(f"port {port} appears twice")
        seen.add(port)
        probes.append(Probe(str(port), address.strip(), port))
    return probes


def _missing_ports(probes) -> list:
    have = {p.port for p in probes}
    return [p for p in DEFAULT_PORTS if p not in have]


def load_config(env, *, unit: str = UNIT_RENEW, host=None, live_dir=None,
                fetch_cwd=None, probes=None, units=None, restart_argv=None,
                lock_path=None) -> Config:
    """The renewer's Config from `env` (a mapping); a non-None keyword is an
    override that replaces its env key. Raises ConfigRefused naming every
    missing/empty/malformed REQUIRED key. A probe override (flags) skips the
    PROBES key and its default-port coverage check."""
    problems: list = []

    def need(key, override):
        val = override if override else (env.get(key) or "").strip()
        if not val:
            problems.append((key, "is not set"))
        return val

    host = need(ENV_HOST, host)
    fetch_cwd = need(ENV_FETCH_CWD, fetch_cwd)
    raw_argv = (env.get(ENV_FETCH_ARGV) or "").strip()
    fetch_argv: list = []
    if not raw_argv:
        problems.append((ENV_FETCH_ARGV, "is not set"))
    else:
        try:
            fetch_argv = shlex.split(raw_argv)
        except ValueError as exc:
            problems.append((ENV_FETCH_ARGV, f"cannot be split ({exc})"))
        else:
            if not fetch_argv or not fetch_argv[0]:
                problems.append((ENV_FETCH_ARGV, "has an empty command"))
    if probes is None:
        raw_probes = (env.get(ENV_PROBES) or "").strip()
        if not raw_probes:
            problems.append((ENV_PROBES, "is not set"))
        else:
            try:
                probes = parse_probes(raw_probes)
            except ValueError as exc:
                problems.append((ENV_PROBES, f"is malformed ({exc})"))
            else:
                gone = _missing_ports(probes)
                if gone:
                    problems.append((ENV_PROBES, "misses the port(s) "
                                     + ", ".join(map(str, gone))))
    if problems:
        raise ConfigRefused(unit, problems)
    live_dir = live_dir or (env.get(ENV_LIVE_DIR) or "").strip() or \
        os.path.join(os.path.expanduser("~"), DEFAULT_LIVE_SUBDIR)
    return Config(
        host=host, live_dir=live_dir,
        fetch_argv=[tok.replace("{host}", host) for tok in fetch_argv],
        fetch_cwd=fetch_cwd, units=list(units) if units else list(DEFAULT_UNITS),
        probes=list(probes),
        lock_path=lock_path or os.path.join(live_dir, LOCK_NAME),
        restart_argv=list(restart_argv) if restart_argv
        else list(DEFAULT_RESTART_ARGV))


def load_alarm_target(env, *, to_agent=None, destination=None,
                      unit: str = UNIT_FAILED) -> tuple:
    """(to_agent, destination) for --alarm-failed; both REQUIRED, flags
    override the env keys. Raises ConfigRefused."""
    to = to_agent or (env.get(ENV_ALARM_TO) or "").strip()
    dest = destination or (env.get(ENV_ALARM_PLACE) or "").strip()
    problems = []
    if not to:
        problems.append((ENV_ALARM_TO, "is not set"))
    if not dest:
        problems.append((ENV_ALARM_PLACE, "is not set"))
    if problems:
        raise ConfigRefused(unit, problems)
    return to, dest


# ------------------------------------------------------------------ redaction

_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----",
                     re.DOTALL)
_BLOCK_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----",
                       re.DOTALL)


def _body_lines(text: str) -> list:
    """Long non-marker lines of `text`: the base64 bodies that must never be
    echoed (certs included; redacting a cert body is harmless)."""
    return [ln.strip() for ln in text.replace("\r", "").splitlines()
            if len(ln.strip()) >= 20 and not ln.strip().startswith("-----")]


def _redact(msg: str, secrets=()) -> str:
    msg = _PEM_RE.sub("[PEM REDACTED]", msg)
    for s in secrets:
        if s:
            msg = msg.replace(s, "[REDACTED]")
    return msg


# -------------------------------------------------------------------- parsing

def parse_fetch_output(text: str) -> tuple[str, str]:
    """(cert_chain_pem, key_pem) from the fetch command's stdout. CR is
    stripped. Needs >= 1 CERTIFICATE block and exactly 1 private-key block."""
    text = text.replace("\r", "")
    certs, keys = [], []
    for m in _BLOCK_RE.finditer(text):
        label, block = m.group(1), m.group(0).strip() + "\n"
        if label == "CERTIFICATE":
            certs.append(block)
        elif label.endswith("PRIVATE KEY"):
            keys.append(block)
    if not certs:
        raise RenewRefused("fetch output contains no CERTIFICATE block")
    if len(keys) != 1:
        raise RenewRefused(
            f"fetch output contains {len(keys)} private key blocks "
            "(expected exactly 1)")
    return "".join(certs), keys[0]


# ----------------------------------------------------------------- validation

def _load_cert(path):
    with open(path, "rb") as fh:
        return x509.load_pem_x509_certificate(fh.read())


def _spki(public_key) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)


def validate_staged(cert_path, key_path, live_cert_path, host) -> str:
    """'ok' (staged is strictly newer and sound) or 'noop' (same notAfter as
    the live cert). Raises RenewRefused otherwise. Nothing is written."""
    try:
        staged = _load_cert(cert_path)
    except Exception as exc:
        raise RenewRefused(
            f"staged certificate unreadable ({type(exc).__name__})") from None
    # C1: SAN must carry the host
    try:
        san = staged.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []
    if host.lower() not in [n.lower() for n in san]:
        raise RenewRefused(f"certificate SAN lacks DNS:{host}")
    # C2: the key must be this cert's key
    try:
        with open(key_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
    except Exception as exc:
        raise RenewRefused(
            f"staged private key unreadable ({type(exc).__name__})") from None
    if _spki(key.public_key()) != _spki(staged.public_key()):
        raise RenewRefused("private key does not match the certificate")
    # C3/C4/C5: notAfter against the deployed cert
    try:
        live = _load_cert(live_cert_path)
    except Exception as exc:
        raise RenewRefused(
            f"live certificate unreadable ({type(exc).__name__})") from None
    new_after, old_after = staged.not_valid_after_utc, live.not_valid_after_utc
    if new_after < old_after:
        raise RenewRefused(
            f"staged notAfter {new_after:%Y-%m-%dT%H:%M:%SZ} is EARLIER than "
            f"the live {old_after:%Y-%m-%dT%H:%M:%SZ} (anomaly, not a renewal)")
    if new_after == old_after:
        return "noop"
    # C6: the servers' own fail-closed loader, on the staged pair
    from umwelt import tls_context
    try:
        tls_context.resolve_tls_context(cert_path, key_path)
    except tls_context.TLSConfigError as exc:
        raise RenewRefused(
            f"the servers' TLS loader rejected the staged pair: {exc}") from None
    return "ok"


# --------------------------------------------------------------------- probes

def probe_served_notafter(address: str, port: int,
                          timeout: float = 10.0) -> datetime:
    """notAfter of the cert a listener actually SERVES (a real TLS handshake;
    the chain is deliberately not verified: only the date is read). Raises
    OSError (ssl.SSLError included) when it cannot connect or handshake."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((address, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise OSError("peer presented no certificate")
    return x509.load_der_x509_certificate(der).not_valid_after_utc


def _read_probe(p: Probe, probe, expect: datetime):
    """Retry until the probe serves `expect` or attempts run out ->
    (served datetime | None, error text | None)."""
    last_err, got = None, None
    for attempt in range(max(1, PROBE_ATTEMPTS)):
        try:
            got = probe(p)
            last_err = None
            if got == expect:
                return got, None
        except Exception as exc:
            got, last_err = None, f"cannot read: {type(exc).__name__}: {exc}"
        if attempt < PROBE_ATTEMPTS - 1:
            time.sleep(PROBE_DELAY_S)
    return got, last_err


# --------------------------------------------------------------- fetch/restart

def _default_fetch(config: Config) -> str:
    try:
        cp = subprocess.run(config.fetch_argv, cwd=config.fetch_cwd,
                            capture_output=True, timeout=FETCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RenewRefused(
            f"fetch command timed out after {FETCH_TIMEOUT_S}s") from None
    except OSError as exc:
        raise RenewRefused(
            f"fetch command could not run: {type(exc).__name__}") from None
    out = cp.stdout.decode("utf-8", "replace")
    err = cp.stderr.decode("utf-8", "replace")
    if cp.returncode != 0:
        # stderr helps diagnose (tool logged out, PC asleep); stdout is
        # where the key would be and is never quoted.
        tail = _redact(err.strip(), _body_lines(out + "\n" + err))[:300]
        raise RenewRefused(f"fetch command failed: exit status "
                           f"{cp.returncode}" + (f" ({tail})" if tail else ""))
    return out


def _default_restart(config: Config, unit: str) -> None:
    try:
        cp = subprocess.run(list(config.restart_argv) + [unit],
                            capture_output=True, timeout=RESTART_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {RESTART_TIMEOUT_S}s") from None
    if cp.returncode != 0:
        raise RuntimeError(f"exit status {cp.returncode}")


# ------------------------------------------------------------------- run state

def _write_new(path: str, data: bytes, mode: int) -> None:
    """Create `path` exclusively with `mode` AT CREATION (no wider window)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), mode)
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class _Cleanup:
    """Everything this run created that must not outlive it (C8, C16)."""

    def __init__(self):
        self.files: list = []
        self.dirs: list = []

    def run(self):
        for f in self.files:
            _rm(f)
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)


def _acquire_lock(path: str):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _utc(dtv: datetime) -> datetime:
    return dtv.astimezone(timezone.utc) if dtv.tzinfo else dtv.replace(
        tzinfo=timezone.utc)


def _fmt(dtv) -> str:
    return dtv.strftime("%Y-%m-%dT%H:%M:%SZ") if dtv else "unknown"


def run(config, *, dry_run=False, fetch=None, restart=None, probe=None,
        now=None) -> int:
    """One renewal pass; returns the C19 exit code. Collaborators are
    injectable: fetch(config) -> str, restart(unit) -> None (raises on
    failure), probe(Probe) -> datetime."""
    now = _utc(now or datetime.now(timezone.utc))
    fetch = fetch or _default_fetch
    restart = restart or (lambda unit: _default_restart(config, unit))
    probe = probe or (lambda p: probe_served_notafter(p.address, p.port))
    secrets: list = []

    def say(msg: str, err: bool = False) -> None:
        print(_redact(msg, secrets), file=sys.stderr if err else sys.stdout,
              flush=True)

    try:
        lock_fd = _acquire_lock(config.lock_path)
    except OSError as exc:
        say(f"cert_renew: cannot open the lock {config.lock_path}: "
            f"{type(exc).__name__}", err=True)
        return EXIT_REFUSED
    if lock_fd is None:
        say("cert_renew: another run holds the lock; exiting without "
            "touching anything", err=True)
        return EXIT_LOCKED
    old_umask = os.umask(0o077)
    cleanup = _Cleanup()
    try:
        return _run_locked(config, dry_run, fetch, restart, probe, now, say,
                           secrets, cleanup)
    finally:
        cleanup.run()
        os.umask(old_umask)
        os.close(lock_fd)


def _run_locked(config, dry_run, fetch, restart, probe, now, say, secrets,
                cleanup) -> int:
    host, live_dir = config.host, config.live_dir
    live_cert = os.path.join(live_dir, f"{host}.crt")
    live_key = os.path.join(live_dir, f"{host}.key")
    new_cert, new_key = live_cert + ".new", live_key + ".new"
    backup = os.path.join(live_dir, "old-" + now.strftime("%Y%m%dT%H%M%SZ"))

    # ---- phase 1: nothing live is touched; any failure here is exit 1 ----
    try:
        try:
            text = fetch(config)
        except RenewRefused as exc:
            say(f"cert_renew: refused: {exc.reason}", err=True)
            return EXIT_REFUSED
        secrets.extend(_body_lines(text))
        try:
            cert_pem, key_pem = parse_fetch_output(text)
        except RenewRefused as exc:
            say(f"cert_renew: refused: {exc.reason}", err=True)
            return EXIT_REFUSED
        del text

        staging = tempfile.mkdtemp(dir=live_dir, prefix=".renew.")
        cleanup.dirs.append(staging)
        os.chmod(staging, 0o700)
        s_cert = os.path.join(staging, f"{host}.crt")
        s_key = os.path.join(staging, f"{host}.key")
        _write_new(s_key, key_pem.encode("utf-8"), 0o600)
        _write_new(s_cert, cert_pem.encode("utf-8"), 0o644)
        try:
            verdict = validate_staged(s_cert, s_key, live_cert, host)
        except RenewRefused as exc:
            say(f"cert_renew: refused: {exc.reason}", err=True)
            return EXIT_REFUSED

        live_after = _load_cert(live_cert).not_valid_after_utc
        new_after = _load_cert(s_cert).not_valid_after_utc
        if verdict == "noop":
            say(f"cert_renew: no change; the staged cert has the live "
                f"notAfter {_fmt(live_after)}")
            return EXIT_OK
        if dry_run:
            say(f"cert_renew: dry-run ok; would install notAfter "
                f"{_fmt(new_after)} (live {_fmt(live_after)})")
            return EXIT_OK

        # stage beside the targets (same filesystem => atomic rename)
        for stale in (new_cert, new_key):
            _rm(stale)                          # a killed earlier run's leftover
        cleanup.files += [new_key, new_cert]
        _write_new(new_key, key_pem.encode("utf-8"), 0o600)
        _write_new(new_cert, cert_pem.encode("utf-8"), 0o644)

        # C10: back up the previous pair before anything live changes
        os.mkdir(backup, 0o700)
        os.chmod(backup, 0o700)
        try:
            shutil.copy2(live_key, backup)
            shutil.copy2(live_cert, backup)
        except BaseException:
            shutil.rmtree(backup, ignore_errors=True)
            raise
    except Exception as exc:
        say(f"cert_renew: refused: unexpected {type(exc).__name__}: {exc}",
            err=True)
        return EXIT_REFUSED

    # ---- phase 2: swap, restart, probe; any failure rolls back (exit 2) ----
    step = "swap"
    try:
        step = "swap of the private key"
        os.replace(new_key, live_key)
        step = "swap of the certificate"
        os.replace(new_cert, live_cert)
        for unit in config.units:
            step = f"restart of {unit}"
            restart(unit)
        for p in config.probes:
            step = f"probe {p.label} ({p.address}:{p.port})"
            got, err = _read_probe(p, probe, new_after)
            if got != new_after:
                raise RuntimeError(err or f"serves notAfter {_fmt(got)}, "
                                   f"expected {_fmt(new_after)}")
    except Exception as exc:
        say(f"cert_renew: FAILED at {step}: {type(exc).__name__}: {exc}; "
            "rolling back", err=True)
        return _rollback(config, restart, probe, backup, live_cert, live_key,
                         live_after, say, cleanup)
    say(f"cert_renew: renewed {host}: notAfter {_fmt(live_after)} -> "
        f"{_fmt(new_after)}; previous pair kept in {backup}")
    return EXIT_OK


def _rollback(config, restart, probe, backup, live_cert, live_key, old_after,
              say, cleanup) -> int:
    """Restore the backed-up pair (key first, cert last), restart every unit
    again, then re-probe and REPORT (probe-only listeners cannot be restarted
    by us). 2 when restored, 3 when the restore itself failed."""
    try:
        # Phase A: copy BOTH backup files to temps beside the targets. A
        # failure here (a missing or unreadable backup file) leaves the live
        # pair completely untouched, never a restored key beside the new cert.
        staged = []
        for target in (live_key, live_cert):            # key first, cert last
            tmp = target + ".rollback"
            cleanup.files.append(tmp)
            _rm(tmp)
            shutil.copy2(os.path.join(backup, os.path.basename(target)), tmp)
            staged.append((tmp, target))
        # Phase B: the two renames, back to back, key first, cert last.
        for tmp, target in staged:
            os.replace(tmp, target)
    except Exception as exc:
        say(f"cert_renew: ROLLBACK FAILED ({type(exc).__name__}: {exc}); the "
            f"live pair may be inconsistent. The previous pair is in "
            f"{backup} -- restore it by hand (key first, then cert) and "
            "restart the units", err=True)
        return EXIT_ROLLBACK_FAILED
    for unit in config.units:
        try:
            restart(unit)
        except Exception as exc:
            say(f"cert_renew: rollback: restart of {unit} failed again: "
                f"{type(exc).__name__}: {exc}", err=True)
    for p in config.probes:
        got, err = _read_probe(p, probe, old_after)
        if err:
            state = err
        elif got == old_after:
            state = f"serving the previous cert (notAfter {_fmt(got)})"
        else:
            state = f"STILL SERVING notAfter {_fmt(got)} -- not the previous cert"
        say(f"cert_renew: rollback: probe {p.label} ({p.address}:{p.port}) "
            f"{state}", err=True)
    say(f"cert_renew: rolled back to the previous pair (kept in {backup})",
        err=True)
    return EXIT_ROLLED_BACK


# ------------------------------------------------------- failure crossing (C20)

_MEANING = {
    1: "refused before touching the live pair",
    2: "swap or probe failed and was rolled back to the previous pair; a unit "
       "may still be down, check the journal for `failed again`",
    3: "ROLLBACK FAILED: the live pair may be inconsistent, the previous pair "
       "is kept in an old-* dir beside it",
}


def alarm_failed(exit_code, *, url, agent, to_agent, destination,
                 service_result=None, opener=None) -> int:
    """Post ONE addressed crossing for a failed renew run. Exit 4 (locked)
    never pages. Non-zero if the post failed."""
    if exit_code == EXIT_LOCKED:
        return 0
    from umwelt import world_backup
    code = "unknown" if exit_code is None else str(exit_code)
    meaning = _MEANING.get(exit_code) if isinstance(exit_code, int) else None
    detail = f"exit code {code}" + (f", {meaning}" if meaning else "")
    if service_result:
        detail += f", unit result {service_result}"
    message = (f"umwelt tailnet cert renew FAILED ({detail}) -- see "
               "`journalctl --user -u umwelt-cert-renew.service`")
    return world_backup.alarm(url, agent=agent, to_agent=to_agent,
                              destination=destination, message=message,
                              opener=opener)


# ------------------------------------------------------------------------ main

def _parse_probe(spec: str) -> Probe:
    try:
        label, _, rest = spec.partition("=")
        address, _, port = rest.rpartition(":")
        if not (label and address and port):
            raise ValueError
        return Probe(label, address, int(port))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"bad --probe {spec!r}: want LABEL=ADDRESS:PORT") from None


def _exit_status(raw):
    """Systemd's MONITOR_EXIT_STATUS: an int for a normal exit, a signal name
    or number (with MONITOR_EXIT_CODE=killed/dumped) otherwise."""
    if raw is None or raw == "":
        return None
    if os.environ.get("MONITOR_EXIT_CODE") in ("killed", "dumped"):
        return f"signal {raw}"
    try:
        return int(raw)
    except ValueError:
        return raw


def main(argv=None) -> int:
    env = os.environ
    p = argparse.ArgumentParser(prog="umwelt.cert_renew",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="fetch and validate only; change nothing")
    p.add_argument("--host", default=None,
                   help=f"overrides ${ENV_HOST}")
    p.add_argument("--live-dir", default=None,
                   help=f"overrides ${ENV_LIVE_DIR} (default ~/.umwelt/tls)")
    p.add_argument("--fetch-cwd", default=None,
                   help=f"overrides ${ENV_FETCH_CWD}")
    p.add_argument("--lock-path", default=None)
    p.add_argument("--unit", action="append", default=None,
                   help="unit to restart, in order (repeatable; replaces the "
                        "defaults)")
    p.add_argument("--probe", action="append", default=None, type=_parse_probe,
                   metavar="LABEL=ADDRESS:PORT",
                   help=f"listener to probe (repeatable; replaces ${ENV_PROBES})")
    p.add_argument("--restart-argv", default=None,
                   help="restart command prefix, shell-split "
                        "(default: systemctl --user restart)")
    p.add_argument("--alarm-failed", action="store_true",
                   help="post the failure crossing instead of renewing")
    p.add_argument("--exit-code", default=None,
                   help="the failed run's exit code (default "
                        "$MONITOR_EXIT_STATUS, set by systemd for OnFailure=)")
    p.add_argument("--url", default=None)
    p.add_argument("--agent", default=None,
                   help=f"overrides ${ENV_ALARM_FROM} (default "
                        f"{DEFAULT_ALARM_FROM!r})")
    p.add_argument("--to-agent", default=None,
                   help=f"overrides ${ENV_ALARM_TO}")
    p.add_argument("--destination", default=None,
                   help=f"overrides ${ENV_ALARM_PLACE}")
    a = p.parse_args(argv)

    if a.alarm_failed:
        try:
            to_agent, destination = load_alarm_target(
                env, to_agent=a.to_agent, destination=a.destination)
        except ConfigRefused as exc:
            print(exc.message(), file=sys.stderr, flush=True)
            return EXIT_REFUSED
        from umwelt import client_auth
        raw = a.exit_code if a.exit_code is not None else env.get(
            "MONITOR_EXIT_STATUS")
        agent = a.agent or (env.get(ENV_ALARM_FROM) or "").strip() \
            or DEFAULT_ALARM_FROM
        return alarm_failed(
            _exit_status(raw),
            url=client_auth.resolve_world_url(explicit=a.url), agent=agent,
            to_agent=to_agent, destination=destination,
            service_result=env.get("MONITOR_SERVICE_RESULT"))

    try:
        cfg = load_config(
            env, host=a.host, live_dir=a.live_dir, fetch_cwd=a.fetch_cwd,
            probes=a.probe, units=a.unit, lock_path=a.lock_path,
            restart_argv=shlex.split(a.restart_argv) if a.restart_argv
            else None)
    except ConfigRefused as exc:
        print(exc.message(), file=sys.stderr, flush=True)
        return EXIT_REFUSED
    return run(cfg, dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
