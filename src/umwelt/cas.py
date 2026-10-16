"""Content-addressed store (CAS) for Umwelt.

Blobs are stored by sha256 under ``<db_dir>/cas/<first2>/<rest>`` (git
object-store layout — the two-level split avoids single-directory inode limits
at scale). Content-addressed, so:

- the ref **is** the hash → ``store`` is idempotent (same content → same ref,
  no duplicate write), and ``retrieve(ref)`` either returns the exact bytes or
  misses. There is nothing to forge: you cannot register a ref for content you
  did not hash.
- append-only: a blob is never mutated or deleted (v0 has no GC — see
  ``docs/CAPABILITY-CHANNEL.md``).

An inventory row in ``cas_entries`` (see ``db.py``) makes the store queryable
without a filesystem scan. This module owns the on-disk bytes + the inventory
write; the capability-channel handler (later) is the only caller a *scroll* can
reach — CAS itself has no socket and no notion of identity.

Design + review: ``docs/CAPABILITY-CHANNEL.md`` (Sol #3795, ``ob_0908``).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from . import db

_REF_PREFIX = "cas:sha256:"
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def cas_dir(db_path: str) -> Path:
    """The CAS root, derived from db_path — no config (Sol #3795)."""
    return Path(db_path).parent / "cas"


def _blob_path(db_path: str, hexhash: str) -> Path:
    return cas_dir(db_path) / hexhash[:2] / hexhash[2:]


def ref_for(hexhash: str) -> str:
    return _REF_PREFIX + hexhash


def parse_ref(ref: str) -> str | None:
    """Return the hex hash for a well-formed ref, else None.

    Rejects anything that isn't exactly ``cas:sha256:<64 lowercase hex>`` — a
    malformed ref must miss cleanly, never touch an unexpected path.
    """
    if not isinstance(ref, str) or not ref.startswith(_REF_PREFIX):
        return None
    hexhash = ref[len(_REF_PREFIX):]
    return hexhash if _HEX64.match(hexhash) else None


def store(conn, db_path: str, content, ref_type: str = "blob") -> str:
    """Store content (bytes or str) and return its ``cas:sha256:<hash>`` ref.

    Idempotent: storing identical content twice writes the blob once and returns
    the same ref. Bytes on disk are written atomically (temp + rename) so a
    concurrent reader never sees a partial blob.
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError("content must be bytes or str")
    content = bytes(content)

    hexhash = hashlib.sha256(content).hexdigest()
    path = _blob_path(db_path, hexhash)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp in the same dir, then atomic rename — a fixed temp name
        # would race under concurrent stores of identical content.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp, path)  # atomic within a filesystem
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # Inventory: idempotent on the hash primary key.
    conn.execute(
        "INSERT OR IGNORE INTO cas_entries(hash, stored_at, size_bytes, ref_type) "
        "VALUES(?, ?, ?, ?)",
        (hexhash, db.now_iso(), len(content), ref_type),
    )
    conn.commit()
    return ref_for(hexhash)


def retrieve(conn, db_path: str, ref: str) -> bytes | None:
    """Return the exact bytes for a ref, or None (malformed / not-found /
    integrity-failure).

    Self-verifying: the bytes are re-hashed and checked against the ref. If a
    blob on disk has been corrupted or tampered with so it no longer matches its
    address, retrieve returns None rather than hand back content under a hash it
    doesn't have — the content-addressing integrity guarantee.

    ``conn`` is unused today — reserved so the future capability-channel handler
    can log retrievals (who read what) without a signature break.
    """
    hexhash = parse_ref(ref)
    if hexhash is None:
        return None
    path = _blob_path(db_path, hexhash)
    # Read-then-except (not exists-then-read): honours the "None on any miss"
    # contract even if the blob vanishes between a check and the read, and it's
    # one syscall fewer.
    try:
        content = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return None
    if hashlib.sha256(content).hexdigest() != hexhash:
        return None  # corruption / tampering — address no longer matches content
    return content
