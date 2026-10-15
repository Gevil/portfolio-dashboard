"""Exchange calendars wrapper: sessions, holidays, early closes, quote session.

No network, no sleeps; the clock is always an explicit ``now`` argument.
"""
import datetime
import logging
from zoneinfo import ZoneInfo

import pytest

from app.api import calendars, registry

D = datetime.date

ENTRIES = [
    {"id": "ASML", "kind": "equity", "role": "holding", "quoteCurrency": "EUR",
     "providers": {"yahoo": "ASML.AS"},
     "listing": {"symbol": "ASML.AS", "venue": "XAMS", "currency": "EUR"}},
    {"id": "NVDA", "kind": "equity", "role": "holding", "quoteCurrency": "EUR",
     "providers": {"yahoo": "NVD.DE"},
     "listing": {"symbol": "NVD.DE", "venue": "XETR", "currency": "EUR"}},
    {"id": "GSPC", "kind": "index", "role": "benchmark", "quoteCurrency": "USD",
     "providers": {"yahoo": "^GSPC"},
     "listing": {"symbol": "^GSPC", "venue": "XNYS", "currency": "USD"}},
    {"id": "NOLIST", "kind": "index", "role": "benchmark", "providers": {}},
]


def epoch(y, m, d, h, mi, tz):
    return datetime.datetime(y, m, d, h, mi, tzinfo=ZoneInfo(tz)).timestamp()


@pytest.fixture
def wl(monkeypatch):
    monkeypatch.setattr(registry, "_raw_watchlist", lambda: ENTRIES)
    monkeypatch.setattr(registry, "_memo_raw", None)
    monkeypatch.setattr(registry, "_memo_entries", [])


def test_labour_day_2026_closes_euronext_and_xetra_but_not_nyse():
    day = D(2026, 5, 1)
    assert not calendars.is_session("XAMS", day)
    assert not calendars.is_session("XETR", day)
    assert calendars.is_session("XNYS", day)


def test_christmas_eve_2026_xetra_closed_amsterdam_early_close():
    day = D(2026, 12, 24)
    assert not calendars.is_session("XETR", day)
    assert calendars.is_session("XAMS", day)
    opens, closes = calendars.session_bounds("XAMS", day)
    assert (opens.hour, opens.minute) == (9, 0)
    assert (closes.hour, closes.minute) < (17, 30)            # early close
    assert closes.utcoffset() == datetime.timedelta(hours=1)  # tz-aware, venue tz (CET)
    assert calendars.session_bounds("XETR", day) is None


def test_regular_day_bounds_are_venue_local_and_dst_aware():
    opens, closes = calendars.session_bounds("XNYS", D(2026, 6, 1))
    assert (opens.hour, opens.minute, closes.hour, closes.minute) == (9, 30, 16, 0)
    assert opens.utcoffset() == datetime.timedelta(hours=-4)   # EDT


def test_previous_session_across_weekend_and_holiday():
    # Monday after the Friday 1 May holiday: skips the weekend AND the holiday
    assert calendars.previous_session("XAMS", D(2026, 5, 4)) == D(2026, 4, 30)
    # plain weekend
    assert calendars.previous_session("XNYS", D(2026, 6, 1)) == D(2026, 5, 29)
    # NYSE observes Independence Day on Friday 3 July 2026 (4 July is a Saturday)
    assert calendars.previous_session("XNYS", D(2026, 7, 6)) == D(2026, 7, 2)
    # strictly before: a session day maps to the session before it
    assert calendars.previous_session("XAMS", D(2026, 4, 30)) == D(2026, 4, 29)
    # a non-session day maps to the last session before it
    assert calendars.previous_session("XAMS", D(2026, 5, 1)) == D(2026, 4, 30)


def test_next_session_and_session_on_or_before():
    assert calendars.next_session("XETR", D(2026, 4, 30)) == D(2026, 5, 4)
    assert calendars.next_session("XNYS", D(2026, 4, 30)) == D(2026, 5, 1)
    assert calendars.session_on_or_before("XAMS", D(2026, 5, 3)) == D(2026, 4, 30)
    assert calendars.session_on_or_before("XAMS", D(2026, 4, 30)) == D(2026, 4, 30)


