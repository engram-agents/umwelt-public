"""Independent cert-expiry alarm (umwelt #1108).

    python -m umwelt.cert_alarm [--probe LABEL=ADDRESS:PORT ...] [--url U]

NO HOST IDENTITY LIVES IN THIS CODE. The per-port probe addresses and the
recipient come ONLY from an install-time systemd drop-in (`Environment=` lines,
written by deploy/install-cert-renew.sh) or the environment:

    UMWELT_CERT_PROBES       PORT=ADDRESS,... covering every default port
    UMWELT_CERT_ALARM_TO     the agent who hears the alarm
    UMWELT_CERT_ALARM_PLACE  the destination place id of the addressed crossing
    optional: UMWELT_CERT_ALARM_FROM (default "cert-alarm"),
              UMWELT_CERT_ALARM_OFFICE (default "place_office")

With a required key unset, empty or malformed this refuses: exit 1, the message
names the key(s) and the drop-in path, and nothing was read or posted.

A backstop for the renewer: it reads the cert each probe address actually
SERVES (a TLS handshake -- never the files on disk) and posts ONE addressed
crossing when any listener has fewer than 21 days left, or cannot be read at
all (unknown is not ok). When any listener has fewer than 7 days left the
same crossing is also posted to the Office.

This module imports NOTHING from `umwelt.cert_renew` and `cert_renew` imports
nothing from here (pinned by tests/test_cert_alarm.py::test_A6_*): if the
renewer's logic is what broke, the alarm must not break with it. The TLS reader
below is therefore its own, not shared with the renewer's probe. What the two
DO share is the posting path: both post through world_backup.alarm, so a break
there silences both.

Opening an issue is OFF: this module has no code path that does it (A7).
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography import x509

WARN_DAYS = 21.0
ESCALATE_DAYS = 7.0
DEFAULT_PORTS = (5006, 5007, 5008, 5009, 5014)
DEFAULT_FROM = "cert-alarm"
OFFICE_PLACE = "place_office"

ENV_PROBES = "UMWELT_CERT_PROBES"
ENV_ALARM_TO = "UMWELT_CERT_ALARM_TO"
ENV_ALARM_PLACE = "UMWELT_CERT_ALARM_PLACE"
ENV_ALARM_FROM = "UMWELT_CERT_ALARM_FROM"
ENV_ALARM_OFFICE = "UMWELT_CERT_ALARM_OFFICE"
DROPIN = "~/.config/systemd/user/umwelt-cert-alarm.service.d/identity.conf"
EXIT_REFUSED = 1


@dataclass(frozen=True)
class Probe:
    label: str
    address: str
    port: int



class ConfigRefused(Exception):
    """A required config key is missing/empty/malformed; `.problems` is a list
    of (key, why)."""

    def __init__(self, problems: list):
        self.problems = problems
        super().__init__("config refused: " + ", ".join(k for k, _ in problems))

    def message(self) -> str:
        detail = "; ".join(f"{k} {why}" for k, why in self.problems)
        return (f"cert_alarm: refusing to run: {detail}. Set it in {DROPIN} "
                "(written by deploy/install-cert-renew.sh) or the "
                "environment.")


def parse_probes(text: str) -> list:
    """`PORT=ADDRESS,PORT=ADDRESS,...` -> [Probe(label=str(port), ...)].
    Raises ValueError with a short reason. (Deliberately its own copy: this
    module shares no logic with the renewer.)"""
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


def load_config(env, *, probes=None, to_agent=None, destination=None) -> tuple:
    """(probes, to_agent, destination) from `env` (a mapping); a non-None
    keyword is an override that replaces its env key (a probe override skips
    the PROBES key and its default-port coverage check). Raises
    ConfigRefused naming every missing/empty/malformed REQUIRED key."""
    problems: list = []
    if probes is None:
        raw = (env.get(ENV_PROBES) or "").strip()
        if not raw:
            problems.append((ENV_PROBES, "is not set"))
        else:
            try:
                probes = parse_probes(raw)
            except ValueError as exc:
                problems.append((ENV_PROBES, f"is malformed ({exc})"))
            else:
                have = {p.port for p in probes}
                gone = [p for p in DEFAULT_PORTS if p not in have]
                if gone:
                    problems.append((ENV_PROBES, "misses the port(s) "
                                     + ", ".join(map(str, gone))))
    to = to_agent or (env.get(ENV_ALARM_TO) or "").strip()
    if not to:
        problems.append((ENV_ALARM_TO, "is not set"))
    dest = destination or (env.get(ENV_ALARM_PLACE) or "").strip()
    if not dest:
        problems.append((ENV_ALARM_PLACE, "is not set"))
    if problems:
        raise ConfigRefused(problems)
    return list(probes), to, dest


@dataclass(frozen=True)
class Reading:
    label: str
    days_left: float | None
    error: str | None


def served_notafter(address: str, port: int, timeout: float = 10.0) -> datetime:
    """notAfter of the cert `address:port` serves right now (handshake only;
    the chain is not verified, only the date is read)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = socket.create_connection((address, port), timeout=timeout)
    try:
        tls = ctx.wrap_socket(sock)
        try:
            der = tls.getpeercert(binary_form=True)
        finally:
            tls.close()
    except BaseException:
        sock.close()
        raise
    if not der:
        raise OSError("peer presented no certificate")
    return x509.load_der_x509_certificate(der).not_valid_after_utc


