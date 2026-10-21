"""Paper execution: placement state machine, reconciliation, fills, end-of-window
cancel, kill switch / halt, rebalance and seed (contract §3.3-3.7).

A fake broker implements the slice-A Protocol; the clock, calendar, quotes,
config and ``notify.alert`` are injected - nothing touches the network, sleeps
or the real data dir. The risk engine (``paper_risk``) is the real one.

Run inside the service image (see test_prices.py for the command).
"""
import asyncio
import datetime

import pytest

from app.api import broker as broker_mod
from app.api import notify, paper_eval, paper_ledger, paper_risk
from app.api import paper_exec as pex

TZ = datetime.timezone(datetime.timedelta(hours=2))      # CEST on the test day


def at(h, m=0, day=19):
    return datetime.datetime(2026, 10, day, h, m, tzinfo=TZ).timestamp()


NOW = at(11, 0)                      # inside the 09:30-17:00 trading window
TICKERS = {"ASML": "ASMLa_EQ", "NVDA": "NVDd_EQ"}
UNIVERSE = {k: {"t212": v} for k, v in TICKERS.items()}
TODAY = pex._today(NOW)


def run(coro):
    return asyncio.run(coro)


def bounds(day):
    return (datetime.datetime(day.year, day.month, day.day, 9, 0, tzinfo=TZ),
            datetime.datetime(day.year, day.month, day.day, 17, 30, tzinfo=TZ))


def mk_order(id="B1", ticker="ASMLa_EQ", side="buy", qty=2.0, filled=0.0, status="open",
             created=NOW, limit=100.3, avg=None, value=None, raw=None):
    return broker_mod.Order(id=id, ticker=ticker, side=side, quantity=qty, filled_quantity=filled,
                            limit_price=limit, avg_fill_price=avg, filled_value=value,
                            status=status, created_at=created, raw_status=raw)


def position(ticker, qty, avg=100.0, current=None):
    return broker_mod.Position(ticker=ticker, quantity=qty, average_price=avg,
                               current_price=current, ppl=None, currency="EUR")


def intent(pid="ASML", side="buy", qty=2.0, price=100.0):
    slip = 1.003 if side == "buy" else 0.997
    return paper_risk.Intent(id=pid, ticker=TICKERS[pid], side=side, quantity=qty,
                             limit_price=round(price * slip, 2), ref_price=price,
                             ref_price_as_of=int(NOW) - 10, value_eur=qty * price,
                             reason="rebalance 0.0%->20.0%")


def ledger_row(pid="ASML", side="buy", qty=2.0, state="submitted", broker_id="B1", created=NOW,
               filled=0.0, value=None, attempts=1, unknown_passes=0, limit=100.3, ref=100.0,
               intent_id=None):
    return {"intentId": intent_id or f"{TODAY}-s1-manual-{pid}-{side}-9", "book": "practice",
            "strategyVersion": "s1", "source": "manual", "createdAt": created, "id": pid,
            "ticker": TICKERS[pid], "side": side, "quantity": qty, "limitPrice": limit,
            "refPrice": ref, "refPriceAsOf": int(created) - 10, "reason": "t", "state": state,
            "brokerOrderId": broker_id, "filledQuantity": filled, "avgFillPrice": None,
            "filledValueEur": value, "unknownPasses": unknown_passes, "attempts": attempts,
            "message": "", "updatedAt": created}


class FakeBroker:
    name, env = "t212", "demo"

    def __init__(self):
        self.open, self.history, self.positions_list = [], [], []
        self.open_error = self.positions_error = None
        self.place_results = []          # consumed first; empty -> an open Order
        self.cancel_result = True
        self.on_place = None
        self.place_gate = None
        self.placed, self.cancelled, self.calls = [], [], []
        self._seq = 0

    def quantity_decimals(self, ticker):
        return 2

    async def open_orders(self, fresh=False):
        self.calls.append(("open_orders", fresh))
        return self.open_error or list(self.open)

    async def order_history(self, limit=50, fresh=False):
        self.calls.append(("order_history", fresh))
        return list(self.history)

    async def positions(self, fresh=False):
        self.calls.append(("positions", fresh))
        return self.positions_error or list(self.positions_list)

    async def place_limit_order(self, ticker, side, quantity, limit_price):
        self.calls.append(("place", ticker, side, quantity, limit_price))
        self.placed.append((ticker, side, quantity, limit_price))
        if self.on_place:
            self.on_place(ticker, side)
        if self.place_gate is not None:
            await self.place_gate.wait()
        if self.place_results:
            return self.place_results.pop(0)
        self._seq += 1
        return mk_order(id=f"B{self._seq}", ticker=ticker, side=side, qty=quantity,
                        limit=limit_price, status="open")

    async def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        self.cancelled.append(order_id)
        return self.cancel_result


class FakeAlert:
    def __init__(self):
        self.calls, self.statuses = [], []

    async def __call__(self, ticker, source, title, body, **kw):
        self.calls.append({"ticker": ticker, "source": source, "title": title,
                           "body": body, **kw})
        return notify.Delivery(self.statuses.pop(0) if self.statuses else notify.SENT)


class Env:
    def __init__(self):
        self.now = NOW
        self.broker = FakeBroker()
        self.alert = FakeAlert()
        self.cfg = {"mode": "demo", "strategyVersion": "s1", "budgetEur": 1000.0,
                    "universe": dict(UNIVERSE), "limits": {}}
        self.cfg_intraday = {"bucket": "intraday", "enabled": False, "mode": "off",
                             "strategyVersion": "i1", "budgetEur": 1000.0,
                             "universe": dict(UNIVERSE), "limits": {}}
        self.quotes = {"ASML": 100.0, "NVDA": 100.0}
        self.closes = {}
        self.session = True
        self.errs = []


@pytest.fixture
def env(monkeypatch, tmp_path):
    e = Env()
    for name in ("SEASON_FILE", "SNAPSHOTS_FILE", "ORDERS_FILE", "HALT_FILE"):
        monkeypatch.setattr(paper_ledger, name, tmp_path / f"{name.lower()}.json")

    async def quote_universe(universe):
        return {pid: ({"price": e.quotes[pid], "prevClose": e.quotes[pid],
                       "asOf": int(e.now) - 10, "stale": False}
                      if pid in e.quotes else None) for pid in universe}

    async def daily_closes(pid, period):
        return list(e.closes.get(pid, []))

    monkeypatch.setattr(pex, "_now", lambda: e.now)
    monkeypatch.setattr(pex, "_broker", lambda: e.broker)
    monkeypatch.setattr(pex.paper_eval, "_paper_cfg",
                        lambda bucket="daily": e.cfg_intraday if bucket == "intraday" else e.cfg)
    monkeypatch.setattr(pex.paper_eval, "enabled", lambda: True)
    monkeypatch.setattr(pex.paper_eval, "_quote_universe", quote_universe)
    monkeypatch.setattr(pex.paper_eval, "decimals_by_id", lambda u: {k: 2 for k in u})
    monkeypatch.setattr(pex.registry, "get", lambda pid: {"id": pid, "kind": "equity"})
    monkeypatch.setattr(pex.calendars, "mic_for", lambda ident: "XETR")
    monkeypatch.setattr(pex.calendars, "session_bounds",
                        lambda mic, day: bounds(day) if e.session else None)
    monkeypatch.setattr(pex.prices, "daily_closes", daily_closes)
    monkeypatch.setattr(pex.notify, "alert", e.alert)
    pex.reset_state()
    return e


def place(env, *intents, source="manual"):
    """Place intents the way a locked caller does (``rebalance`` minus planning)."""
    async def go():
        async with pex._get_lock():
            return await pex._place_locked(list(intents), source=source, cfg=env.cfg,
                                           errs=env.errs)
    return run(go())


def orders():
    return paper_ledger.read_orders()


def only_row():
    rows = orders()
    assert len(rows) == 1
    return rows[0]


# ------------------------------------------------------------------- placement

def test_intent_is_written_pending_before_send_with_the_attempt_persisted(env):
    seen = []

    def on_place(ticker, side):       # runs inside place_limit_order: what is on disk NOW?
        seen.append([(r["ticker"], r["side"], r["state"], r["attempts"], r["brokerOrderId"])
                     for r in orders()])
    env.broker.on_place = on_place
    place(env, intent("ASML", "buy", 2.0))
    assert seen == [[("ASMLa_EQ", "buy", "pending", 1, None)]]
    row = only_row()
    assert row["state"] == "submitted" and row["brokerOrderId"] == "B1"
    assert row["intentId"] == f"{TODAY}-daily-s1-manual-ASML-buy-1"
    assert row["limitPrice"] == 100.3 and row["quantity"] == 2.0 and row["source"] == "manual"


def test_the_whole_batch_is_written_pending_before_the_first_send(env):
    seen = []
    env.broker.on_place = lambda t, s: seen.append(sorted((r["id"], r["state"]) for r in orders()))
    place(env, intent("NVDA", "sell", 1.0), intent("ASML", "buy", 2.0))
    assert seen[0] == [("ASML", "pending"), ("NVDA", "pending")]