def test_add_sessions_counts_sessions_not_days():
    assert calendars.add_sessions("XAMS", D(2026, 4, 29), 1) == D(2026, 4, 30)
    assert calendars.add_sessions("XAMS", D(2026, 4, 29), 2) == D(2026, 5, 4)
    assert calendars.add_sessions("XNYS", D(2026, 4, 29), 2) == D(2026, 5, 1)


def test_sessions_between_is_inclusive_and_closures_lists_weekday_holidays():
    got = calendars.sessions_between("XAMS", D(2026, 4, 29), D(2026, 5, 5))
    assert got == [D(2026, 4, 29), D(2026, 4, 30), D(2026, 5, 4), D(2026, 5, 5)]
    assert calendars.sessions_between("XAMS", D(2026, 5, 5), D(2026, 4, 29)) == []
    assert calendars.closures("XAMS", D(2026, 4, 28), D(2026, 5, 5)) == [D(2026, 5, 1)]
    assert calendars.closures("XNYS", D(2026, 4, 28), D(2026, 5, 5)) == []


def test_quote_session_before_open_during_session_and_on_a_holiday():
    tz = "Europe/Amsterdam"
    # Monday 2026-05-04: before 09:00 the quote still belongs to Thursday 30 Apr
    assert calendars.quote_session("XAMS", epoch(2026, 5, 4, 8, 59, tz)) == D(2026, 4, 30)
    # at the open and during the session it is today
    assert calendars.quote_session("XAMS", epoch(2026, 5, 4, 9, 0, tz)) == D(2026, 5, 4)
    assert calendars.quote_session("XAMS", epoch(2026, 5, 4, 12, 0, tz)) == D(2026, 5, 4)
    # after the close it stays today's session
    assert calendars.quote_session("XAMS", epoch(2026, 5, 4, 20, 0, tz)) == D(2026, 5, 4)
    # on the 1 May holiday and on the weekend: the last session
    assert calendars.quote_session("XAMS", epoch(2026, 5, 1, 12, 0, tz)) == D(2026, 4, 30)
    assert calendars.quote_session("XAMS", epoch(2026, 5, 3, 12, 0, tz)) == D(2026, 4, 30)


def test_quote_session_uses_the_venue_clock_not_the_host_clock():
    # 14:00 UTC on 1 May is 10:00 in New York (open, NYSE trades) but a holiday in Amsterdam
    now = epoch(2026, 5, 1, 14, 0, "UTC")
    assert calendars.quote_session("XNYS", now) == D(2026, 5, 1)
    assert calendars.quote_session("XAMS", now) == D(2026, 4, 30)


def test_mic_for_follows_the_listing_venue(wl):
    assert calendars.mic_for("ASML") == "XAMS"
    assert calendars.mic_for("NVDA") == "XETR"          # .DE listing -> Xetra
    assert calendars.mic_for("GSPC") == "XNYS"
    assert calendars.mic_for("NOLIST") == "XNYS"        # no listing
    assert calendars.mic_for("NOPE") == "XNYS"          # not on the watchlist


def test_unknown_mic_is_treated_as_xnys_with_a_warning(caplog):
    calendars._warned.discard("unknown:ZZZZ")
    with caplog.at_level(logging.WARNING, logger=calendars.log.name):
        assert calendars.is_session("ZZZZ", D(2026, 5, 1))          # NYSE is open
        assert not calendars.is_session("ZZZZ", D(2026, 7, 3))      # NYSE holiday
    assert sum("unknown MIC" in r.getMessage() for r in caplog.records) == 1


def test_dates_outside_the_calendar_fall_back_to_weekdays_with_one_warning(caplog):
    calendars._warned.discard("bounds:XNYS")
    with caplog.at_level(logging.WARNING, logger=calendars.log.name):
        assert calendars.is_session("XNYS", D(1980, 1, 2))          # Wednesday
        assert not calendars.is_session("XNYS", D(1980, 1, 5))      # Saturday
        assert calendars.previous_session("XNYS", D(1980, 1, 7)) == D(1980, 1, 4)
        assert calendars.next_session("XNYS", D(1980, 1, 4)) == D(1980, 1, 7)
        assert calendars.sessions_between("XNYS", D(1980, 1, 4), D(1980, 1, 8)) == [
            D(1980, 1, 4), D(1980, 1, 7), D(1980, 1, 8)]
        opens, _ = calendars.session_bounds("XNYS", D(1980, 1, 2))
        assert (opens.hour, opens.minute) == (9, 30)
    assert sum("falling back" in r.getMessage() for r in caplog.records) == 1
