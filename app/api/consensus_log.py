"""Analyst-consensus snapshot log (docs/research/data-sources.md section 3.7 and 4 item 4).

Yahoo keeps no history of analyst estimates, so a daily snapshot is the only way to get a point-in-time series. One
row per equity of the registry (``paper_universe``) and day, appended to ``data/consensus/<YYYY-MM>.jsonl`` (UTC month
of ``fetchedAt``): ``{id, targetMean, targetHigh, targetLow, recKey, recMean, nAnalysts, fetchedAt}``. A name with no
analyst field at all (ETFs, uncovered names) gets no row. Nothing in a forecast packet reads this log yet: the
revision features wait for the week-12 readout (at least 60 sessions of history first), and DataQuality must NOT add
consensus to the packets.

Readers (point in time, ``fetchedAt <= as_of``):

* :func:`pit` ``(id, as_of_iso)``: the newest stored row, or None.
* :func:`revisions` ``(id, as_of_iso)``: ``{targetMeanChg7dPct, targetMeanChg30dPct, recMeanChg30d}``. The newest row
  is compared with the newest row at or before ``<its own time> - N days`` (N = 7 or 30; that reference must be no
  older than N + 4 / N + 10 days, else the change is not an N-day change and is None). A field is None when either
  row has fewer than ``MIN_ANALYSTS`` analysts (a thin mid-cap consensus is noise), a missing value, or a
  non-positive target. The whole result is None when there is no row, and its fields are None when the newest row is
  older than ``MAX_STALE_S`` before the as-of.
"""
import logging
import math
import os
import pathlib

from app.api import prices, textsafe, universe_news
from app.api.regime_feed import FeedError, as_of_epoch, cached_rows, iso_ceil, iso_epoch, mono, pace

log = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
CONSENSUS_DIR = DATA_DIR / "consensus"
YF_KEY = "consensus_log"               # prices._yf backoff key: analyst fetches never block price fetches
MIN_ANALYSTS = 3
MAX_STALE_S = 5 * 86400
HISTORY_S = 60 * 86400
WINDOW_DAYS = (7, 30)
SLACK_DAYS = {7: 4, 30: 10}
FAILS_STOP = 3                         # consecutive failed names that end a pass (a limited Yahoo answers all of them)


def _pos(x) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) and v > 0 else None


def snapshot_row(pid: str, info, fetched: float) -> dict | None:
    """The store row of one Yahoo ``info`` dict, or None when it carries no analyst field at all."""
    if not isinstance(info, dict):
        return None
    n = info.get("numberOfAnalystOpinions")
    n = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) and n >= 0 else None
    rec_key = textsafe.clean_text(info.get("recommendationKey"), 24).lower() or None
    if rec_key in ("none", "null"):
        rec_key = None
    rec_mean = _pos(info.get("recommendationMean"))
    row = {"id": pid, "targetMean": _pos(info.get("targetMeanPrice")), "targetHigh": _pos(info.get("targetHighPrice")),
           "targetLow": _pos(info.get("targetLowPrice")), "recKey": rec_key,
           "recMean": rec_mean if rec_mean is not None and rec_mean <= 5 else None,
           "nAnalysts": n, "fetchedAt": iso_ceil(fetched)}
    if all(row[k] is None for k in ("targetMean", "targetHigh", "targetLow", "recKey", "recMean", "nAnalysts")):
        return None
    return row


async def _fetch_info(row: dict) -> dict:
    await pace("consensus")
    info = await prices._yf(YF_KEY, prices._yf_info_blocking, row["yahoo"])
    if info is None:
        raise FeedError("http", "yfinance info unavailable (error or rate limit)")
    return info