@pytest.mark.parametrize("where", ["open", "history"])
def test_a_matching_broker_order_is_attached_and_nothing_is_sent(env, where):
    match = mk_order("X9", qty=2.0, created=NOW, status="open" if where == "open" else "filled",
                     filled=0.0 if where == "open" else 2.0, avg=100.0, value=200.0)
    getattr(env.broker, where).append(match)
    place(env, intent("ASML", "buy", 2.0))
    assert env.broker.placed == []
    row = only_row()
    assert row["brokerOrderId"] == "X9" and row["attempts"] == 0
    assert row["state"] == ("submitted" if where == "open" else "filled")
    assert "attached" in row["message"]
    if where == "history":
        assert row["filledQuantity"] == 2.0 and row["filledValueEur"] == 200.0


@pytest.mark.parametrize("other", [
    {"created": NOW - 60},                     # older than intent.createdAt - 5 s
    {"qty": 2.5},                              # quantity differs by more than the step
    {"side": "sell"},
    {"ticker": "NVDd_EQ"},
])
def test_an_order_that_is_not_this_intents_does_not_match(env, other):
    env.broker.open.append(mk_order("X9", **{"qty": 2.0, "created": NOW, **other}))
    place(env, intent("ASML", "buy", 2.0))
    assert len(env.broker.placed) == 1 and only_row()["brokerOrderId"] == "B1"


def test_an_order_within_the_time_slack_and_the_quantity_step_matches(env):
    env.broker.open.append(mk_order("X9", qty=2.005, created=NOW - 4))
    place(env, intent("ASML", "buy", 2.0))
    assert env.broker.placed == [] and only_row()["brokerOrderId"] == "X9"


def test_an_order_already_attached_to_another_intent_is_never_reused(env):
    env.broker.open.append(mk_order("X9", qty=2.0, created=NOW))
    place(env, intent("ASML", "buy", 2.0))          # attaches X9
    place(env, intent("ASML", "buy", 2.0))          # an identical second intent
    assert len(env.broker.placed) == 1
    first, second = orders()
    assert first["brokerOrderId"] == "X9" and second["brokerOrderId"] == "B1"
    assert second["intentId"].endswith("-buy-2")


@pytest.mark.parametrize("kind", ["timeout", "http", "rate_limited", "bad_response"])
def test_an_ambiguous_send_failure_is_unknown_and_not_resent_now(env, kind):
    env.broker.place_results = [broker_mod.err(kind)]
    place(env, intent())
    row = only_row()
    assert row["state"] == "unknown" and row["attempts"] == 1 and row["brokerOrderId"] is None
    assert kind in row["message"]
    assert len(env.broker.placed) == 1 and env.errs == [kind]


def test_two_unknown_passes_then_not_sent_and_exactly_one_resend(env):
    env.broker.place_results = [broker_mod.err("timeout")]
    place(env, intent())
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "unknown" and row["unknownPasses"] == 1
    assert len(env.broker.placed) == 1                      # still no resend after one pass
    run(pex.run_pass())                                     # 2nd pass without a match
    row = only_row()
    assert len(env.broker.placed) == 2                      # the ONE resend
    assert row["state"] == "submitted" and row["attempts"] == 2 and row["brokerOrderId"] == "B1"
    assert "resent once" in row["message"]
    run(pex.run_pass())
    run(pex.run_pass())
    assert len(env.broker.placed) == 2                      # never a third send


def test_a_resend_that_also_goes_unknown_ends_not_sent_without_a_third_send(env):
    env.broker.place_results = [broker_mod.err("timeout"), broker_mod.err("http")]
    place(env, intent())
    run(pex.run_pass())
    run(pex.run_pass())                                     # resend -> unknown again
    row = only_row()
    assert row["state"] == "unknown" and row["attempts"] == 2 and row["unknownPasses"] == 0
    run(pex.run_pass())
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "not_sent" and row["attempts"] == 2
    assert len(env.broker.placed) == 2


def test_an_unknown_send_that_turns_up_at_the_broker_is_attached_never_resent(env):
    env.broker.place_results = [broker_mod.err("timeout")]
    place(env, intent())
    env.broker.open.append(mk_order("B5", qty=2.0, created=NOW + 1))     # it did reach the broker
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "submitted" and row["brokerOrderId"] == "B5" and row["attempts"] == 1
    assert len(env.broker.placed) == 1


@pytest.mark.parametrize("blocker", ["window_closed", "halted", "mode_shadow"])
def test_no_resend_outside_the_window_while_halted_or_when_not_in_demo(env, blocker):
    env.broker.place_results = [broker_mod.err("timeout")]
    place(env, intent())
    run(pex.run_pass())
    if blocker == "window_closed":
        env.now = at(17, 10)
    elif blocker == "halted":
        paper_ledger.write_halt({"halted": True, "reason": "x", "at": 1.0, "by": "owner",
                                 "resumedAt": None, "resumedBy": None, "notifiedAt": 1.0})
    else:
        env.cfg["mode"] = "shadow"
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "not_sent" and "not resent" in row["message"]
    assert len(env.broker.placed) == 1


@pytest.mark.parametrize("kind", ["rejected", "bad_request"])
def test_a_rejected_order_is_terminal_with_the_broker_message_and_never_retried(env, kind):
    env.broker.place_results = [broker_mod.err(kind, 400, "insufficient funds")]
    place(env, intent())
    row = only_row()
    assert row["state"] == "rejected" and "insufficient funds" in row["message"]
    for _ in range(3):
        run(pex.run_pass())
    assert only_row()["state"] == "rejected" and len(env.broker.placed) == 1


def test_a_send_refused_before_placement_is_not_sent_and_reported_as_an_error(env):
    env.broker.place_results = [broker_mod.err("auth", 401, "bad key")]
    place(env, intent())
    row = only_row()
    assert row["state"] == "not_sent" and "auth" in row["message"]
    assert env.errs == ["auth"]


def test_when_existing_orders_cannot_be_listed_nothing_is_sent(env):
    env.broker.open_error = broker_mod.err("timeout")
    place(env, intent())
    row = only_row()
    assert row["state"] == "not_sent" and "could not verify" in row["message"]
    assert env.broker.placed == []


def test_shadow_mode_writes_shadow_rows_and_never_calls_the_broker(env):
    env.cfg["mode"] = "shadow"
    rows = place(env, intent("NVDA", "sell", 1.0), intent("ASML", "buy", 2.0))
    assert [r["state"] for r in rows] == ["shadow", "shadow"]
    assert env.broker.calls == []
    assert paper_ledger.ledger_cash(orders(), 1000.0)["cashEur"] == 1000.0   # shadow never moves cash


@pytest.mark.parametrize("mode", ["off", "live-auto", ""])
def test_mode_off_refuses_with_a_conflict_and_writes_nothing(env, mode):
    env.cfg["mode"] = mode
    with pytest.raises(paper_eval.PaperConflict, match="mode"):
        place(env, intent())
    assert orders() == [] and env.broker.calls == []


def test_a_halted_bucket_refuses_with_a_conflict_and_writes_nothing(env):
    paper_ledger.write_halt({"halted": True, "reason": "drawdown", "at": 1.0, "by": "kill_switch",
                             "resumedAt": None, "resumedBy": None})
    with pytest.raises(paper_eval.PaperConflict, match="halted.*drawdown"):
        place(env, intent())
    assert orders() == [] and env.broker.calls == []


# ------------------------------------------------------------- crash recovery

def test_a_pending_row_that_was_never_sent_is_not_sent_after_a_restart(env):
    paper_ledger.save_orders([ledger_row(state="pending", attempts=0, broker_id=None)])
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "not_sent" and "never sent" in row["message"]
    assert env.broker.placed == []


def test_a_pending_row_that_may_have_been_sent_is_reconciled_as_unknown(env):
    paper_ledger.save_orders([ledger_row(state="pending", attempts=1, broker_id=None)])
    env.broker.open.append(mk_order("B5", qty=2.0, created=NOW + 1))
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "submitted" and row["brokerOrderId"] == "B5"
    assert env.broker.placed == []


def test_a_pending_row_with_no_matching_order_continues_as_unknown(env):
    paper_ledger.save_orders([ledger_row(state="pending", attempts=1, broker_id=None)])
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "unknown" and row["unknownPasses"] == 1 and env.broker.placed == []


def test_a_failed_listing_does_not_advance_the_unknown_passes(env):
    paper_ledger.save_orders([ledger_row(state="unknown", attempts=1, broker_id=None)])
    env.broker.open_error = broker_mod.err("timeout")
    out = run(pex.run_pass())
    assert only_row()["unknownPasses"] == 0
    assert out["errors"] == ["timeout"] and pex.worker_state()["consecutiveErrors"] == 1


# ------------------------------------------------------- reconciliation / fills

def test_fills_move_ledger_cash_through_partial_and_full_fills(env):
    place(env, intent("ASML", "buy", 4.0))                      # limit 100.3, reserved 4 x 100.3
    cash = paper_ledger.ledger_cash(orders(), 1000.0)
    assert cash["reservedEur"] == pytest.approx(401.2) and cash["cashEur"] == pytest.approx(598.8)

    env.broker.open = [mk_order("B1", qty=4.0, filled=1.5, status="partially_filled",
                                avg=100.0, value=150.0)]
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "partially_filled" and row["filledQuantity"] == 1.5
    cash = paper_ledger.ledger_cash(orders(), 1000.0)
    assert cash["filledBuyEur"] == 150.0
    assert cash["reservedEur"] == pytest.approx(2.5 * 100.3)
    assert cash["cashEur"] == pytest.approx(1000.0 - 150.0 - 2.5 * 100.3)

    env.broker.open = []
    env.broker.history = [mk_order("B1", qty=4.0, filled=4.0, status="filled", avg=100.0, value=400.0)]
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "filled" and row["filledQuantity"] == 4.0 and row["avgFillPrice"] == 100.0
    assert paper_ledger.ledger_cash(orders(), 1000.0) == {
        "cashEur": 600.0, "reservedEur": 0.0, "filledBuyEur": 400.0, "filledSellEur": 0.0}
    assert pex.worker_state()["lastReconcileAt"] == NOW