def check(probes, *, now, probe=None) -> list:
    """One Reading per probe. `probe(address, port) -> datetime` (default: the
    real TLS reader). A probe that cannot be read yields days_left=None and an
    error string -- never a skipped entry."""
    probe = probe or served_notafter
    out = []
    for p in probes:
        try:
            not_after = probe(p.address, p.port)
            days = (not_after - now).total_seconds() / 86400.0
            out.append(Reading(p.label, days, None))
        except Exception as exc:
            out.append(Reading(p.label, None, f"{type(exc).__name__}: {exc}"))
    return out


def decide(readings, *, warn_days=WARN_DAYS,
           escalate_days=ESCALATE_DAYS) -> tuple[bool, bool]:
    """(fire, escalate_to_office). A listener fails when it has STRICTLY fewer
    than warn_days left or cannot be read; the Office is added only when a
    readable listener has strictly fewer than escalate_days left."""
    fire = any(r.days_left is None or r.days_left < warn_days for r in readings)
    escalate = any(r.days_left is not None and r.days_left < escalate_days
                   for r in readings)
    return fire, escalate


def _message(readings, warn_days: float) -> str:
    lines = []
    for r in readings:
        if r.days_left is None:
            lines.append(f"  {r.label}: cannot read ({r.error})")
        elif r.days_left < warn_days:
            lines.append(f"  {r.label}: {r.days_left:.1f} days left")
    return (f"umwelt tailnet cert expiry alarm: the certificate served by "
            f"{len(lines)} listener(s) has under {warn_days:g} days left or "
            "cannot be read (this checks what is SERVED, not the files):\n"
            + "\n".join(lines)
            + "\nThe daily renewer (umwelt-cert-renew) should have replaced "
              "it: `journalctl --user -u umwelt-cert-renew.service`.")


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


def main(argv=None) -> int:
    env = os.environ
    p = argparse.ArgumentParser(prog="umwelt.cert_alarm",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--probe", action="append", default=None, type=_parse_probe,
                   metavar="LABEL=ADDRESS:PORT",
                   help=f"listener to read (repeatable; replaces ${ENV_PROBES})")
    p.add_argument("--warn-days", type=float, default=WARN_DAYS)
    p.add_argument("--escalate-days", type=float, default=ESCALATE_DAYS)
    p.add_argument("--url", default=None)
    p.add_argument("--agent", default=None,
                   help=f"overrides ${ENV_ALARM_FROM} (default {DEFAULT_FROM!r})")
    p.add_argument("--to-agent", default=None, help=f"overrides ${ENV_ALARM_TO}")
    p.add_argument("--destination", default=None,
                   help=f"overrides ${ENV_ALARM_PLACE}")
    p.add_argument("--office-place", default=None,
                   help=f"overrides ${ENV_ALARM_OFFICE} "
                        f"(default {OFFICE_PLACE!r})")
    a = p.parse_args(argv)

    try:
        probes, to_agent, destination = load_config(
            env, probes=a.probe, to_agent=a.to_agent,
            destination=a.destination)
    except ConfigRefused as exc:
        print(exc.message(), file=sys.stderr, flush=True)
        return EXIT_REFUSED
    agent = a.agent or (env.get(ENV_ALARM_FROM) or "").strip() or DEFAULT_FROM
    office = a.office_place or (env.get(ENV_ALARM_OFFICE) or "").strip() \
        or OFFICE_PLACE

    readings = check(probes, now=datetime.now(timezone.utc))
    fire, escalate = decide(readings, warn_days=a.warn_days,
                            escalate_days=a.escalate_days)
    if not fire:
        print(f"cert_alarm: ok; every listener has >= {a.warn_days:g} days left")
        return 0

    from umwelt import client_auth, world_backup
    url = client_auth.resolve_world_url(explicit=a.url)
    destinations = [destination]
    if escalate:
        destinations.append(office)
    message = _message(readings, a.warn_days)
    failed = False
    for dest in destinations:               # attempt every destination
        rc = world_backup.alarm(url, agent=agent, to_agent=to_agent,
                                destination=dest, message=message)
        failed = failed or rc != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
