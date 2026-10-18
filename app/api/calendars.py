"""Exchange calendars: sessions, holidays and early closes per venue (MIC).

Thin wrapper over ``exchange_calendars`` (pinned in requirements.txt). One
calendar object is built per MIC and cached. Every function takes a MIC and
plain ``datetime.date`` values and returns plain ``datetime`` objects, so no
pandas type leaks into callers.

Invariants:
* An unknown MIC is treated as XNYS (one warning), a known MIC never raises.
* Dates outside the calendar's bounds fall back to Mon-Fri arithmetic with
  the venue's regular hours (one warning per MIC): a missing holiday table
  must degrade to "weekdays are sessions", never to an exception.
* ``session_bounds`` carries early closes (e.g. Amsterdam 24 Dec).
"""
import datetime
import logging
import time
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

log = logging.getLogger(__name__)

KNOWN_MICS = ("XAMS", "XETR", "XNYS", "XNAS", "XPAR", "XBRU", "XMIL")
DEFAULT_MIC = "XNYS"
_CAL_END_YEARS_AHEAD = 2
_CAL_START_YEARS_BACK = 20
_ONE_DAY = datetime.timedelta(days=1)

# Regular hours / tz for the Mon-Fri fallback (no registry import: registry
# imports this module).
_FALLBACK = {
    "XAMS": ("Europe/Amsterdam", (9, 0), (17, 30)),
    "XPAR": ("Europe/Paris", (9, 0), (17, 30)),
    "XBRU": ("Europe/Brussels", (9, 0), (17, 30)),
    "XETR": ("Europe/Berlin", (9, 0), (17, 30)),
    "XMIL": ("Europe/Rome", (9, 0), (17, 30)),
    "XNAS": ("America/New_York", (9, 30), (16, 0)),
    "XNYS": ("America/New_York", (9, 30), (16, 0)),
}

_cals: dict[str, object] = {}
_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def _norm_mic(mic) -> str:
    key = str(mic or "").strip().upper()
    if key in _FALLBACK:
        return key
    _warn_once("unknown:" + key, "unknown MIC %r, treating as %s", mic, DEFAULT_MIC)
    return DEFAULT_MIC


def _cal(mic: str):
    cal = _cals.get(mic)
    if cal is None:
        year = datetime.date.today().year
        start = pd.Timestamp(year - _CAL_START_YEARS_BACK, 1, 1)
        end = pd.Timestamp(year + _CAL_END_YEARS_AHEAD, 12, 31)
        cal = xcals.get_calendar(mic, start=start, end=end)
        _cals[mic] = cal
    return cal


def _ts(day: datetime.date) -> pd.Timestamp:
    return pd.Timestamp(day.year, day.month, day.day)


def _covered(mic: str, *days: datetime.date) -> bool:
    """True when the calendar covers every day; otherwise warns once."""
    cal = _cal(mic)
    first, last = cal.first_session.date(), cal.last_session.date()
    if all(first < d < last for d in days):
        return True
    _warn_once("bounds:" + mic,
               "%s calendar covers %s..%s, falling back to Mon-Fri outside it",
               mic, first, last)
    return False


def _weekday_on_or_before(day: datetime.date) -> datetime.date:
    while day.weekday() >= 5:
        day -= _ONE_DAY
    return day


def _weekday_on_or_after(day: datetime.date) -> datetime.date:
    while day.weekday() >= 5:
        day += _ONE_DAY
    return day


def is_session(mic: str, day: datetime.date) -> bool:
    mic = _norm_mic(mic)
    if not _covered(mic, day):
        return day.weekday() < 5
    return bool(_cal(mic).is_session(_ts(day)))


def previous_session(mic: str, day: datetime.date) -> datetime.date:
    """Latest session strictly before ``day`` (``day`` need not be a session)."""
    mic = _norm_mic(mic)
    if not _covered(mic, day):
        return _weekday_on_or_before(day - _ONE_DAY)
    return _cal(mic).date_to_session(_ts(day - _ONE_DAY), direction="previous").date()


def next_session(mic: str, day: datetime.date) -> datetime.date:
    """First session strictly after ``day``."""
    mic = _norm_mic(mic)
    if not _covered(mic, day):
        return _weekday_on_or_after(day + _ONE_DAY)
    return _cal(mic).date_to_session(_ts(day + _ONE_DAY), direction="next").date()


def session_on_or_before(mic: str, day: datetime.date) -> datetime.date:
    mic = _norm_mic(mic)
    if not _covered(mic, day):
        return _weekday_on_or_before(day)
    return _cal(mic).date_to_session(_ts(day), direction="previous").date()


def add_sessions(mic: str, day: datetime.date, n: int) -> datetime.date:
    """The n-th session after ``day`` (n >= 1; smaller n is treated as 1)."""
    out = day
    for _ in range(max(1, int(n))):
        out = next_session(mic, out)
    return out


def sessions_between(mic: str, start: datetime.date,
                     end: datetime.date) -> list[datetime.date]:
    """Sessions in [start, end], inclusive (empty when inverted)."""
    if start > end:
        return []
    mic = _norm_mic(mic)
    if not _covered(mic, start, end):
        out, cur = [], start
        while cur <= end:
            if cur.weekday() < 5:
                out.append(cur)
            cur += _ONE_DAY
        return out
    idx = _cal(mic).sessions_in_range(_ts(start), _ts(end))
    return [d.date() for d in idx]


def session_bounds(mic: str, day: datetime.date):
    """(open, close) tz-aware in the venue's tz, early closes included; None
    when ``day`` is not a session."""
    mic = _norm_mic(mic)
    if not is_session(mic, day):
        return None
    if not _covered(mic, day):
        tz_name, (oh, om), (ch, cm) = _FALLBACK[mic]
        tz = ZoneInfo(tz_name)
        return (datetime.datetime(day.year, day.month, day.day, oh, om, tzinfo=tz),
                datetime.datetime(day.year, day.month, day.day, ch, cm, tzinfo=tz))
    cal = _cal(mic)
    label = _ts(day)
    tz = ZoneInfo(_FALLBACK[mic][0])
    return (cal.session_open(label).to_pydatetime().astimezone(tz),
            cal.session_close(label).to_pydatetime().astimezone(tz))


def quote_session(mic: str, now: float | None = None) -> datetime.date:
    """The session a quote "belongs to" at ``now``: today when today is a
    session and ``now`` is at/after its open, else the last session before
    today (before the open, weekend, holiday)."""
    mic = _norm_mic(mic)
    ts = time.time() if now is None else now
    local = datetime.datetime.fromtimestamp(ts, ZoneInfo(_FALLBACK[mic][0]))
    today = local.date()
    bounds = session_bounds(mic, today)
    if bounds is not None and local >= bounds[0]:
        return today
    return session_on_or_before(mic, today - _ONE_DAY)


def closures(mic: str, start: datetime.date,
             end: datetime.date) -> list[datetime.date]:
    """Weekdays in [start, end] that are not sessions (exchange holidays)."""
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5 and not is_session(mic, cur):
            out.append(cur)
        cur += _ONE_DAY
    return out


def mic_for(ident) -> str:
    """MIC of the watchlist id's listing venue; entries without a listing
    (index benchmarks) and unknown ids -> XNYS."""
    from app.api import registry
    v = registry.venue(ident)
    if not v:
        return DEFAULT_MIC
    return _norm_mic(v["mic"])