def test_a_filled_sell_returns_cash(env):
    paper_ledger.save_orders([ledger_row("NVDA", "buy", 5.0, state="filled", filled=5.0, value=500.0)])
    place(env, intent("NVDA", "sell", 5.0))
    env.broker.history = [mk_order("B1", ticker="NVDd_EQ", side="sell", qty=5.0, filled=5.0,
                                   status="filled", avg=110.0, value=550.0)]
    run(pex.run_pass())
    assert paper_ledger.ledger_cash(orders(), 1000.0)["cashEur"] == 1000.0 - 500.0 + 550.0


@pytest.mark.parametrize("status,raw,state", [("cancelled", "CANCELLED", "cancelled"),
                                              ("rejected", "REJECTED", "rejected")])
def test_a_broker_side_end_state_is_terminal_with_its_status_as_message(env, status, raw, state):
    place(env, intent())
    env.broker.history = [mk_order("B1", status=status, raw=raw)]
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == state and row["message"] == f"broker status {raw}"
    assert paper_ledger.non_terminal(orders()) == []


def test_a_filled_order_without_fill_data_takes_the_order_quantity(env):
    place(env, intent("ASML", "buy", 2.0))
    env.broker.history = [mk_order("B1", qty=2.0, filled=0.0, status="filled")]
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "filled" and row["filledQuantity"] == 2.0
    assert "taken from the order quantity" in row["message"]


def test_fills_never_decrease(env):
    paper_ledger.save_orders([ledger_row(qty=4.0, state="partially_filled", filled=3.0, value=300.0)])
    env.broker.open = [mk_order("B1", qty=4.0, filled=1.0, status="partially_filled", value=100.0)]
    run(pex.run_pass())
    assert only_row()["filledQuantity"] == 3.0


def test_a_tracked_order_missing_at_the_broker_stays_open_with_a_message(env):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="GONE")])
    run(pex.run_pass())
    row = only_row()
    assert row["state"] == "submitted" and "not found" in row["message"]


# --------------------------------------------------------- end-of-window cancel

def test_open_orders_stay_inside_the_window_and_are_cancelled_after_the_cutoff(env):
    place(env, intent("ASML", "buy", 2.0))
    env.broker.open = [mk_order("B1", qty=2.0)]
    run(pex.run_pass())
    assert env.broker.cancelled == [] and only_row()["state"] == "submitted"

    env.now = at(16, 59)
    run(pex.run_pass())
    assert env.broker.cancelled == []

    env.now = at(17, 5)                                      # close 17:30 - 30 min = 17:00
    out = run(pex.run_pass())
    assert env.broker.cancelled == ["B1"] and out["cancelled"] == 1
    row = only_row()
    assert row["state"] == "cancelled" and "end of the trading window" in row["message"]
    assert paper_ledger.ledger_cash(orders(), 1000.0)["reservedEur"] == 0.0     # reservation released


def test_a_partial_fill_that_landed_before_the_cancel_is_kept(env):
    place(env, intent("ASML", "buy", 4.0))
    env.broker.open = [mk_order("B1", qty=4.0, filled=1.5, status="partially_filled",
                                avg=100.0, value=150.0)]
    env.now = at(17, 5)
    run(pex.run_pass())
    row = only_row()
    assert env.broker.cancelled == ["B1"]
    assert row["state"] == "cancelled" and row["filledQuantity"] == 1.5 and row["filledValueEur"] == 150.0
    assert paper_ledger.ledger_cash(orders(), 1000.0)["cashEur"] == 850.0


def test_a_fill_that_the_cancel_raced_against_is_taken_from_history(env):
    place(env, intent("ASML", "buy", 4.0))
    env.broker.open = [mk_order("B1", qty=4.0)]
    env.broker.history = [mk_order("B1", qty=4.0, filled=4.0, status="filled", avg=100.0, value=400.0)]
    env.now = at(17, 5)
    run(pex.run_pass())
    # history is authoritative: the order was filled, and the row says so (not "cancelled")
    row = only_row()
    assert row["state"] == "filled" and row["filledQuantity"] == 4.0


def test_a_row_from_a_previous_session_is_cancelled_even_inside_todays_window(env):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="OLD", created=at(11, 0, day=16))])
    env.broker.open = [mk_order("OLD", qty=2.0, created=at(11, 0, day=16))]
    run(pex.run_pass())
    assert env.broker.cancelled == ["OLD"] and only_row()["state"] == "cancelled"


def test_no_session_today_cancels_leftover_orders(env):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="B1")])
    env.broker.open = [mk_order("B1", qty=2.0)]
    env.session = False
    run(pex.run_pass())
    assert env.broker.cancelled == ["B1"]


def test_a_cancel_the_broker_declines_leaves_the_row_for_reconcile(env):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="B1")])
    env.broker.open = [mk_order("B1", qty=2.0)]
    env.now = at(17, 5)
    env.broker.cancel_result = False
    out = run(pex.run_pass())
    assert only_row()["state"] == "submitted" and out["errors"] == [] and out["cancelled"] == 0


def test_a_cancel_error_is_counted_and_leaves_the_row(env):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="B1")])
    env.broker.open = [mk_order("B1", qty=2.0)]
    env.now = at(17, 5)
    env.broker.cancel_result = broker_mod.err("http", 500)
    out = run(pex.run_pass())
    assert only_row()["state"] == "submitted" and out["errors"] == ["http"]


# ------------------------------------------------------ kill switch (pure part)

KS = {"drawdownPct": 15, "vsBaselinePct": 10, "dailyLossPct": 5}


def reason(**over):
    kw = dict(limits=KS, equity=1000.0, peak=1000.0, bucket_pct=0.0, buy_hold_pct=0.0,
              gap_ref_pct=None, day_ref_equity=1000.0)
    kw.update(over)
    return pex.kill_switch_reason(**kw)


def test_no_condition_means_no_reason():
    assert reason() is None


def test_drawdown_from_the_peak():
    assert reason(equity=840.0, day_ref_equity=None, bucket_pct=-16.0, buy_hold_pct=-16.0
                  ).startswith("drawdown 16.0%")
    assert reason(equity=880.0, day_ref_equity=None) is None


def test_return_trailing_buy_and_hold_by_the_limit():
    r = reason(bucket_pct=0.0, buy_hold_pct=20.0)
    assert r.startswith("bucket trails buy-and-hold by 20.0 points")
    assert reason(bucket_pct=5.0, buy_hold_pct=14.0) is None


def test_baseline_gap_is_measured_from_the_gap_at_the_last_resume():
    assert reason(bucket_pct=0.0, buy_hold_pct=20.0, gap_ref_pct=-20.0) is None
    assert reason(bucket_pct=-8.0, buy_hold_pct=22.0, gap_ref_pct=-20.0).startswith("bucket trails")


def test_daily_loss_against_the_reference_equity():
    assert reason(equity=940.0, peak=940.0).startswith("daily loss 6.0%")
    assert reason(equity=960.0, peak=960.0) is None


def test_the_limits_come_from_config_with_plan_defaults_for_missing_keys():
    assert reason(equity=840.0, day_ref_equity=None, limits={"drawdownPct": 50}) is None
    assert reason(equity=840.0, day_ref_equity=None, limits=None).startswith("drawdown")
    assert reason(equity=840.0, peak=1000.0, limits={"drawdownPct": 50, "dailyLossPct": 5}
                  ).startswith("daily loss")


def test_missing_reference_data_skips_that_condition_instead_of_guessing():
    assert reason(peak=None, buy_hold_pct=None, bucket_pct=None, day_ref_equity=None,
                  equity=1.0) is None


# ------------------------------------------------- kill switch (through a pass)

def snapshot_row(date, equity, taken=None):
    return {"date": date, "equityEur": equity, "cashEur": 0.0, "positions": [],
            "accountTotalEur": None, "accountCashEur": None,
            "takenAt": taken if taken is not None else at(17, 45, day=int(date[-2:]))}


def book(env, price, *, weights=None, snaps=(), extra_rows=(), nvda_price=None):
    """Season since 2026-10-01 + a tracked ASML position of 5 bought for 500:
    equity = 5 x price + 500 cash (the other 500 of the budget is that cost)."""
    paper_ledger.write_season({"book": "practice", "strategyVersion": "s1",
                               "inceptionDate": "2026-10-01", "startedAt": 1.0,
                               "baselineWeights": weights or {"ASML": 100.0}})
    paper_ledger.save_orders([ledger_row("ASML", "buy", 5.0, state="filled", broker_id="H1",
                                         filled=5.0, value=500.0), *extra_rows])
    env.broker.positions_list = [position("ASMLa_EQ", 5.0)]
    env.quotes = {"ASML": price, "NVDA": nvda_price or 100.0}
    env.closes = {"ASML": [("2026-10-01", 100.0)], "NVDA": [("2026-10-01", 100.0)]}
    for s in snaps:
        paper_ledger.save_snapshot_row(s)


