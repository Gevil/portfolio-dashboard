"""fundamentals: quarterly (10-Q) facts and TTM for US filers are built from the
XBRL facts exactly (FY + YTD - prior YTD, Q4 = FY - 9M), or unavailable - never
approximated; a foreign private issuer's quarterly/TTM is NOT_APPLICABLE, not a
gap. Synthetic companyfacts bodies only: no network."""
import pytest

from app.api import fundamentals as fd


@pytest.fixture(autouse=True)
def default_cfg(monkeypatch):
    monkeypatch.setattr(fd, "cfg", lambda: dict(fd.DEFAULTS))


def fact(start, end, val, form, filed):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed}


def concept(*entries, unit="USD"):
    return {"units": {unit: list(entries)}}


Q = "10-Q"
K = "10-K"


def revenue_facts():
    """A calendar-year company: FY2025 on the 10-K, the 2025 quarters/YTD as
    comparatives, 2026 Q1/Q2/6M on 10-Qs. FY25 500; 2025 Q1..Q3 = 100/110/135
    (6M 210, 9M 345 -> derived Q4 155); 2026 Q1 150, Q2 160, 6M 310, so
    TTM = 500 + 310 - 210 = 600."""
    return [
        fact("2025-01-01", "2025-12-31", 500, K, "2026-02-10"),
        fact("2025-01-01", "2025-03-31", 100, Q, "2026-05-01"),
        fact("2025-04-01", "2025-06-30", 110, Q, "2026-08-01"),
        fact("2025-01-01", "2025-06-30", 210, Q, "2026-08-01"),
        fact("2025-07-01", "2025-09-30", 135, Q, "2025-11-01"),
        fact("2025-01-01", "2025-09-30", 345, Q, "2025-11-01"),
        fact("2026-01-01", "2026-03-31", 150, Q, "2026-05-01"),
        fact("2026-04-01", "2026-06-30", 160, Q, "2026-08-01"),
        fact("2026-01-01", "2026-06-30", 310, Q, "2026-08-01"),
    ]


def us_filer():
    revenue = revenue_facts()
    ni = [fact("2025-01-01", "2025-12-31", 100, K, "2026-02-10"),
          fact("2026-01-01", "2026-06-30", 60, Q, "2026-08-01"),
          fact("2025-01-01", "2025-06-30", 40, Q, "2026-08-01")]
    ocf = [fact("2025-01-01", "2025-12-31", 80, K, "2026-02-10"),
           fact("2026-01-01", "2026-06-30", 50, Q, "2026-08-01"),
           fact("2025-01-01", "2025-06-30", 30, Q, "2026-08-01")]
    capex = [fact("2025-01-01", "2025-12-31", 20, K, "2026-02-10"),
             fact("2026-01-01", "2026-06-30", 12, Q, "2026-08-01"),
             fact("2025-01-01", "2025-06-30", 8, Q, "2026-08-01")]
    assets = [{"end": "2026-06-30", "val": 1000, "form": Q, "filed": "2026-08-01"}]
    return {"Revenues": concept(*revenue),
            "NetIncomeLoss": concept(*ni),
            "NetCashProvidedByUsedInOperatingActivities": concept(*ocf),
            "PaymentsToAcquirePropertyPlantAndEquipment": concept(*capex),
            "Assets": concept(*assets)}


def test_ttm_is_fiscal_year_plus_ytd_minus_prior_ytd():
    out = fd.extract(us_filer(), 8)
    assert out["filer"] == "domestic"
    ttm = out["ttm"]
    assert ttm["periodEnd"] == "2026-06-30"
    assert ttm["revenue"] == 600 and ttm["ni"] == 120
    assert ttm["ocf"] == 100 and ttm["capex"] == 24 and ttm["fcf"] == 76


def test_quarterly_rows_use_the_ten_q_quarter_and_derive_q4():
    rows = fd.extract(us_filer(), 8)["quarterly"]
    assert [r["end"] for r in rows] == ["2025-03-31", "2025-06-30", "2025-09-30",
                                        "2025-12-31", "2026-03-31", "2026-06-30"]
    assert [r["revenue"] for r in rows] == [100, 110, 135, 155, 150, 160]
    q4 = rows[3]
    assert q4["derived"] is True and q4["revenue"] == 155      # 500 - 345
    assert all(r["derived"] is False for r in rows if r is not q4)
    # a balance-sheet value is attached only where a 10-Q reported near that date
    assert rows[-1]["assets"] == 1000 and rows[0]["assets"] is None


def test_ttm_without_the_prior_year_twin_is_unavailable_not_approximated():
    gaap = us_filer()
    for name in ("Revenues", "NetIncomeLoss",
                 "NetCashProvidedByUsedInOperatingActivities",
                 "PaymentsToAcquirePropertyPlantAndEquipment"):
        entries = gaap[name]["units"]["USD"]
        gaap[name] = concept(*[e for e in entries
                               if not (e["start"] == "2025-01-01" and e["end"] == "2025-06-30")])
    assert fd.extract(gaap, 8)["ttm"] is None


def test_ttm_is_the_fiscal_year_when_no_ten_q_follows_it():
    gaap = us_filer()
    for name in list(gaap):
        gaap[name] = concept(*[e for e in gaap[name]["units"]["USD"]
                               if e.get("form") != Q or e.get("end", "") < "2026"])
    ttm = fd.extract(gaap, 8)["ttm"]
    assert ttm["periodEnd"] == "2025-12-31" and ttm["revenue"] == 500 and ttm["ni"] == 100


def test_foreign_private_issuer_quarterly_and_ttm_are_not_applicable():
    gaap = {"RevenueFromContractWithCustomerExcludingAssessedTax": concept(
                fact("2024-01-01", "2024-12-31", 28000, "20-F", "2025-02-12"),
                fact("2025-01-01", "2025-12-31", 32000, "20-F", "2026-02-11"), unit="EUR"),
            "NetIncomeLoss": concept(
                fact("2025-01-01", "2025-12-31", 9000, "20-F", "2026-02-11"), unit="EUR")}
    out = fd.extract(gaap, 8)
    assert out["filer"] == "fpi" and out["currency"] == "EUR"
    assert out["quarterly"] == fd.NOT_APPLICABLE_FPI == "NOT_APPLICABLE (foreign private issuer)"
    assert out["ttm"] == fd.NOT_APPLICABLE_FPI
    assert [r["fy"] for r in out["annual"]] == [2024, 2025]      # annual rows unaffected


def test_filer_type_follows_the_newest_annual_report():
    moved = {"Revenues": concept(
        fact("2023-01-01", "2023-12-31", 1, "20-F", "2024-02-01"),
        fact("2024-01-01", "2024-12-31", 2, "10-K", "2025-02-01"))}
    assert fd.filer_type(moved) == "domestic"
    assert fd.filer_type({}) is None
    assert fd.filer_type({"Revenues": concept(
        fact("2025-01-01", "2025-03-31", 1, Q, "2025-05-01"))}) is None


def test_payload_without_a_cache_has_the_new_keys_empty():
    body = fd._payload("zzz", None, None, "no SEC CIK")
    assert body["quarterly"] == [] and body["ttm"] is None and body["filer"] is None