async def run_snapshot(rows: list[dict], now: float, deadline: float) -> dict:
    """Snapshot ``rows`` (registry rows) until ``deadline`` (``regime_feed.mono()``). Never raises:
    ``{"new": rows written, "done": [ids handled], "failed": {id: why}}``. An id is done when its row was written or
    Yahoo had no analyst data for it; failed ids are retried by the caller. Three failed names in a row end the pass."""
    new, done, failed, streak = 0, [], {}, 0
    for row in rows:
        if mono() > deadline or streak >= FAILS_STOP:
            break
        pid = row["id"]
        try:
            info = await _fetch_info(row)
        except FeedError as e:
            failed[pid] = str(e)
            streak += 1
            if e.kind == "rate_limited":
                break
            continue
        except Exception as e:
            failed[pid] = f"error: {type(e).__name__}"
            streak += 1
            log.exception("consensus_log %s failed", pid)
            continue
        streak = 0
        snap = snapshot_row(pid, info, now)
        if snap is None:
            done.append(pid)
            continue
        if universe_news.append_jsonl(universe_news.month_path(math.ceil(now), CONSENSUS_DIR), [snap]):
            new += 1
            done.append(pid)
        else:
            failed[pid] = "write: store not writable"
    return {"new": new, "done": done, "failed": failed}


# ------------------------------------------------------------------ point-in-time readers

def _index(rows: list[dict]) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for r in rows:
        t = iso_epoch(r.get("fetchedAt"))
        if isinstance(r.get("id"), str) and t is not None:
            out.setdefault(r["id"], []).append((t, r))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def _history(pid: str, cutoff: float) -> list[tuple]:
    """``[(epoch, row)]`` oldest first with ``fetchedAt <= cutoff`` over the last HISTORY_S."""
    out = []
    for path in universe_news.month_paths(cutoff - HISTORY_S, cutoff, CONSENSUS_DIR):
        out.extend(e for e in cached_rows(path, _index).get(pid, []) if e[0] <= cutoff)
    out.sort(key=lambda x: x[0])
    return out


def pit(pid: str, as_of_iso: str) -> dict | None:
    """The newest snapshot of ``pid`` fetched at or before the as-of, or None."""
    hist = _history(pid, as_of_epoch(as_of_iso))
    return dict(hist[-1][1]) if hist else None


def _reference(hist: list[tuple], t_last: float, days: int):
    target = t_last - days * 86400
    cands = [e for e in hist[:-1] if e[0] <= target and target - e[0] <= SLACK_DAYS[days] * 86400]
    return cands[-1][1] if cands else None


def _thin(*rows) -> bool:
    return any(not isinstance(r.get("nAnalysts"), int) or r["nAnalysts"] < MIN_ANALYSTS for r in rows)


def _pct(last: dict, ref: dict | None) -> float | None:
    if ref is None or _thin(last, ref):
        return None
    a, b = _pos(last.get("targetMean")), _pos(ref.get("targetMean"))
    return None if a is None or b is None else round((a / b - 1.0) * 100.0, 3)


def _delta(last: dict, ref: dict | None) -> float | None:
    if ref is None or _thin(last, ref):
        return None
    a, b = _pos(last.get("recMean")), _pos(ref.get("recMean"))
    return None if a is None or b is None else round(a - b, 3)


def revisions(pid: str, as_of_iso: str) -> dict | None:
    """Consensus revisions of ``pid`` at the as-of; see the module docstring for the exact rule."""
    cutoff = as_of_epoch(as_of_iso)
    hist = _history(pid, cutoff)
    out: dict = {"targetMeanChg7dPct": None, "targetMeanChg30dPct": None, "recMeanChg30d": None}
    if not hist:
        return None
    t_last, last = hist[-1]
    if cutoff - t_last > MAX_STALE_S:
        return out
    ref7, ref30 = _reference(hist, t_last, 7), _reference(hist, t_last, 30)
    out["targetMeanChg7dPct"] = _pct(last, ref7)
    out["targetMeanChg30dPct"] = _pct(last, ref30)
    out["recMeanChg30d"] = _delta(last, ref30)
    return out
