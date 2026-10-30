"""World packages: a world's starting content as data (umwelt#1043).

A package is a directory `worlds/<name>/` holding `world.json` (format 1 or
2, strict: unknown keys are refused). Format 2 (slice 2) adds `links`,
`things` (readable text and the empty charge board) and the optional
`residences` role. The engine loads it ONCE, at boot, and
only when the `places` table is empty; an existing world is never read,
validated or changed (`boot_load` returns "skipped" without opening the
package path). The load is one transaction: places in list order, the
roles, (format 2) the links and things, and a single provenance row in
`world_package`; any failure rolls everything back, source files included, and a fresh world with a missing or invalid package
raises `WorldPackageError` so the server can refuse to start.

`places` has no author column, so the provenance the design calls
"author=world:<name>" lives in the `world_package` table (name, format,
load time, the place ids created) instead.

This module does not import `db` or `server` at import time (db lazily
imports this module for `seed_places`), and it is stdlib-only so a copy of
it can be exercised from any directory layout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WORLD_FILE = "world.json"
FORMAT = 1          # the original format; still loads exactly as before
FORMAT_2 = 2        # + links, things, the residences role
SUPPORTED_FORMATS = (FORMAT, FORMAT_2)
STARTER_NAME = "starter-village"
REQUIRED_ROLES = ("hub", "workshop")
OPTIONAL_ROLES_F2 = ("residences",)

MAX_BYTES = 1024 * 1024  # world.json larger than this is refused
RESERVED_ID_PREFIX = "place_post_"  # seed_posts owns these ids

_NAME_RE = re.compile(r"[a-z0-9-]+")
_TOP_KEYS = {"format", "name", "roles", "places"}
_TOP_KEYS_F2 = _TOP_KEYS | {"links", "things"}
_LINK_KEYS = {"src", "dst", "label"}
_THING_KEYS = {"key", "name", "place", "description", "kind", "source",
               "checklist"}
_THING_REQUIRED = ("key", "name", "place", "description", "kind")
THING_KINDS = ("readable", "charge_board")
AUTHOR_PREFIX = "world:"      # things carry author = "world:<package name>"
MAX_SOURCE_BYTES = MAX_BYTES  # one package text file
_PLACE_KEYS = {"id", "kind", "owner", "parent", "label", "description"}
_PLACE_REQUIRED = ("id", "kind", "label")


class WorldPackageError(ValueError):
    """A package is missing or invalid. The message names file and field."""


@dataclass(frozen=True)
class Package:
    name: str
    format: int
    roles: dict
    places: tuple
    source: str
    links: tuple = ()
    things: tuple = ()


def _engine_place_id_ok(value) -> bool:
    # The ONE id rule: the same pattern create_place enforces. Imported
    # lazily (server imports this module's callers; no cycle at import time).
    from . import server
    return isinstance(value, str) and bool(server._PLACE_ID_RE.match(value))


def place_id_ok(value) -> bool:
    return _engine_place_id_ok(value)


# ------------------------------------------------------------ locating

def candidate_paths(start=None):
    """Where the shipped package may live, nearest ancestor first."""
    here = Path(start if start is not None else __file__).resolve()
    return [p / "worlds" / STARTER_NAME for p in here.parents]


def find_shipped_package(start=None) -> Path:
    """Walk the parents of `start` (default: this file) for
    `worlds/starter-village/world.json`. Never a fixed parents[N]: the
    installed tree is not laid out like the source tree."""
    tried = candidate_paths(start)
    for cand in tried:
        if (cand / WORLD_FILE).is_file():
            return cand
    raise WorldPackageError(
        "no shipped world package found; tried: "
        + ", ".join(str(c) for c in tried))


def default_package_path() -> Path:
    """$UMWELT_WORLD_PACKAGE if set, else the shipped starter village."""
    env = os.environ.get("UMWELT_WORLD_PACKAGE", "").strip()
    if env:
        return Path(env)
    return find_shipped_package()


# ---------------------------------------------------------- validation

def _fail(file, field, problem):
    raise WorldPackageError(f"{file}: {field}: {problem}")


def load_package(path, place_id_ok=None) -> Package:
    """Read and validate a package. `path` is the package directory (or the
    world.json file itself). Raises WorldPackageError naming file + field."""
    ok = place_id_ok if place_id_ok is not None else _engine_place_id_ok
    p = Path(path)
    f = p if p.suffix == ".json" else p / WORLD_FILE
    try:
        size = f.stat().st_size
        if size > MAX_BYTES:
            raise WorldPackageError(
                f"{f}: file is too large ({size} bytes; the limit is "
                f"{MAX_BYTES} bytes)")
        raw = f.read_bytes()
    except OSError as exc:
        raise WorldPackageError(
            f"{f}: cannot read the world package ({exc.strerror or exc})"
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorldPackageError(
            f"{f}: not valid UTF-8 text ({exc.reason} at byte {exc.start})"
        ) from exc
    try:
        doc = json.loads(text)
    except RecursionError as exc:
        raise WorldPackageError(
            f"{f}: JSON is nested too deeply") from exc
    except ValueError as exc:
        raise WorldPackageError(f"{f}: not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        _fail(f, "top level", "must be a JSON object")
    fmt = doc.get("format")
    top_keys = (_TOP_KEYS_F2 if type(fmt) is int and fmt == FORMAT_2
                else _TOP_KEYS)
    for key in doc:
        if key not in top_keys:
            _fail(f, key, f"unknown top-level key {key!r}")
    for key in ("format", "name", "roles", "places"):
        if key not in doc:
            _fail(f, key, "required key is missing")
    if type(fmt) is not int or fmt not in SUPPORTED_FORMATS:
        _fail(f, "format", f"must be the integer 1 or 2, got {fmt!r}")
    name = doc["name"]
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        _fail(f, "name", f"must match [a-z0-9-]+, got {name!r}")

    raw_places = doc["places"]
    if not isinstance(raw_places, list) or not raw_places:
        _fail(f, "places", "must be a non-empty list")
    seen: list[str] = []
    places = []
    for i, rp in enumerate(raw_places):
        at = f"places[{i}]"
        if not isinstance(rp, dict):
            _fail(f, at, "must be an object")
        for key in rp:
            if key not in _PLACE_KEYS:
                _fail(f, at, f"unknown key {key!r}")
        for key in _PLACE_REQUIRED:
            if key not in rp:
                _fail(f, f"{at}.{key}", "required key is missing")
        pid = rp["id"]
        if not ok(pid):
            _fail(f, f"{at}.id", f"{pid!r} is not a valid place id")
        if pid.startswith(RESERVED_ID_PREFIX):
            _fail(f, f"{at}.id",
                  f"{pid!r} uses the reserved prefix {RESERVED_ID_PREFIX!r} "
                  "(agents' private Posts)")
        if pid in seen:
            _fail(f, f"{at}.id", f"duplicate place id {pid!r}")
        if rp["kind"] not in ("public", "private"):
            _fail(f, f"{at}.kind",
                  f"must be 'public' or 'private', got {rp['kind']!r}")
        owner = rp.get("owner")
        if rp["kind"] == "public":
            if owner is not None:
                _fail(f, f"{at}.owner", "must be null for a public place")
        elif not isinstance(owner, str) or not owner:
            _fail(f, f"{at}.owner",
                  "must be a non-empty string for a private place")
        parent = rp.get("parent")
        if parent is not None:
            if parent not in seen:
                _fail(f, f"{at}.parent",
                      f"{parent!r} must name a place earlier in the list "
                      "(or be null)")
        label = rp["label"]
        if not isinstance(label, str) or not label.strip():
            _fail(f, f"{at}.label", "must be a non-empty string")
        desc = rp.get("description", "")
        if not isinstance(desc, str):
            _fail(f, f"{at}.description", "must be a string")
        seen.append(pid)
        places.append({"id": pid, "kind": rp["kind"], "owner": owner,
                       "parent": parent, "label": label,
                       "description": desc})

    roles = doc["roles"]
    if not isinstance(roles, dict):
        _fail(f, "roles", "must be an object")
    known_roles = REQUIRED_ROLES + (OPTIONAL_ROLES_F2 if fmt == FORMAT_2
                                    else ())
    for role in roles:
        if role not in known_roles:
            _fail(f, f"roles.{role}", f"unknown role {role!r}")
    for role in REQUIRED_ROLES:
        if role not in roles:
            _fail(f, f"roles.{role}", "required role is missing")
    for role in roles:
        if roles[role] not in seen:
            _fail(f, f"roles.{role}",
                  f"{roles[role]!r} is not a place in this package")

    links = ()
    things = ()
    if fmt == FORMAT_2:
        links = _validate_links(f, doc.get("links", []), seen)
        things = _validate_things(f, doc.get("things", []), seen, name)
    return Package(name=name, format=fmt, roles=dict(roles),
                   places=tuple(places), source=str(f),
                   links=links, things=things)


def _validate_links(f, raw, place_ids) -> tuple:
    """links: [{src, dst, label?}] -- directed rows, both ends in this
    package. A mutual link is two entries. No cycle rule: links are lateral."""
    if not isinstance(raw, list):
        _fail(f, "links", "must be a list")
    seen_pairs = set()
    out = []
    for i, rl in enumerate(raw):
        at = f"links[{i}]"
        if not isinstance(rl, dict):
            _fail(f, at, "must be an object")
        for key in rl:
            if key not in _LINK_KEYS:
                _fail(f, at, f"unknown key {key!r}")
        for key in ("src", "dst"):
            if key not in rl:
                _fail(f, f"{at}.{key}", "required key is missing")
            if rl[key] not in place_ids:
                _fail(f, f"{at}.{key}",
                      f"{rl[key]!r} is not a place in this package")
        if rl["src"] == rl["dst"]:
            _fail(f, at, f"a place cannot link to itself ({rl['src']!r})")
        pair = (rl["src"], rl["dst"])
        if pair in seen_pairs:
            _fail(f, at, f"duplicate link {rl['src']!r} -> {rl['dst']!r}")
        seen_pairs.add(pair)
        label = rl.get("label")
        if label is not None and (not isinstance(label, str)
                                  or not label.strip()):
            _fail(f, f"{at}.label", "must be a non-empty string or null")
        out.append({"src": rl["src"], "dst": rl["dst"], "label": label})
    return tuple(out)


def _read_package_text(f, at, base: Path, rel) -> str:
    """Read one package text file, or raise naming file and field. The path
    must be relative, free of `..`, resolve INSIDE the package directory
    (no symlink out), be a regular file, and hold UTF-8 text.

    TOCTOU note: resolve-then-check and read are two steps. The read opens
    the already-resolved path with O_NOFOLLOW (the final component cannot
    be swapped for a symlink between the check and the open), re-checks that
    the opened file is a regular file, and, after the read, resolves the
    path again and refuses if it moved. A writer who can swap a directory
    component of the package between those steps can still race us; the
    package directory is operator-owned and read once at first boot, and the
    same writer could edit world.json itself, so this narrows the window
    rather than claiming to close it."""
    if not isinstance(rel, str) or not rel.strip():
        _fail(f, at, "package file path must be a non-empty string")
    if "\x00" in rel:
        _fail(f, at, "package file path contains a NUL byte")
    pure = Path(rel)
    if pure.is_absolute() or rel.startswith(("/", "\\")) \
            or re.match(r"^[A-Za-z]:", rel):
        _fail(f, at, f"package file path {rel!r} must be relative")
    if ".." in pure.parts or ".." in re.split(r"[\\/]", rel):
        _fail(f, at, f"package file path {rel!r} must not contain '..'")
    base_resolved = base.resolve()
    target = base / pure
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _fail(f, at, f"package file {rel!r} cannot be read "
                     f"({getattr(exc, 'strerror', None) or exc})")
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        _fail(f, at, f"package file {rel!r} resolves outside the package "
                     "directory")
    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        _fail(f, at, f"package file {rel!r} cannot be opened "
                     f"({exc.strerror or exc})")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            _fail(f, at, f"package file {rel!r} is not a regular file")
        if st.st_size > MAX_SOURCE_BYTES:
            _fail(f, at, f"package file {rel!r} is too large "
                         f"({st.st_size} bytes; the limit is "
                         f"{MAX_SOURCE_BYTES})")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_SOURCE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        if target.resolve(strict=True) != resolved:
            _fail(f, at, f"package file {rel!r} changed while it was read")
    except (OSError, RuntimeError):
        _fail(f, at, f"package file {rel!r} changed while it was read")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f, at, f"package file {rel!r} is not valid UTF-8 text "
                     f"({exc.reason} at byte {exc.start})")


def _validate_things(f, raw, place_ids, pkg_name) -> tuple:
    """things: [{key, name, place, description, kind, source?, checklist?}].
    Limits and the printable-name rule come from the engine's own create
    path (server), imported lazily: this module stays import-free of server."""
    if not isinstance(raw, list):
        _fail(f, "things", "must be a list")
    if not raw:
        return ()
    from . import server
    base = Path(f).parent
    keys = set()
    out = []
    for i, rt in enumerate(raw):
        at = f"things[{i}]"
        if not isinstance(rt, dict):
            _fail(f, at, "must be an object")
        for key in rt:
            if key not in _THING_KEYS:
                _fail(f, at, f"unknown key {key!r}")
        for key in _THING_REQUIRED:
            if key not in rt:
                _fail(f, f"{at}.{key}", "required key is missing")
        key = rt["key"]
        if not isinstance(key, str) or not _NAME_RE.fullmatch(key):
            _fail(f, f"{at}.key", f"must match [a-z0-9-]+, got {key!r}")
        if key in keys:
            _fail(f, f"{at}.key", f"duplicate thing key {key!r}")
        keys.add(key)
        name = rt["name"]
        if not isinstance(name, str) or not name.strip():
            _fail(f, f"{at}.name", "must be a non-empty string")
        name = name.strip()
        if len(name) > server._NAME_MAX_LEN:
            _fail(f, f"{at}.name",
                  f"exceeds the {server._NAME_MAX_LEN}-char limit")
        problem = server._name_char_problem(name)
        if problem:
            _fail(f, f"{at}.name", problem)
        desc = rt["description"]
        if not isinstance(desc, str) or not desc.strip():
            _fail(f, f"{at}.description", "must be a non-empty string")
        desc = desc.strip()
        if len(desc) > server._DESCRIPTION_MAX_LEN:
            _fail(f, f"{at}.description",
                  f"exceeds the {server._DESCRIPTION_MAX_LEN}-char limit")
        if rt["place"] not in place_ids:
            _fail(f, f"{at}.place",
                  f"{rt['place']!r} is not a place in this package")
        kind = rt["kind"]
        if kind not in THING_KINDS:
            _fail(f, f"{at}.kind",
                  f"must be one of {list(THING_KINDS)}, got {kind!r}")

        files = {}
        checklist = None
        if kind == "readable":
            src = rt.get("source")
            if not isinstance(src, dict) or not src:
                _fail(f, f"{at}.source", "a readable needs a non-empty "
                      "object of {name in the thing: package file path}")
            for relname, relpath in src.items():
                where = f"{at}.source[{relname!r}]"
                if (not isinstance(relname, str) or not relname.strip()
                        or Path(relname).is_absolute()
                        or ".." in Path(relname).parts):
                    _fail(f, f"{at}.source",
                          f"file name {relname!r} must be a relative name "
                          "without '..'")
                files[relname] = _read_package_text(f, where, base, relpath)
            if "checklist" in rt:
                cl = rt["checklist"]
                if not isinstance(cl, list):
                    _fail(f, f"{at}.checklist", "must be a list of strings")
                if len(cl) > server._CHECKLIST_MAX_ITEMS:
                    _fail(f, f"{at}.checklist",
                          f"exceeds the {server._CHECKLIST_MAX_ITEMS}-item "
                          "limit")
                for j, text in enumerate(cl):
                    if not isinstance(text, str) or not text.strip():
                        _fail(f, f"{at}.checklist[{j}]",
                              "must be a non-empty string")
                    if len(text) > server._CHECKLIST_ITEM_MAX_LEN:
                        _fail(f, f"{at}.checklist[{j}]",
                              f"exceeds the "
                              f"{server._CHECKLIST_ITEM_MAX_LEN}-char limit")
                checklist = [t.strip() for t in cl]
        else:  # charge_board: an empty board of the engine kind
            for extra in ("source", "checklist"):
                if extra in rt:
                    _fail(f, f"{at}.{extra}",
                          "a charge_board is created empty; it takes no "
                          f"{extra}")
        out.append({"key": key, "name": name, "place": rt["place"],
                    "description": desc, "kind": kind, "files": files,
                    "checklist": checklist})
    return tuple(out)


# ------------------------------------------------------------- loading

def _insert_place(conn, place) -> None:
    conn.execute(
        "INSERT INTO places(id, kind, owner, label, description, parent_id) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (place["id"], place["kind"], place["owner"], place["label"],
         place["description"], place["parent"]))


def _insert_role(conn, role, place_id) -> None:
    conn.execute("INSERT INTO world_roles(role, place_id) VALUES(?, ?)",
                 (role, place_id))


def _insert_provenance(conn, pkg) -> None:
    conn.execute(
        "INSERT INTO world_package(name, format, loaded_utc, place_ids) "
        "VALUES(?, ?, ?, ?)",
        (pkg.name, pkg.format,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
         json.dumps([p["id"] for p in pkg.places])))


def _record_thing_ids(conn, pkg, thing_ids) -> None:
    """Format 2: the {package key: thing id} map, on the provenance row.
    A format-1 row keeps thing_ids NULL (it created no things)."""
    conn.execute("UPDATE world_package SET thing_ids = ? WHERE name = ?",
                 (json.dumps(thing_ids), pkg.name))


def _insert_links(conn, pkg) -> None:
    if not pkg.links:  # format 1: touch nothing (and import nothing)
        return
    from . import db
    for ln in pkg.links:
        db.add_place_link(conn, ln["src"], ln["dst"], ln["label"],
                          mutual=False, commit=False)


def _insert_things(conn, pkg, things_dir, written) -> dict:
    """Create the package's things inside the open transaction; returns
    {key: thing id}. Source files are written to `things_dir` as they go and
    each directory is appended to `written` so a failure can remove them."""
    key_to_id = {}
    if not pkg.things:  # format 1: touch nothing (and import nothing)
        return key_to_id
    from . import db, kind_ids
    author = AUTHOR_PREFIX + pkg.name
    for t in pkg.things:
        tid = str(uuid.uuid4())
        metadata = {}
        if t["kind"] == "charge_board":
            # What the engine's own forge path stamps for an empty board of
            # this kind (the drift cell pins it): the kind id and nothing else.
            metadata["kind_id"] = kind_ids.CHARGE_BOARD_KIND_ID
        elif t["checklist"] is not None:
            metadata["checklist"] = [{"text": x, "done": False}
                                     for x in t["checklist"]]
        if t["files"]:
            from . import server
            written.append(Path(things_dir) / tid)
            try:
                server._write_source(Path(things_dir), tid, t["files"])
            except (ValueError, OSError) as exc:
                raise WorldPackageError(
                    f"{pkg.source}: things[{t['key']!r}].source: "
                    f"cannot write the thing's files ({exc})") from exc
        db.create_thing(
            conn, id=tid, place_id=t["place"], name=t["name"],
            description=t["description"], kind="readable",
            exec_command=None, metadata=metadata, author=author,
            created_at=db.now_iso(),
            weight_est=db.compute_weight_est(t["name"], t["description"]),
            commit=False)
        key_to_id[t["key"]] = tid
    return key_to_id


def _places_empty(conn) -> bool:
    return conn.execute("SELECT 1 FROM places LIMIT 1").fetchone() is None


def boot_load(conn, path=None, place_id_ok=None, things_dir=None) -> str:
    """Load the package into an EMPTY world. Returns "loaded" or "skipped".

    Skipped (nothing read, written or validated; `path` is not opened) when
    `places` already has any row. Raises WorldPackageError, having written
    nothing, when the world is empty and the package is missing/invalid.

    `things_dir` (umwelt#1043 slice 2) is where a format-2 package's readable
    things get their source files: a path, or a zero-argument callable that
    returns one (called only when a fresh world is actually loading, so an
    existing world never resolves it). Format 1 creates no things and
    ignores it.
    """
    if not _places_empty(conn):
        return "skipped"
    if conn.in_transaction:
        conn.commit()
    pkg = load_package(path if path is not None else default_package_path(),
                       place_id_ok)
    if any(t["files"] for t in pkg.things):
        if things_dir is None:
            raise WorldPackageError(
                f"{pkg.source}: things: this package has readable things but "
                "no things_dir was given to write their files to")
        if callable(things_dir):  # resolved only now, for a world that loads
            things_dir = things_dir()
    conn.execute("BEGIN IMMEDIATE")
    written: list = []
    try:
        if not _places_empty(conn):  # lost a race with another booter
            conn.rollback()
            return "skipped"
        for place in pkg.places:
            _insert_place(conn, place)
        _insert_links(conn, pkg)
        for role, place_id in pkg.roles.items():
            _insert_role(conn, role, place_id)
        thing_ids = _insert_things(conn, pkg, things_dir, written)
        _insert_provenance(conn, pkg)
        if pkg.format == FORMAT_2:
            _record_thing_ids(conn, pkg, thing_ids)
        conn.commit()
    except BaseException:
        conn.rollback()
        for d in written:  # source files are outside the transaction
            shutil.rmtree(d, ignore_errors=True)
        raise
    return "loaded"
