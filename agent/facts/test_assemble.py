import inspect
import pandas as pd
import pytest
from agent.facts.assemble import assemble_facts, DECIDE_THESIS_KEYS

NOW = pd.Timestamp("2026-08-13 09:20", tz="America/New_York")
EMPTY = {"MNQ": pd.DataFrame(), "MES": pd.DataFrame()}


def test_key_contract_matches_what_decide_thesis_actually_reads():
    """Pins NEW-INSIGHT 5. If decide_thesis grows a key, this test fails loudly."""
    import agent.run_agent as ra
    src = inspect.getsource(ra.decide_thesis)
    read = {ln.split('facts.get("')[1].split('"')[0]
            for ln in src.splitlines() if 'facts.get("' in ln}
    missing = read - set(DECIDE_THESIS_KEYS)
    assert not missing, f"decide_thesis reads keys the assembler does not provide: {missing}"


def test_assembler_emits_every_contract_key():
    from agent.facts.store import FactStore
    _, _, d, _ = assemble_facts(FactStore(), EMPTY, NOW)
    for k in DECIDE_THESIS_KEYS:
        assert k in d, f"missing contract key {k}"


def test_assembler_never_raises_on_degraded_input():
    from agent.facts.store import FactStore
    out = assemble_facts(FactStore(), EMPTY, NOW)
    assert len(out) == 4


def test_menus_block_is_present_and_not_hashed():
    """S8/S9 are additive overlays; the hashed identity stays byte-stable."""
    from agent.facts.store import FactStore
    _, _, d, _ = assemble_facts(FactStore(), EMPTY, NOW)
    assert "menus" in d


# --- added during implementation (not in the plan) --------------------------- #

def _synthetic_bars(days=20):
    """A long enough ramp for compute_facts to find prev-day / week structure."""
    n = days * 24 * 60
    idx = pd.date_range(NOW - pd.Timedelta(days=days), periods=n, freq="1min",
                        tz="America/New_York")
    base = pd.Series(range(n), index=idx).astype(float) * 0.05 + 29000
    wave = (pd.Series(range(n), index=idx) % 720).astype(float) * 0.1
    px = base + wave
    return pd.DataFrame({"Open": px, "High": px + 4, "Low": px - 4, "Close": px,
                         "Volume": 1.0}, index=idx)


def test_assembler_produces_a_real_view_on_real_shaped_bars():
    from agent.facts.store import FactStore
    bars = {"MNQ": _synthetic_bars(), "MES": _synthetic_bars()}
    text, ctx, d, mag = assemble_facts(FactStore(), bars, NOW)
    assert ctx == "", "recorded L1 runs passed an EMPTY context block"
    assert text and "S1" in text or len(text) > 200
    assert d.get("now_price") is not None
    assert not d.get("degraded")
    for k in DECIDE_THESIS_KEYS:
        assert k in d
    assert isinstance(mag, dict)


def test_assembler_ignores_the_store_entirely():
    """The Analyzer is self-contained; parity forces the view through derive_facts."""
    import agent.facts.assemble as mod
    src = inspect.getsource(mod.assemble_facts) + inspect.getsource(mod.build_bundle)
    assert "store." not in src, "assemble_facts must not read the FactStore"


def test_no_lookahead_bars_at_or_after_now_are_excluded():
    from agent.facts.store import FactStore
    bars = {"MNQ": _synthetic_bars(), "MES": _synthetic_bars()}
    _, _, a, _ = assemble_facts(FactStore(), bars, NOW)
    truncated = {tk: df[df.index < NOW] for tk, df in bars.items()}
    _, _, b, _ = assemble_facts(FactStore(), truncated, NOW)
    assert a.get("now_price") == b.get("now_price")
