"""Task 10: the planless-day shadow ledger. LOGGING HOOK ONLY."""
import ast
import inspect
import os

import pandas as pd

from agent.trader.records import DECISIONS_FILE
from agent.trader.shadow_ledger import LEDGER_FILE, ShadowLedger

TZ = "America/New_York"
NOW = pd.Timestamp("2026-07-17 09:42:00", tz=TZ)


def _setup(**kw):
    base = dict(now=NOW, date="2026-07-17", mechanism="extreme_reject_close",
                entry=28644.75, stop=28662.5, result=-17.75)
    base.update(kw)
    return base


def test_a_planless_day_logs_a_paper_outcome(tmp_path):
    led = ShadowLedger(tmp_path)
    assert led.log(**_setup()) is True
    rows = led.read()
    assert len(rows) == 1 and rows[0]["mechanism"] == "extreme_reject_close"


def test_nothing_is_logged_when_a_plan_is_armed(tmp_path):
    """The ledger's meaning is 'what a PLANLESS session gave up'. A row from a day that
    traded would inflate the cost the stay-dark decision is weighed against."""
    led = ShadowLedger(tmp_path)
    assert led.log(plan_armed=True, **_setup()) is False
    assert led.read() == []


def test_the_record_carries_mechanism_entry_sl_and_result(tmp_path):
    led = ShadowLedger(tmp_path)
    led.log(**_setup())
    row = led.read()[0]
    for field in ("mechanism", "entry", "stop", "result", "time", "date"):
        assert row.get(field) is not None, field


def test_every_record_is_marked_as_never_acted_on(tmp_path):
    """'Log the paper outcome; act on nothing.' The flag makes that readable on disk."""
    led = ShadowLedger(tmp_path)
    led.log(**_setup())
    assert led.read()[0]["acted"] is False


def test_the_ledger_writes_to_its_own_file_not_trader_decisions(tmp_path):
    led = ShadowLedger(tmp_path)
    led.log(**_setup())
    assert os.path.exists(tmp_path / LEDGER_FILE)
    assert not os.path.exists(tmp_path / DECISIONS_FILE)
    assert LEDGER_FILE != DECISIONS_FILE


def test_the_ledger_writes_no_legacy_state_file(tmp_path):
    led = ShadowLedger(tmp_path)
    led.log(**_setup())
    for forbidden in ("events.jsonl", "daily.json", "hypothesis.json",
                      "position.json", "smts.json"):
        assert not (tmp_path / forbidden).exists()


def test_the_ledger_never_places_an_order():
    """AST-level: the module must not reference any order-placement entry point."""
    import agent.trader.shadow_ledger as mod
    tree = ast.parse(inspect.getsource(mod))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for forbidden in ("place", "fill_market", "OrderSim", "RestingOrder", "_enter"):
        assert forbidden not in names, f"the ledger must not reach {forbidden}"


def test_the_ledger_imports_nothing_that_can_trade():
    import agent.trader.shadow_ledger as mod
    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("order_sim" in m or "live_orders" in m or "executor" in m
                   for m in imported), imported


def test_a_write_failure_never_propagates(tmp_path):
    """Instrumentation does not get to take the session down."""
    led = ShadowLedger(tmp_path / "a-file")
    (tmp_path / "a-file").write_text("not a directory", encoding="utf-8")
    assert led.log(**_setup()) is False


def test_reading_an_absent_ledger_is_empty_not_an_error(tmp_path):
    assert ShadowLedger(tmp_path).read() == []


def test_the_hook_counts_what_it_logged(tmp_path):
    """The forward holdout is a COUNT question before it is a P&L one."""
    led = ShadowLedger(tmp_path)
    led.log(**_setup())
    led.log(**_setup(mechanism="fvg_1m_post_extreme"))
    led.log(plan_armed=True, **_setup())
    assert led.logged == 2
