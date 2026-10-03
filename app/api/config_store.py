"""Single owner of the dashboard config file (watchlist, portfolio, aliases,
alertRules, chatModel).

Why this exists: the old reader fell back to defaults on ANY read error and
the next write then persisted those defaults, wiping the portfolio and the
alert rules after one torn read. Rules here:

* A read error never yields silent defaults while a last-good copy exists:
  ``read()`` serves the last-good config (in-memory first, then
  ``data/config.lastgood.json``) and logs loudly.
* ``update()`` is the ONLY write path. It starts from the stored config (or
  the last-good copy when the file is corrupt) and raises ``ConfigUnreadable``
  when neither exists - it never writes defaults over unreadable data.
* The new document is serialised BEFORE any file is touched, written to a
  same-directory temp file, fsynced and ``os.replace``d (the quadlet mounts the
  DIRECTORY ``/app/config``, so the rename is visible to the host), then the
  last-good copy is refreshed.
* One process-wide re-entrant lock serialises read-modify-write cycles
  (``update`` is a blocking call; run it via ``asyncio.to_thread`` from async
  code).

``CONFIG_PATH`` / ``lastgood_path()`` are looked up at call time so tests can
monkeypatch ``CONFIG_PATH`` and ``HISTORY_DIR``.
"""
import copy
import json
import logging
import os
import pathlib
import threading
import time
from typing import Callable

from app.api import jsonstore

log = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config/config.json")

_lock = threading.RLock()
_cache: tuple | None = None        # (file signature, parsed dict)
_lastgood: dict | None = None      # newest config known to have parsed
_state = {"source": "default", "error": None}
_logged_sig: tuple | None = None   # corrupt-file signature already reported


class ConfigUnreadable(RuntimeError):
    """The stored config cannot be parsed and no last-good copy exists."""


class ConfigWriteError(RuntimeError):
    """The new config could not be written; the stored file is unchanged."""


def lastgood_path() -> pathlib.Path:
    return pathlib.Path(os.getenv("HISTORY_DIR", "/app/data")) \
        / "config.lastgood.json"


def default_config() -> dict:
    """Bootstrap document for a box with no config at all. The watchlist is
    empty on purpose: a holding needs a listing, so bare ticker ids would be
    rejected by the registry anyway."""
    return {"watchlist": [], "chatModel": "lane", "portfolio": {}}


def _sig(p: pathlib.Path) -> tuple:
    st = p.stat()
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _parse(p: pathlib.Path) -> dict:
    """Parsed config document; raises ValueError (bad content) / OSError."""
    obj = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("config root must be a JSON object")
    return obj


def _load_lastgood() -> dict | None:
    global _lastgood
    if _lastgood is not None:
        return _lastgood
    try:
        _lastgood = _parse(lastgood_path())
    except (OSError, ValueError):
        return None
    return _lastgood


def _remember_good(cfg: dict) -> None:
    """Refresh the in-memory and on-disk last-good copy when it differs."""
    global _lastgood
    if _lastgood == cfg:
        return
    _lastgood = copy.deepcopy(cfg)
    if not jsonstore.save(lastgood_path(), cfg, indent=2):
        log.warning("config last-good copy could not be written")


def _stored() -> tuple[dict | None, str, str | None]:
    """(config, source, error). ``config`` None => file missing/unreadable
    and no usable copy; callers decide. Never raises."""
    global _cache, _logged_sig
    p = pathlib.Path(CONFIG_PATH)
    try:
        sig = _sig(p)
    except FileNotFoundError:
        lg = _load_lastgood()
        if lg is not None:
            return lg, "lastgood", "config file missing"
        return None, "missing", None
    except OSError as e:
        sig, err = None, f"stat failed: {e}"
    else:
        if _cache is not None and _cache[0] == sig:
            return _cache[1], "file", None
        try:
            cfg = _parse(p)
        except (OSError, ValueError) as e:
            err = f"{type(e).__name__}: {e}"
        else:
            _cache = (sig, cfg)
            _remember_good(cfg)
            return cfg, "file", None
    # Unreadable. Report once per distinct file state, not every 2 s poll.
    if sig is None or sig != _logged_sig:
        _logged_sig = sig
        log.error("CONFIG UNREADABLE (%s): %s - serving last-good copy "
                  "if any; writes are refused without one", p, err)
    _cache = None
    lg = _load_lastgood()
    if lg is not None:
        return lg, "lastgood", err
    return None, "unreadable", err


def read() -> dict:
    """Current config as a private deep copy; never raises.

    Order: stored file -> last-good copy -> bootstrap defaults (only when the
    file never existed or is corrupt with no last-good; see ``status()``)."""
    with _lock:
        cfg, source, err = _stored()
        if cfg is None:
            _state.update(source="default", error=err)
            return default_config()
        _state.update(source=source, error=err)
        return copy.deepcopy(cfg)


def status() -> dict:
    """{source: file|lastgood|default, error: str|None} right now.
    ``source == 'default'`` with an error means the stored config is
    unreadable and nothing better exists (GET/PUT answer 503)."""
    with _lock:
        cfg, source, err = _stored()
        _state.update(source=source if cfg is not None else "default",
                      error=err)
        return dict(_state)


def update(mutator: Callable[[dict], None]) -> dict:
    """Atomic read-modify-write; returns the new config.

    ``mutator`` edits the passed dict in place; if it raises, nothing is
    written. Raises ``ConfigUnreadable`` (stored config corrupt, no last-good),
    ``TypeError``/``ValueError`` (result not JSON-serialisable; file
    untouched) or ``ConfigWriteError`` (disk failure; file untouched)."""
    global _cache
    with _lock:
        base, source, err = _stored()
        if base is None:
            if source == "unreadable":
                raise ConfigUnreadable(
                    f"stored config is unreadable ({err}) and no last-good "
                    "copy exists; refusing to overwrite it")
            base = default_config()           # first boot: nothing to lose
            log.info("config file absent: bootstrapping defaults")
        elif source == "lastgood":
            log.error("config file damaged (%s): rebuilding it from the "
                      "last-good copy", err)
            _preserve_corrupt()
        new = copy.deepcopy(base)
        mutator(new)
        if not isinstance(new, dict):
            raise TypeError("config root must be a JSON object")
        json.dumps(new, ensure_ascii=False, allow_nan=False)  # fail BEFORE IO
        p = pathlib.Path(CONFIG_PATH)
        if not jsonstore.save(p, new, indent=2):
            raise ConfigWriteError(f"could not write {p}")
        try:
            _cache = (_sig(p), new)
        except OSError:
            _cache = None
        _remember_good(new)
        _state.update(source="file", error=None)
        return copy.deepcopy(new)


def _preserve_corrupt() -> None:
    """Keep the damaged file for forensics before it is replaced."""
    p = pathlib.Path(CONFIG_PATH)
    try:
        if p.exists():
            p.replace(p.with_name(f"{p.name}.corrupt-{int(time.time())}"))
    except OSError as e:
        log.warning("could not preserve damaged config: %s", e)


def ensure() -> None:
    """Create the config on first boot (restoring the last-good copy if the
    file vanished). An existing file - readable or not - is never touched."""
    with _lock:
        p = pathlib.Path(CONFIG_PATH)
        if p.exists():
            return
        base = _load_lastgood() or default_config()
        if jsonstore.save(p, base, indent=2):
            log.info("config file created at %s", p)
        else:
            log.warning("config file %s could not be created", p)