def halt_row():
    return paper_ledger.read_halt()


def test_a_healthy_bucket_trips_nothing_and_asks_the_broker_for_no_orders(env):
    book(env, 100.0, snaps=[snapshot_row("2026-10-18", 1000.0)])
    out = run(pex.run_pass())
    assert halt_row()["halted"] is False and out["halted"] is False
    assert env.alert.calls == []
    assert [c[0] for c in env.broker.calls] == ["positions"]      # no open orders: no listing


def test_drawdown_from_peak_equity_halts_cancels_and_pushes_once(env):
    book(env, 68.0, snaps=[snapshot_row("2026-10-10", 1000.0)],          # equity 840 = -16%
         extra_rows=[ledger_row("ASML", "buy", 1.0, state="submitted", broker_id="B7", limit=70.0)])
    run(pex.run_pass())
    h = halt_row()
    assert h["halted"] is True and h["by"] == "kill_switch" and h["reason"].startswith("drawdown 16.0%")
    assert env.broker.cancelled == ["B7"]
    assert [r["state"] for r in orders() if r["brokerOrderId"] == "B7"] == ["cancelled"]
    assert len(env.alert.calls) == 1
    call = env.alert.calls[0]
    assert (call["ticker"], call["source"], call["priority"]) == ("PAPER", "paper", 4)
    assert "drawdown 16.0%" in call["body"]
    run(pex.run_pass())
    run(pex.run_pass())
    assert len(env.alert.calls) == 1                         # one push per halt, however many passes
    assert halt_row()["notifiedAt"] == NOW


def test_trailing_the_buy_and_hold_baseline_halts(env):
    # bucket flat (equity 1000) while the baseline id rose 20 %
    book(env, 100.0, weights={"NVDA": 100.0}, nvda_price=120.0)
    run(pex.run_pass())
    assert halt_row()["halted"] is True
    assert halt_row()["reason"].startswith("bucket trails buy-and-hold by 20.0 points")


def test_a_daily_loss_against_the_previous_snapshot_halts(env):
    book(env, 88.0, snaps=[snapshot_row("2026-10-18", 1000.0)])          # equity 940 = -6 % on the day
    run(pex.run_pass())
    assert halt_row()["reason"].startswith("daily loss 6.0%")


def test_configured_kill_switch_limits_are_honoured(env):
    env.cfg["limits"] = {"killSwitch": {"drawdownPct": 50, "vsBaselinePct": 50, "dailyLossPct": 50}}
    book(env, 68.0, snaps=[snapshot_row("2026-10-18", 1000.0)])
    run(pex.run_pass())
    assert halt_row()["halted"] is False


def test_an_unpriced_position_skips_the_evaluation_instead_of_halting(env):
    book(env, 1.0, snaps=[snapshot_row("2026-10-18", 1000.0)])
    env.quotes = {}                                           # no listing quote and no broker price
    run(pex.run_pass())
    assert halt_row()["halted"] is False and env.alert.calls == []


def test_without_a_season_the_kill_switch_does_not_evaluate(env):
    env.broker.positions_list = [position("ASMLa_EQ", 5.0)]
    out = run(pex.run_pass())
    assert out["halted"] is False and env.broker.calls == []


@pytest.mark.parametrize("status", [notify.SENT, notify.QUEUED, notify.FILTERED, notify.DUPLICATE])
def test_every_delivery_status_but_failed_consumes_the_halt_push(env, status):
    env.alert.statuses = [status]
    book(env, 68.0, snaps=[snapshot_row("2026-10-10", 1000.0)])
    for _ in range(3):
        run(pex.run_pass())
    assert len(env.alert.calls) == 1
    assert halt_row()["notifiedAt"] == NOW and halt_row()["notifyStatus"] == status


def test_a_failed_delivery_is_retried_until_it_is_accepted_and_then_not_again(env):
    env.alert.statuses = [notify.FAILED, notify.SENT]
    book(env, 68.0, snaps=[snapshot_row("2026-10-10", 1000.0)])
    run(pex.run_pass())
    assert halt_row()["halted"] is True and halt_row()["notifiedAt"] is None
    assert len(env.alert.calls) == 1
    run(pex.run_pass())
    assert len(env.alert.calls) == 2 and halt_row()["notifiedAt"] == NOW
    run(pex.run_pass())
    assert len(env.alert.calls) == 2


# ---------------------------------------------------------------- error halts

def open_row_and_failing_listing(env, kind):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="B1")])
    env.broker.open_error = broker_mod.err(kind)


def test_three_consecutive_error_passes_halt_by_errors(env):
    open_row_and_failing_listing(env, "timeout")
    run(pex.run_pass())
    run(pex.run_pass())
    assert halt_row()["halted"] is False and pex.worker_state()["consecutiveErrors"] == 2
    run(pex.run_pass())
    h = halt_row()
    assert h["halted"] is True and h["by"] == "errors"
    assert "3 consecutive" in h["reason"] and "timeout" in h["reason"]
    assert len(env.alert.calls) == 1 and env.alert.calls[0]["priority"] == 4
    assert env.broker.cancelled == ["B1"]                    # the halt cancels the open order


def test_a_clean_pass_resets_the_consecutive_error_count(env):
    open_row_and_failing_listing(env, "http")
    run(pex.run_pass())
    run(pex.run_pass())
    env.broker.open_error = None
    run(pex.run_pass())
    assert pex.worker_state()["consecutiveErrors"] == 0
    env.broker.open_error = broker_mod.err("http")
    run(pex.run_pass())
    run(pex.run_pass())
    assert halt_row()["halted"] is False                     # 2 in a row again, not 4


def test_a_single_auth_error_halts_immediately(env):
    open_row_and_failing_listing(env, "auth")
    run(pex.run_pass())
    h = halt_row()
    assert h["halted"] is True and h["by"] == "errors" and "authentication" in h["reason"]
    assert len(env.alert.calls) == 1


# ----------------------------------------------------------- halt / resume API

def test_owner_halt_cancels_open_orders_pushes_once_and_is_idempotent(env):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="B1")])
    h = run(pex.halt("looking at the news"))
    assert h["halted"] is True and h["by"] == "owner" and h["reason"] == "looking at the news"
    assert env.broker.cancelled == ["B1"] and only_row()["state"] == "cancelled"
    again = run(pex.halt("second press"))
    assert again["reason"] == "looking at the news"
    assert len(env.alert.calls) == 1


def test_a_halt_reason_is_cleaned_and_defaulted(env):
    h = run(pex.halt("  line1\nline2\u202e http://evil.example/x  "))
    assert "\n" not in h["reason"] and "\u202e" not in h["reason"] and "http" not in h["reason"]
    pex.reset_state()
    paper_ledger.write_halt(dict(paper_ledger.HALT_DEFAULT))
    assert run(pex.halt(None))["reason"] == "halted by the owner"


def test_halt_during_a_placement_sends_nothing_further_and_cancels_what_went_out(env):
    async def go():
        env.broker.place_gate = asyncio.Event()

        async def placing():
            async with pex._get_lock():
                return await pex._place_locked(
                    [intent("NVDA", "sell", 1.0), intent("ASML", "buy", 2.0)],
                    source="manual", cfg=env.cfg, errs=[])
        t1 = asyncio.create_task(placing())
        for _ in range(10):
            await asyncio.sleep(0)
        assert env.broker.calls[-1][0] == "place"         # parked inside the first send
        t2 = asyncio.create_task(pex.halt("stop now"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert halt_row()["halted"] is True               # the flag is down before the lock is free
        assert not t2.done()
        env.broker.place_gate.set()
        await t1
        await t2
    run(go())
    assert [p[:2] for p in env.broker.placed] == [("NVDd_EQ", "sell")]     # the buy never went out
    sell, buy = orders()
    assert sell["state"] == "cancelled" and sell["message"] == "cancelled by halt"
    assert buy["state"] == "not_sent" and "halted before" in buy["message"]
    assert env.broker.cancelled == ["B1"] and len(env.alert.calls) == 1


def test_a_resume_that_wins_the_lock_leaves_the_pending_halt_effects_a_no_op(env):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="B1")])

    async def go():
        async with pex._get_lock():                      # something holds the lock...
            t = asyncio.create_task(pex.halt("x"))
            for _ in range(10):
                await asyncio.sleep(0)
            assert halt_row()["halted"] is True
            # ...and the owner's resume lands first
            paper_ledger.write_halt({**halt_row(), "halted": False, "resumedAt": NOW, "resumedBy": "owner"})
        await t
    run(go())
    assert halt_row()["halted"] is False
    assert env.broker.cancelled == [] and env.alert.calls == []
    assert only_row()["state"] == "submitted"


def test_the_halt_effects_still_run_when_the_halt_row_cannot_be_written(env, monkeypatch):
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="B1")])
    monkeypatch.setattr(pex.paper_ledger, "write_halt", lambda row, bucket="daily": False)
    with pytest.raises(paper_eval.PaperUnavailable, match="halt"):
        run(pex.halt("x"))
    assert env.broker.cancelled == ["B1"] and len(env.alert.calls) == 1


