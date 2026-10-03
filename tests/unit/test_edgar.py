"""edgar: a filing is marked seen only after its alert was accepted; a lost or
corrupt seen file reseeds silently instead of flooding alerts."""
import asyncio
import json
import time

import pytest

from app.api import edgar, notify

TODAY = time.strftime("%Y-%m-%d", time.gmtime())
ACC = "0001045810-26-000001"

FORM4 = """<ownershipDocument>
<issuer><issuerName>Xyz Corp</issuerName><issuerTradingSymbol>XYZ</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><transactionDate><value>{d}</value></transactionDate>
<transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>10000</value></transactionShares>
<transactionPricePerShare><value>150</value></transactionPricePerShare></transactionAmounts>
</nonDerivativeTransaction>
<nonDerivativeTransaction><transactionDate><value>{d}</value></transactionDate>
<transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>20000</value></transactionShares>
<transactionPricePerShare><value>151</value></transactionPricePerShare></transactionAmounts>
</nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>""".format(d=TODAY)


class Resp:
    is_success = True
    status_code = 200
    text = FORM4


@pytest.fixture
def sec(tmp_path, monkeypatch):
    """One US issuer 'XYZ' with one fresh Form 4 holding two notable trades."""
    monkeypatch.setattr(edgar, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(edgar, "RECENT_FILE", tmp_path / "recent.json")
    monkeypatch.setattr(edgar, "_watch_symbols", lambda: ["XYZ"])

    async def ciks(symbols):
        return {"XYZ": 1234}

    async def submissions(cik, sym):
        if state["fetch_ok"]:
            return {"filings": {"recent": {
                "form": ["4"], "filingDate": [TODAY],
                "accessionNumber": [ACC], "items": [""],
                "primaryDocument": ["xslF345X05/form4.xml"]}}}
        return None

    async def sec_get(url):
        return Resp()

    calls = []
    outcomes = []

    async def alert(ticker, source, title, body, **kw):
        calls.append((ticker, title, body))
        return outcomes.pop(0) if outcomes else notify.Delivery(notify.SENT)

    state = {"fetch_ok": True}
    monkeypatch.setattr(edgar, "ticker_ciks", ciks)
    monkeypatch.setattr(edgar, "_submissions", submissions)
    monkeypatch.setattr(edgar, "_sec_get", sec_get)
    monkeypatch.setattr(edgar.notify, "alert", alert)
    return type("Sec", (), {"calls": calls, "outcomes": outcomes,
                            "state": state})


def seen_file():
    return json.loads(edgar.SEEN_FILE.read_text())


def test_failed_push_leaves_filing_unseen_then_retries(sec):
    edgar.SEEN_FILE.write_text("{}")
    sec.outcomes.append(notify.Delivery(notify.FAILED))
    asyncio.run(edgar.run_once())
    assert len(sec.calls) == 1 and seen_file() == {}

    asyncio.run(edgar.run_once())          # retried, now accepted
    assert len(sec.calls) == 2
    assert list(seen_file()) == [f"XYZ:{ACC}"]

    asyncio.run(edgar.run_once())          # seen: silent
    assert len(sec.calls) == 2


def test_one_push_per_filing_groups_its_trades(sec):
    edgar.SEEN_FILE.write_text("{}")
    asyncio.run(edgar.run_once())
    assert len(sec.calls) == 1
    _, title, body = sec.calls[0]
    assert title == "XYZ insider open-market sale"
    assert body.count("open-market sale") == 2
    assert "10,000 sh" in body and "20,000 sh" in body


def test_filtered_alert_still_consumes_the_filing(sec):
    edgar.SEEN_FILE.write_text("{}")
    sec.outcomes.append(notify.Delivery(notify.FILTERED))
    asyncio.run(edgar.run_once())
    assert list(seen_file()) == [f"XYZ:{ACC}"]


def test_corrupt_seen_file_reseeds_silently(sec):
    edgar.SEEN_FILE.write_text("{not json")
    asyncio.run(edgar.run_once())
    assert sec.calls == []                       # no flood of old filings
    assert list(seen_file()) == [f"XYZ:{ACC}"]   # baseline re-established
    asyncio.run(edgar.run_once())
    assert sec.calls == []


def test_missing_seen_file_with_total_fetch_outage_stays_unknown(sec):
    sec.state["fetch_ok"] = False
    asyncio.run(edgar.run_once())
    assert not edgar.SEEN_FILE.exists()          # nothing written from nothing
    sec.state["fetch_ok"] = True
    asyncio.run(edgar.run_once())                # first successful pass seeds
    assert sec.calls == [] and list(seen_file()) == [f"XYZ:{ACC}"]


def test_legacy_list_seen_file_is_still_understood(sec):
    edgar.SEEN_FILE.write_text(json.dumps([f"XYZ:{ACC}"]))
    asyncio.run(edgar.run_once())
    assert sec.calls == []
