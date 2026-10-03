"""Durable JSON state helpers shared by every worker.

One implementation instead of a hand-rolled ``tmp + os.replace`` in each
module. ``save`` serialises BEFORE touching the file (a TypeError can never
truncate the target), writes a sibling temp file, fsyncs it, then renames over
the target. It returns False instead of raising: a failed state write must not
kill a worker loop, but callers that need delivery semantics check the result.

Do not use this for a file that is a single-file bind mount (os.replace would
swap the inode the host sees); mount the directory instead.
"""
import json
import logging
import os
import pathlib

log = logging.getLogger(__name__)


def load(path, default=None):
    """Parsed JSON from ``path``; ``default`` when missing, empty or corrupt
    (corruption is logged once per call - callers decide whether to alert)."""
    p = pathlib.Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as e:
        log.warning("state read failed %s: %s", p.name, e)
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except ValueError as e:
        log.warning("state file %s is corrupt (%s)", p.name, e)
        return default


def save(path, data, *, indent=None, sort_keys=False) -> bool:
    """Atomic write; True on success, False (logged) on any failure."""
    p = pathlib.Path(path)
    try:
        payload = json.dumps(data, indent=indent, sort_keys=sort_keys,
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        log.warning("state serialise failed %s: %s", p.name, e)
        return False
    tmp = p.with_name(p.name + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        return True
    except OSError as e:
        log.warning("state write failed %s: %s", p.name, e)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