def test_resume_clears_the_halt_and_records_who_and_when(env):
    run(pex.halt("x"))
    env.now = NOW + 60
    row = run(pex.resume())
    assert row["halted"] is False and row["resumedAt"] == NOW + 60 and row["resumedBy"] == "owner"
    assert row["reason"] == "x" and row["by"] == "owner"            # the halt it ended stays visible
    assert paper_ledger.read_halt() == row


def test_resume_without_a_halt_changes_nothing(env):
    assert run(pex.resume())["halted"] is False
    assert not paper_ledger.HALT_FILE.exists()


def test_resume_resets_the_error_counter_so_the_next_clean_pass_is_not_a_fourth_strike(env):
    open_row_and_failing_listing(env, "timeout")
    for _ in range(3):
        run(pex.run_pass())
    assert halt_row()["halted"] is True
    run(pex.resume())
    assert pex.worker_state()["consecutiveErrors"] == 0


def test_a_resumed_kill_switch_re_arms_from_the_resume_instead_of_halting_again(env):
    book(env, 68.0, snaps=[snapshot_row("2026-10-10", 1000.0)])      # equity 840, peak 1000
    run(pex.run_pass())
    assert halt_row()["halted"] is True and len(env.alert.calls) == 1
    run(pex.resume())

    run(pex.run_pass())                                              # first pass after the resume
    h = halt_row()
    assert h["halted"] is False
    assert h["rearm"]["equityEur"] == 840.0 and h["rearm"]["at"] == NOW
    run(pex.run_pass())
    assert halt_row()["halted"] is False                             # still-true history does not re-halt

    env.quotes["ASML"] = 40.0                                        # equity 700: -16.7 % from 840
    run(pex.run_pass())
    h = halt_row()
    assert h["halted"] is True and h["reason"].startswith("drawdown 16.7%")
    assert len(env.alert.calls) == 2                                 # one push per halt


# -------------------------------------------------------------- rebalance

def held_book(env, mode="demo"):
    """Season + a tracked NVDA position of 5 bought for 500 (cash 500)."""
    env.cfg["mode"] = mode
    env.cfg["limits"] = {"maxTurnoverPctPerDay": 100, "adjustFraction": 1.0}
    paper_ledger.write_season({"book": "practice", "strategyVersion": "s1",
                               "inceptionDate": "2026-10-01", "startedAt": 1.0,
                               "baselineWeights": {"ASML": 60.0, "NVDA": 40.0}})
    paper_ledger.save_orders([ledger_row("NVDA", "buy", 5.0, state="filled", broker_id="H1",
                                         filled=5.0, value=500.0,
                                         intent_id="2026-10-16-s1-manual-NVDA-buy-1")])
    env.broker.positions_list = [position("NVDd_EQ", 5.0)]


TARGETS = {"ASML": 40, "NVDA": 0}


def test_a_dry_run_plans_but_never_writes_or_sends(env):
    held_book(env)
    before = orders()
    res = run(pex.rebalance(targets=TARGETS, dry_run=True))
    assert res["rows"] == []
    intents = res["plan"]["intents"]
    assert [(i["side"], i["id"]) for i in intents] == [("sell", "NVDA"), ("buy", "ASML")]
    assert set(intents[0]) >= {"limit_price", "ref_price", "ref_price_as_of", "value_eur", "reason"}
    assert orders() == before and env.broker.placed == []
    assert [c[0] for c in env.broker.calls] == ["positions"]


def test_a_dry_run_is_allowed_when_off_or_halted_and_says_so(env):
    held_book(env, mode="off")
    res = run(pex.rebalance(targets=TARGETS, dry_run=True))
    assert "mode_off" in res["plan"]["warnings"]
    paper_ledger.write_halt({"halted": True, "reason": "x", "at": 1.0, "by": "owner",
                             "resumedAt": None, "resumedBy": None})
    assert "halted" in run(pex.rebalance(targets=TARGETS, dry_run=True))["plan"]["warnings"]


def test_a_real_rebalance_sends_sells_before_buys_and_writes_the_rows(env):
    held_book(env)
    res = run(pex.rebalance(targets=TARGETS, dry_run=False))
    assert env.broker.placed == [("NVDd_EQ", "sell", 5.0, 99.7), ("ASMLa_EQ", "buy", 4.0, 100.3)]
    sell, buy = res["rows"]
    assert sell["intentId"] == f"{TODAY}-daily-s1-manual-NVDA-sell-1"
    assert buy["intentId"] == f"{TODAY}-daily-s1-manual-ASML-buy-1"
    assert [r["state"] for r in res["rows"]] == ["submitted", "submitted"]
    assert [r["state"] for r in orders()][-2:] == ["submitted", "submitted"]
    assert res["plan"]["intents"][0]["ticker"] == "NVDd_EQ"


def test_a_second_identical_rebalance_creates_no_new_order(env):
    held_book(env)
    run(pex.rebalance(targets=TARGETS, dry_run=False))
    assert len(env.broker.placed) == 2
    second = run(pex.rebalance(targets=TARGETS, dry_run=False))
    assert second["rows"] == [] and second["plan"]["intents"] == []
    assert len(env.broker.placed) == 2                       # in-flight orders are already counted
    assert {s["reason"] for s in second["plan"]["skipped"]} == {"within_band"}


def test_shadow_rebalance_writes_shadow_rows_and_places_nothing(env):
    held_book(env, mode="shadow")
    res = run(pex.rebalance(targets=TARGETS, dry_run=False))
    assert [r["state"] for r in res["rows"]] == ["shadow", "shadow"]
    assert env.broker.placed == []
    assert all(c[0] == "positions" for c in env.broker.calls)


@pytest.mark.parametrize("blocker", ["off", "halted"])
def test_a_real_rebalance_is_refused_with_a_conflict_before_any_broker_call(env, blocker):
    held_book(env, mode="off" if blocker == "off" else "demo")
    if blocker == "halted":
        paper_ledger.write_halt({"halted": True, "reason": "dd", "at": 1.0, "by": "kill_switch",
                                 "resumedAt": None, "resumedBy": None})
    before = orders()
    with pytest.raises(paper_eval.PaperConflict):
        run(pex.rebalance(targets=TARGETS, dry_run=False))
    assert orders() == before and env.broker.calls == []


def test_rebalance_needs_a_season(env):
    with pytest.raises(paper_eval.PaperConflict, match="no season"):
        run(pex.rebalance(targets=TARGETS, dry_run=True))


@pytest.mark.parametrize("targets", [None, {}, [], {"ASML": -1}, {"ASML": 101}, {"ASML": True},
                                     {"ASML": "40"}, {"": 10}, {"ASML": float("nan")}])
def test_malformed_targets_are_a_bad_request(env, targets):
    held_book(env)
    with pytest.raises(paper_eval.PaperBadRequest):
        run(pex.rebalance(targets=targets, dry_run=True))


@pytest.mark.parametrize("cash", [-1, 101, True, "2"])
def test_a_malformed_cash_percentage_is_a_bad_request(env, cash):
    held_book(env)
    with pytest.raises(paper_eval.PaperBadRequest, match="cashPct"):
        run(pex.rebalance(targets=TARGETS, cash_pct=cash, dry_run=True))


def test_rebalance_is_disabled_without_the_gate(env, monkeypatch):
    held_book(env)
    monkeypatch.setattr(pex.paper_eval, "enabled", lambda: False)
    with pytest.raises(paper_eval.PaperDisabled):
        run(pex.rebalance(targets=TARGETS, dry_run=True))


def test_an_unreachable_broker_is_unavailable_and_writes_nothing(env):
    held_book(env)
    env.broker.positions_error = broker_mod.err("timeout")
    before = orders()
    with pytest.raises(paper_eval.PaperUnavailable):
        run(pex.rebalance(targets=TARGETS, dry_run=False))
    assert orders() == before


def test_an_auth_error_while_placing_halts_the_bucket_and_pushes(env):
    held_book(env)
    env.broker.place_results = [broker_mod.err("auth", 401)]
    run(pex.rebalance(targets=TARGETS, dry_run=False))
    assert halt_row()["halted"] is True and halt_row()["by"] == "errors"
    assert len(env.alert.calls) == 1


# -------------------------------------------------------------------- seed

def seed_book(env, mode="demo", weights=None):
    env.cfg["mode"] = mode
    env.cfg["limits"] = {"adjustFraction": 1.0}
    paper_ledger.write_season({"book": "practice", "strategyVersion": "s1",
                               "inceptionDate": "2026-10-19", "startedAt": 1.0,
                               "baselineWeights": weights or {"ASML": 60.0, "NVDA": 40.0}})


def test_seed_exempts_the_caps_and_labels_the_rows_seed(env):
    seed_book(env)
    preview = run(pex.seed(dry_run=True))
    plan = preview["plan"]
    assert preview["rows"] == [] and env.broker.placed == []
    asml = next(i for i in plan["intents"] if i["id"] == "ASML")
    assert plan["targetsApplied"]["ASML"] == pytest.approx(58.8)          # not clipped to the 40 % cap
    assert asml["value_eur"] == pytest.approx(588.0, rel=1e-3) and asml["side"] == "buy"
    # the same targets as a plain rebalance: clipped to 40 % and stopped by the daily turnover cap
    plain = run(pex.rebalance(targets={"ASML": 60, "NVDA": 40}, dry_run=True))["plan"]
    assert plain["targetsApplied"]["ASML"] == 40.0
    assert {"id": "ASML", "reason": "turnover_cap"} in plain["skipped"]

    res = run(pex.seed(dry_run=False))
    assert {r["source"] for r in res["rows"]} == {"seed"}
    assert res["rows"][0]["intentId"].startswith(f"{TODAY}-daily-s1-seed-")
    assert len(env.broker.placed) == 2 and all(side == "buy" for _, side, _, _ in env.broker.placed)


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("why", ["position", "open_row"])
def test_seed_is_a_conflict_unless_the_bucket_is_empty(env, why, dry_run):
    seed_book(env)
    if why == "position":
        env.broker.positions_list = [position("NVDd_EQ", 1.0)]
    else:
        paper_ledger.save_orders([ledger_row(state="submitted")])
    before = orders()
    with pytest.raises(paper_eval.PaperConflict, match="empty bucket"):
        run(pex.seed(dry_run=dry_run))
    assert orders() == before and env.broker.placed == []


def test_seed_is_allowed_again_once_only_terminal_rows_remain_and_positions_are_gone(env):
    seed_book(env)
    paper_ledger.save_orders([ledger_row("NVDA", "buy", 1.0, state="filled", filled=1.0, value=100.0),
                              ledger_row("NVDA", "sell", 1.0, state="filled", filled=1.0, value=110.0)])
    res = run(pex.seed(dry_run=True))
    assert res["plan"]["intents"]


def test_seed_needs_a_season_with_baseline_weights(env):
    with pytest.raises(paper_eval.PaperConflict, match="no season"):
        run(pex.seed(dry_run=True))
    seed_book(env, weights={})
    paper_ledger.write_season({"book": "practice", "inceptionDate": "2026-10-19", "baselineWeights": {}})
    with pytest.raises(paper_eval.PaperConflict, match="baseline weights"):
        run(pex.seed(dry_run=True))


def test_a_seed_in_mode_off_is_refused_when_real_and_previewable_when_dry(env):
    seed_book(env, mode="off")
    assert run(pex.seed(dry_run=True))["plan"]["intents"]
    with pytest.raises(paper_eval.PaperConflict):
        run(pex.seed(dry_run=False))


# ------------------------------------------------------------------- misc

def test_the_broker_is_always_the_demo_broker(monkeypatch):
    seen = []
    monkeypatch.setattr(pex.broker_mod, "get_broker", lambda env="demo": seen.append(env))
    assert pex._broker() is None
    assert run(pex.run_pass()) == {"active": 0, "cancelled": 0, "errors": [], "halted": False}
    assert seen == ["demo", "demo"]       # every lookup asks for the demo broker, never another env


def test_set_mode_writes_through_config_store_and_normalises(env, monkeypatch):
    cfg = {"paperTrading": {"mode": "off", "budgetEur": 1000}}
    monkeypatch.setattr(pex.config_store, "update", lambda mut: (mut(cfg), cfg)[1])
    assert run(pex.set_mode(" DEMO ")) == "demo"
    assert cfg["paperTrading"] == {"mode": "demo", "budgetEur": 1000}
    empty = {}
    monkeypatch.setattr(pex.config_store, "update", lambda mut: (mut(empty), empty)[1])
    assert run(pex.set_mode("shadow")) == "shadow" and empty == {"paperTrading": {"mode": "shadow"}}


@pytest.mark.parametrize("mode", ["live-auto", "live", "", None, 3])
def test_set_mode_rejects_anything_but_off_shadow_demo(env, monkeypatch, mode):
    called = []
    monkeypatch.setattr(pex.config_store, "update", lambda mut: called.append(1))
    with pytest.raises(paper_eval.PaperBadRequest):
        run(pex.set_mode(mode))
    assert called == []


def test_set_mode_needs_the_gate(env, monkeypatch):
    monkeypatch.setattr(pex.paper_eval, "enabled", lambda: False)
    with pytest.raises(paper_eval.PaperDisabled):
        run(pex.set_mode("demo"))


# --------------------------------------- the kill switch runs in demo mode only

def apply_mode_to_env(env, monkeypatch):
    """config_store.update stand-in whose write is visible to the injected config."""
    def update(mutate):
        holder = {"paperTrading": {"mode": env.cfg["mode"]}}
        mutate(holder)
        env.cfg["mode"] = holder["paperTrading"]["mode"]
        return holder
    monkeypatch.setattr(pex.config_store, "update", update)


@pytest.mark.parametrize("mode,halts", [("off", False), ("shadow", False), ("demo", True)])
def test_market_triggers_evaluate_only_in_demo_and_idle_polling_makes_no_broker_call(env, mode, halts):
    env.cfg["mode"] = mode
    book(env, 68.0, snaps=[snapshot_row("2026-10-10", 1000.0)])       # equity 840: -16 % from the peak
    run(pex.run_pass())
    assert halt_row()["halted"] is halts
    assert len(env.alert.calls) == (1 if halts else 0)
    if not halts:
        assert env.broker.calls == []                                 # no positions read, no listing


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_consecutive_errors_do_not_halt_outside_demo(env, mode):
    env.cfg["mode"] = mode
    open_row_and_failing_listing(env, "timeout")
    for _ in range(5):
        out = run(pex.run_pass())
        assert out["errors"] == ["timeout"] and out["halted"] is False
    assert halt_row()["halted"] is False and env.alert.calls == []
    assert pex.worker_state()["consecutiveErrors"] == 0
    assert pex.worker_state()["lastError"] == "timeout"


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_an_auth_error_reconciling_open_rows_still_halts_in_any_mode(env, mode):
    env.cfg["mode"] = mode
    open_row_and_failing_listing(env, "auth")
    run(pex.run_pass())
    h = halt_row()
    assert h["halted"] is True and h["by"] == "errors" and "authentication" in h["reason"]
    assert len(env.alert.calls) == 1 and env.broker.cancelled == ["B1"]


@pytest.mark.parametrize("mode,halts", [("shadow", False), ("demo", True)])
def test_auth_while_reading_positions_for_a_rebalance_halts_only_in_demo(env, mode, halts):
    held_book(env, mode=mode)
    env.broker.positions_error = broker_mod.err("auth", 401)
    with pytest.raises(paper_eval.PaperUnavailable):
        run(pex.rebalance(targets=TARGETS, dry_run=False))
    assert halt_row()["halted"] is halts and len(env.alert.calls) == (1 if halts else 0)


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_open_rows_are_still_reconciled_and_cancelled_outside_demo(env, mode):
    env.cfg["mode"] = mode
    paper_ledger.save_orders([ledger_row("ASML", "buy", 2.0, state="submitted", broker_id="B1"),
                              ledger_row("NVDA", "buy", 1.0, state="submitted", broker_id="B2")])
    env.broker.open = [mk_order("B1", qty=2.0)]
    env.broker.history = [mk_order("B2", ticker="NVDd_EQ", qty=1.0, filled=1.0, status="filled",
                                   avg=100.0, value=100.0)]
    env.now = at(17, 5)
    run(pex.run_pass())
    by_id = {r["brokerOrderId"]: r for r in orders()}
    assert by_id["B2"]["state"] == "filled"                           # reconciled
    assert by_id["B1"]["state"] == "cancelled" and env.broker.cancelled == ["B1"]   # window over


def test_changing_the_mode_to_demo_re_arms_the_kill_switch(env, monkeypatch):
    apply_mode_to_env(env, monkeypatch)
    env.cfg["mode"] = "off"
    book(env, 68.0, snaps=[snapshot_row("2026-10-10", 1000.0)])       # equity 840, peak 1000
    run(pex.run_pass())                                               # off: nothing evaluated
    assert halt_row()["halted"] is False and env.broker.calls == []
    pex._state["consecutiveErrors"] = 2

    assert run(pex.set_mode("demo")) == "demo" and env.cfg["mode"] == "demo"
    h = halt_row()
    assert h["halted"] is False and h["resumedAt"] == NOW and h["resumedBy"] == "mode"
    assert h["rearm"] is None and pex.worker_state()["consecutiveErrors"] == 0

    run(pex.run_pass())                                               # first demo pass records the reference
    assert halt_row()["rearm"]["equityEur"] == 840.0 and halt_row()["halted"] is False
    run(pex.run_pass())
    assert halt_row()["halted"] is False                              # the 16 % history no longer counts
    env.quotes["ASML"] = 40.0                                         # equity 700: -16.7 % from 840
    run(pex.run_pass())
    assert halt_row()["halted"] is True and halt_row()["reason"].startswith("drawdown 16.7%")
    assert len(env.alert.calls) == 1


def test_setting_demo_when_already_demo_does_not_re_arm(env, monkeypatch):
    apply_mode_to_env(env, monkeypatch)
    env.cfg["mode"] = "demo"
    run(pex.set_mode("demo"))
    assert not paper_ledger.HALT_FILE.exists()


def test_leaving_demo_does_not_touch_the_kill_switch_state(env, monkeypatch):
    apply_mode_to_env(env, monkeypatch)
    run(pex.set_mode("shadow"))
    assert env.cfg["mode"] == "shadow" and not paper_ledger.HALT_FILE.exists()


def test_a_standing_halt_survives_a_switch_to_demo_until_the_owner_resumes(env, monkeypatch):
    apply_mode_to_env(env, monkeypatch)
    env.cfg["mode"] = "shadow"
    halted = {"halted": True, "reason": "drawdown", "at": 5.0, "by": "kill_switch",
              "resumedAt": None, "resumedBy": None, "notifiedAt": 6.0}
    paper_ledger.write_halt(dict(halted))
    run(pex.set_mode("demo"))
    assert paper_ledger.read_halt() == halted


def test_the_trading_window_is_open_plus_30_to_close_minus_30(env):
    def state(h, m):
        return pex.window_state("XETR", at(h, m))
    assert state(9, 29)["open"] is False and state(9, 30)["open"] is True
    assert state(16, 59)["open"] is True and state(17, 0)["open"] is False
    s = state(11, 0)
    assert (s["opensAt"], s["closesAt"]) == (int(at(9, 30)), int(at(17, 0)))
    env.session = False
    assert pex.window_state("XETR", NOW) == {"open": False, "opensAt": None, "closesAt": None}


def test_an_early_close_shortens_the_window(env, monkeypatch):
    def early(mic, day):
        o, c = bounds(day)
        return o, c.replace(hour=13, minute=0)
    monkeypatch.setattr(pex.calendars, "session_bounds", early)
    assert pex.window_state("XETR", at(12, 29))["open"] is True
    assert pex.window_state("XETR", at(12, 31))["open"] is False


def test_the_worker_needs_the_fast_cadence_while_a_row_is_open_or_a_window_is_open(env):
    assert pex.needs_fast_pass(NOW) is True                              # window open
    env.now = at(18, 0)
    assert pex.needs_fast_pass(at(18, 0)) is False                       # closed, nothing open
    paper_ledger.save_orders([ledger_row(state="unknown", broker_id=None)])
    assert pex.needs_fast_pass(at(18, 0)) is True                        # a non-terminal row
    paper_ledger.save_orders([ledger_row(state="filled", filled=2.0, value=200.0)])
    assert pex.needs_fast_pass(at(18, 0)) is False                       # terminal rows do not count
    env.session = False
    assert pex.needs_fast_pass(NOW) is False                             # not a session day


def test_status_extra_reports_halt_open_intents_reconcile_time_and_windows(env):
    paper_ledger.save_orders([ledger_row(state="submitted"), ledger_row(state="filled", filled=2.0,
                                                                        value=200.0)])
    env.broker.open = [mk_order("B1", qty=2.0)]
    extra = pex.status_extra()
    assert extra["halt"]["halted"] is False and extra["openIntents"] == 1
    assert extra["lastReconcileAt"] is None
    assert extra["tradingWindow"] == {"XETR": {"open": True, "opensAt": int(at(9, 30)),
                                               "closesAt": int(at(17, 0))}}
    run(pex.run_pass())
    assert pex.status_extra()["lastReconcileAt"] == NOW


def test_a_worker_pass_waits_for_a_placement_in_progress(env):
    """Single lock around every execute/reconcile: the pass must not reconcile
    while a send is in flight."""
    async def go():
        env.broker.place_gate = asyncio.Event()

        async def placing():
            async with pex._get_lock():
                return await pex._place_locked([intent()], source="manual", cfg=env.cfg, errs=[])
        t1 = asyncio.create_task(placing())
        for _ in range(10):
            await asyncio.sleep(0)
        assert env.broker.calls[-1][0] == "place"            # parked inside the send
        t2 = asyncio.create_task(pex.run_pass())
        for _ in range(10):
            await asyncio.sleep(0)
        assert env.broker.calls[-1][0] == "place"            # the pass has not touched the broker
        assert not t2.done()
        env.broker.place_gate.set()
        await t1
        await t2
    run(go())
    kinds = [c[0] for c in env.broker.calls]
    assert kinds.index("place") < len(kinds) - 1             # the pass reconciled AFTER the send
    assert kinds[kinds.index("place") + 1] == "open_orders"


# =============================================== Phase 5: two buckets, one account

def enable_intraday(env, mode="demo"):
    env.cfg_intraday.update(enabled=True, mode=mode, limits={"maxTurnoverPctPerDay": 100})


def i_row(pid="ASML", side="buy", qty=2.0, **kw):
    """A ledger row of the intraday bucket."""
    row = ledger_row(pid, side, qty, **kw)
    row["bucket"] = "intraday"
    row["strategyVersion"] = "i1"
    row["intentId"] = kw.get("intent_id") or f"{TODAY}-intraday-i1-agent-{pid}-{side}-9"
    return row


def halt_of(bucket):
    return paper_ledger.read_halt(bucket)


def test_new_rows_carry_their_bucket_and_a_bucket_prefixed_intent_id(env):
    enable_intraday(env)

    async def go(cfg):
        async with pex._get_lock():
            return await pex._place_locked([intent("ASML", "buy", 1.0)], source="agent",
                                           cfg=cfg, errs=env.errs)
    daily, = run(go(env.cfg))
    intra, = run(go(env.cfg_intraday))
    assert daily["bucket"] == "daily" and daily["intentId"] == f"{TODAY}-daily-s1-agent-ASML-buy-1"
    assert intra["bucket"] == "intraday" and intra["intentId"] == f"{TODAY}-intraday-i1-agent-ASML-buy-1"
    assert [r["strategyVersion"] for r in orders()] == ["s1", "i1"]


def test_existing_rows_without_a_bucket_belong_to_the_daily_bucket(env):
    paper_ledger.save_orders([ledger_row(state="submitted"), i_row("NVDA", state="submitted", broker_id="B2")])
    assert pex.status_extra()["openIntents"] == 1
    assert pex.status_extra("intraday")["openIntents"] == 1
    paper_ledger.save_orders([ledger_row(state="submitted")])
    assert pex.status_extra("intraday")["openIntents"] == 0


@pytest.mark.parametrize("first,second", [("daily", "intraday"), ("intraday", "daily")])
def test_a_broker_order_attached_to_one_bucket_is_never_attached_to_the_other(env, first, second):
    enable_intraday(env)
    env.broker.open.append(mk_order("X9", qty=2.0, created=NOW))
    cfgs = {"daily": env.cfg, "intraday": env.cfg_intraday}

    async def go(bucket):
        async with pex._get_lock():
            return await pex._place_locked([intent("ASML", "buy", 2.0)], source="agent",
                                           cfg=cfgs[bucket], errs=env.errs)
    a, = run(go(first))
    b, = run(go(second))
    assert a["brokerOrderId"] == "X9" and b["brokerOrderId"] == "B1"        # the second one was SENT
    assert len(env.broker.placed) == 1
    assert {r["bucket"]: r["brokerOrderId"] for r in orders()} == {first: "X9", second: "B1"}


def test_a_reconcile_never_hands_one_buckets_broker_order_to_the_other_buckets_unknown_row(env):
    enable_intraday(env)
    paper_ledger.save_orders([
        i_row(state="submitted", broker_id="X9"),
        ledger_row(state="unknown", broker_id=None, attempts=1)])          # daily, send outcome unknown
    env.broker.open.append(mk_order("X9", qty=2.0, created=NOW))
    run(pex.run_pass())
    daily = next(r for r in orders() if r["bucket" if "bucket" in r else "intentId"] != "intraday"
                 and paper_ledger.row_bucket(r) == "daily")
    assert daily["brokerOrderId"] is None and daily["state"] == "unknown"
    assert daily["unknownPasses"] == 1                                    # not found, NOT stolen


def test_each_bucket_plans_on_its_own_cash_and_holdings(env):
    enable_intraday(env)
    held_book(env)                                  # DAILY holds NVDA 5 bought for 500 (cash 500)
    paper_ledger.write_season({"book": "practice", "bucket": "intraday", "strategyVersion": "i1",
                               "inceptionDate": "2026-10-01", "startedAt": 1.0,
                               "baselineWeights": {"ASML": 60.0, "NVDA": 40.0}}, "intraday")
    plan = run(pex.rebalance(targets={"ASML": 40, "NVDA": 0}, dry_run=True, bucket="intraday"))["plan"]
    assert plan["equityEur"] == pytest.approx(1000.0)                     # its own EUR 1,000, not 500 + a share
    assert [(i["side"], i["id"]) for i in plan["intents"]] == [("buy", "ASML")]    # nothing to sell
    daily = run(pex.rebalance(targets={"ASML": 40, "NVDA": 0}, dry_run=True))["plan"]
    assert [(i["side"], i["id"]) for i in daily["intents"]] == [("sell", "NVDA"), ("buy", "ASML")]


def test_a_real_intraday_rebalance_writes_intraday_rows_and_leaves_the_daily_ones(env):
    enable_intraday(env)
    held_book(env)
    paper_ledger.write_season({"book": "practice", "bucket": "intraday", "strategyVersion": "i1",
                               "inceptionDate": "2026-10-01", "startedAt": 1.0,
                               "baselineWeights": {"ASML": 100.0}}, "intraday")
    before = orders()
    res = run(pex.rebalance(targets={"ASML": 40}, dry_run=False, bucket="intraday", source="agent"))
    assert {r["bucket"] for r in res["rows"]} == {"intraday"}
    assert res["rows"][0]["intentId"].startswith(f"{TODAY}-intraday-i1-agent-")
    assert orders()[:len(before)] == before and len(orders()) == len(before) + 1


def test_seed_of_one_bucket_is_not_blocked_by_the_other_buckets_position(env):
    enable_intraday(env)
    held_book(env)                                  # daily holds NVDA 5; the broker holds it too
    paper_ledger.write_season({"book": "practice", "bucket": "intraday", "strategyVersion": "i1",
                               "inceptionDate": "2026-10-19", "startedAt": 1.0,
                               "baselineWeights": {"ASML": 100.0}}, "intraday")
    assert run(pex.seed(dry_run=True, bucket="intraday"))["plan"]["intents"]
    with pytest.raises(paper_eval.PaperConflict, match="empty bucket"):
        run(pex.seed(dry_run=True))                 # the daily bucket does hold NVDA


def test_a_position_nobody_explains_blocks_a_seed(env):
    enable_intraday(env)
    paper_ledger.write_season({"book": "practice", "bucket": "intraday", "strategyVersion": "i1",
                               "inceptionDate": "2026-10-19", "startedAt": 1.0,
                               "baselineWeights": {"ASML": 100.0}}, "intraday")
    env.broker.positions_list = [position("NVDd_EQ", 1.0)]
    with pytest.raises(paper_eval.PaperConflict, match="empty bucket"):
        run(pex.seed(dry_run=True, bucket="intraday"))


def test_an_owner_halt_stops_one_bucket_only_and_cancels_only_its_orders(env):
    enable_intraday(env)
    paper_ledger.save_orders([ledger_row(state="submitted", broker_id="D1"),
                              i_row("NVDA", state="submitted", broker_id="I1")])
    h = run(pex.halt("intraday looks wrong", bucket="intraday"))
    assert h["halted"] is True and halt_of("intraday")["halted"] is True
    assert halt_of("daily")["halted"] is False
    assert env.broker.cancelled == ["I1"]
    states = {r["bucket"] if "bucket" in r else "daily": r["state"] for r in orders()}
    assert states == {"daily": "submitted", "intraday": "cancelled"}
    assert len(env.alert.calls) == 1 and env.alert.calls[0]["title"] == "Paper trading halted (intraday)"
    paper_ledger.write_season({"book": "practice", "strategyVersion": "s1", "inceptionDate": "2026-10-01",
                               "startedAt": 1.0, "baselineWeights": {"ASML": 100.0}})
    env.cfg["mode"] = "shadow"
    assert run(pex.rebalance(targets={"ASML": 30}, dry_run=False))["rows"]       # daily still trades
    with pytest.raises(paper_eval.PaperConflict, match="halted"):
        pex._gate(env.cfg_intraday)
    pex._gate(env.cfg)                                                      # the daily gate is open


def test_resume_clears_one_bucket_only(env):
    enable_intraday(env)
    run(pex.halt("a", bucket="daily"))
    run(pex.halt("b", bucket="intraday"))
    row = run(pex.resume(bucket="intraday"))
    assert row["halted"] is False and halt_of("intraday")["halted"] is False
    assert halt_of("daily")["halted"] is True


def test_the_kill_switch_uses_each_buckets_own_limits_and_halts_only_that_bucket(env):
    enable_intraday(env)
    env.cfg_intraday["limits"] = {"killSwitch": {"dailyLossPct": 2}}       # tighter than daily's default 5
    paper_ledger.write_season({"book": "practice", "strategyVersion": "s1",
                               "inceptionDate": "2026-10-01", "startedAt": 1.0,
                               "baselineWeights": {"ASML": 100.0}})
    paper_ledger.write_season({"book": "practice", "bucket": "intraday", "strategyVersion": "i1",
                               "inceptionDate": "2026-10-01", "startedAt": 1.0,
                               "baselineWeights": {"ASML": 100.0}}, "intraday")
    paper_ledger.save_orders([ledger_row("ASML", "buy", 5.0, state="filled", broker_id="H1",
                                         filled=5.0, value=500.0),
                              i_row("ASML", "buy", 5.0, state="filled", broker_id="H2",
                                    filled=5.0, value=500.0)])
    env.broker.positions_list = [position("ASMLa_EQ", 10.0)]                # the broker holds both
    env.quotes = {"ASML": 94.0, "NVDA": 100.0}                              # each bucket: 470 + 500 = -3 %
    env.closes = {"ASML": [("2026-10-01", 100.0)], "NVDA": [("2026-10-01", 100.0)]}
    for b in ("daily", "intraday"):
        paper_ledger.save_snapshot_row(snapshot_row("2026-10-18", 1000.0), b)
    out = run(pex.run_pass())
    assert halt_of("intraday")["halted"] is True and halt_of("intraday")["reason"].startswith(
        "intraday: daily loss 3.0%")
    assert halt_of("daily")["halted"] is False and out["halted"] is True
    assert len(env.alert.calls) == 1 and "intraday" in env.alert.calls[0]["title"]


def test_a_bucket_that_is_not_in_demo_is_not_evaluated(env):
    enable_intraday(env, mode="shadow")
    paper_ledger.write_season({"book": "practice", "bucket": "intraday", "strategyVersion": "i1",
                               "inceptionDate": "2026-10-01", "startedAt": 1.0,
                               "baselineWeights": {"ASML": 100.0}}, "intraday")
    out = run(pex.run_pass())
    assert out["halted"] is False and env.broker.calls == []               # nothing to reconcile or evaluate


def test_an_auth_error_halts_both_buckets_with_one_push(env):
    enable_intraday(env)
    open_row_and_failing_listing(env, "auth")
    run(pex.run_pass())
    assert halt_of("daily")["halted"] is True and halt_of("intraday")["halted"] is True
    assert halt_of("daily")["at"] == halt_of("intraday")["at"]
    assert len(env.alert.calls) == 1
    assert env.alert.calls[0]["title"] == "Paper trading halted (daily, intraday)"
    assert halt_of("daily")["notifiedAt"] and halt_of("intraday")["notifiedAt"]
    run(pex.run_pass())
    assert len(env.alert.calls) == 1


def test_an_auth_error_halts_only_the_enabled_buckets(env):
    open_row_and_failing_listing(env, "auth")                               # intraday is disabled
    run(pex.run_pass())
    assert halt_of("daily")["halted"] is True and halt_of("intraday")["halted"] is False
    assert env.alert.calls[0]["title"] == "Paper trading halted"


def test_consecutive_errors_halt_the_buckets_in_demo(env):
    enable_intraday(env, mode="demo")
    env.cfg["mode"] = "shadow"
    open_row_and_failing_listing(env, "timeout")
    for _ in range(3):
        run(pex.run_pass())
    assert halt_of("intraday")["halted"] is True and halt_of("daily")["halted"] is False


def test_a_failed_push_of_a_two_bucket_halt_is_retried_once_for_the_pair(env):
    enable_intraday(env)
    env.alert.statuses = [notify.FAILED, notify.SENT]
    open_row_and_failing_listing(env, "auth")
    run(pex.run_pass())
    assert len(env.alert.calls) == 1 and halt_of("daily")["notifiedAt"] is None
    run(pex.run_pass())
    assert len(env.alert.calls) == 2 and halt_of("intraday")["notifiedAt"] is not None


def test_set_mode_writes_the_intraday_bucket_block_and_refuses_a_disabled_one(env, monkeypatch):
    cfg = {"paperTrading": {"mode": "off", "buckets": {"intraday": {"enabled": True, "mode": "off"}}}}
    monkeypatch.setattr(pex.config_store, "update", lambda mut: (mut(cfg), cfg)[1])
    assert run(pex.set_mode("shadow", bucket="intraday")) == "shadow"
    assert cfg["paperTrading"]["buckets"]["intraday"]["mode"] == "shadow"
    assert cfg["paperTrading"]["mode"] == "off"                             # daily untouched
    cfg["paperTrading"]["buckets"]["intraday"]["enabled"] = False
    with pytest.raises(paper_eval.PaperConflict, match="not enabled"):
        run(pex.set_mode("demo", bucket="intraday"))
    cfg["paperTrading"].pop("buckets")
    with pytest.raises(paper_eval.PaperConflict, match="not enabled"):
        run(pex.set_mode("demo", bucket="intraday"))
    assert cfg["paperTrading"] == {"mode": "off"}


def test_switching_a_bucket_to_demo_rearms_its_own_kill_switch_only(env, monkeypatch):
    enable_intraday(env, mode="shadow")
    cfg = {"paperTrading": {"buckets": {"intraday": {"enabled": True, "mode": "shadow"}}}}
    monkeypatch.setattr(pex.config_store, "update", lambda mut: (mut(cfg), cfg)[1])
    run(pex.set_mode("demo", bucket="intraday"))
    assert halt_of("intraday")["resumedBy"] == "mode" and halt_of("daily")["resumedBy"] is None


def test_an_unknown_bucket_is_refused_by_every_action(env):
    with pytest.raises(ValueError, match="bucket"):
        run(pex.halt("x", bucket="weekly"))
    with pytest.raises(ValueError, match="bucket"):
        run(pex.resume(bucket="weekly"))
    with pytest.raises(ValueError, match="bucket"):
        run(pex.rebalance(targets=TARGETS, bucket="weekly"))
    with pytest.raises(ValueError, match="bucket"):
        run(pex.set_mode("demo", bucket="weekly"))
