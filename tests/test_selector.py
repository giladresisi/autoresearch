"""tests/test_selector.py — LLM strategy selector tests.

Unit tests mock strategy_selector._call_claude so no real claude CLI calls are made.
The integration test (marked @pytest.mark.integration) makes a real call and verifies
that Claude Code returns a structurally valid, contextually reasonable response.
"""
import json
import pytest
import numpy as np
import pandas as pd
from datetime import date, timedelta
from unittest.mock import patch

import strategy_selector


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_recent_df(n: int = 35, base_price: float = 100.0) -> pd.DataFrame:
    """Synthetic net-upward DataFrame with enough history for all snapshot calculations.

    Uses alternating up/down closes (3 up : 1 down) so RSI14 has non-zero losses
    and returns valid (non-NaN) RSI values after the 14-bar warmup.
    """
    dates = [date(2026, 1, 2) + timedelta(days=i) for i in range(n)]
    # Build mixed series: +1 three times then -0.3 — net upward, non-monotone
    close = [base_price]
    for i in range(1, n):
        close.append(close[-1] + (1.0 if i % 4 != 0 else -0.3))
    close = np.array(close)
    return pd.DataFrame({
        "open":       close * 0.998,
        "high":       close * 1.005,
        "low":        close * 0.993,
        "close":      close,
        "volume":     np.full(n, 1_500_000.0),
        "price_10am": close * 0.999,
    }, index=pd.Index(dates, name="date"))


def _mock_claude(response_dict: dict):
    """Patch _call_claude to return response_dict serialised as JSON."""
    return patch(
        "strategy_selector._call_claude",
        return_value=json.dumps(response_dict),
    )


# ── _compute_ticker_snapshot ──────────────────────────────────────────────────

class TestComputeTickerSnapshot:
    def test_returns_dict_with_required_keys(self):
        df = make_recent_df()
        snap = strategy_selector._compute_ticker_snapshot(df)
        for key in ("current_price", "sma20", "rsi14", "atr14",
                    "vol_ratio_5d_30d", "pct_change_30d", "above_sma20"):
            assert key in snap, f"Missing key: {key}"

    def test_rsi_in_valid_range(self):
        df = make_recent_df()
        snap = strategy_selector._compute_ticker_snapshot(df)
        assert 0 <= snap["rsi14"] <= 100

    def test_short_df_returns_error_key(self):
        """DataFrames shorter than 14 bars should return {'error': ...} rather than crash."""
        df = make_recent_df(n=5)
        snap = strategy_selector._compute_ticker_snapshot(df)
        assert "error" in snap

    def test_above_sma20_true_for_rising_series(self):
        """A net-upward series should end above its own SMA20."""
        df = make_recent_df(n=35)
        snap = strategy_selector._compute_ticker_snapshot(df)
        assert snap["above_sma20"] is True

    def test_zero_volume_does_not_crash(self):
        """vol_ratio should be None (not ZeroDivisionError) when volume is all zeros."""
        df = make_recent_df()
        df["volume"] = 0.0
        snap = strategy_selector._compute_ticker_snapshot(df)
        assert snap["vol_ratio_5d_30d"] is None


# ── select_strategy (unit — _call_claude mocked) ──────────────────────────────

class TestSelectStrategy:
    def test_happy_path_returns_strategy(self):
        """When Claude returns a valid match, select_strategy surfaces it correctly."""
        with _mock_claude({
            "strategy": "energy-momentum-v1",
            "explanation": "Strong match: XOM is energy, trending above SMA50.",
            "confidence": "high",
        }):
            result = strategy_selector.select_strategy(
                "XOM", make_recent_df(), date(2026, 3, 1)
            )

        assert result["strategy"] == "energy-momentum-v1"
        assert result["confidence"] == "high"
        assert isinstance(result["explanation"], str) and len(result["explanation"]) > 0

    def test_no_match_returns_none_strategy(self):
        """When Claude finds no suitable strategy, select_strategy returns strategy=None."""
        with _mock_claude({
            "strategy": None,
            "explanation": "GS is a financial stock — no registered strategy matches.",
            "confidence": "low",
        }):
            result = strategy_selector.select_strategy(
                "GS", make_recent_df(), date(2026, 3, 1)
            )

        assert result["strategy"] is None
        assert result["confidence"] == "low"

    def test_malformed_json_fallback(self):
        """Non-JSON response should not crash — return strategy=None with raw text as explanation."""
        with patch("strategy_selector._call_claude",
                   return_value="I cannot determine a strategy at this time."):
            result = strategy_selector.select_strategy(
                "AAPL", make_recent_df(), date(2026, 3, 1)
            )

        assert result["strategy"] is None
        assert result["confidence"] == "low"
        assert "cannot determine" in result["explanation"]

    def test_returns_dict_with_all_three_keys(self):
        """Return dict must always contain strategy, explanation, and confidence."""
        with _mock_claude({
            "strategy": "energy-momentum-v1",
            "explanation": "Match.",
            "confidence": "medium",
        }):
            result = strategy_selector.select_strategy(
                "DVN", make_recent_df(), date(2026, 3, 1)
            )

        assert set(result.keys()) == {"strategy", "explanation", "confidence"}

    def test_empty_string_strategy_normalized_to_none(self):
        """Empty-string strategy from Claude should be normalized to None."""
        with _mock_claude({
            "strategy": "",
            "explanation": "No match.",
            "confidence": "low",
        }):
            result = strategy_selector.select_strategy(
                "TSLA", make_recent_df(), date(2026, 3, 1)
            )
        assert result["strategy"] is None

    def test_missing_explanation_defaults_to_empty_string(self):
        """Missing explanation key should yield '' not KeyError."""
        with _mock_claude({"strategy": None, "confidence": "low"}):
            result = strategy_selector.select_strategy(
                "SPY", make_recent_df(), date(2026, 3, 1)
            )
        assert result["explanation"] == ""

    def test_missing_confidence_defaults_to_low(self):
        """Missing confidence key should yield 'low' not KeyError."""
        with _mock_claude({"strategy": None, "explanation": "No match."}):
            result = strategy_selector.select_strategy(
                "SPY", make_recent_df(), date(2026, 3, 1)
            )
        assert result["confidence"] == "low"

    def test_claudecode_stripped_from_subprocess_env(self):
        """CLAUDECODE must not be passed to the claude subprocess."""
        import os, subprocess
        captured_env = {}

        def fake_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            m = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
            m.stdout = json.dumps({"strategy": None, "explanation": "", "confidence": "low"})
            return m

        original_claudecode = os.environ.get("CLAUDECODE")
        os.environ["CLAUDECODE"] = "1"
        try:
            with patch("subprocess.run", side_effect=fake_run):
                strategy_selector.select_strategy("SPY", make_recent_df(), date(2026, 3, 1))
        finally:
            if original_claudecode is None:
                os.environ.pop("CLAUDECODE", None)
            else:
                os.environ["CLAUDECODE"] = original_claudecode

        assert "CLAUDECODE" not in captured_env


# ── scripts/extract_strategy — boundary detection ─────────────────────────────

class TestExtractStrategyBoundary:
    def test_boundary_constant_matches_train_py(self):
        """The BOUNDARY phrase in extract_strategy.py must appear in the current train.py."""
        from scripts.extract_strategy import BOUNDARY

        train_py = (
            __import__("pathlib").Path(__file__).parent.parent / "train.py"
        ).read_text(encoding="utf-8")
        assert BOUNDARY in train_py, (
            f"BOUNDARY phrase '{BOUNDARY}' not found in train.py — "
            "extraction will fail on real commits"
        )

    def test_extract_splits_correctly(self, tmp_path):
        """extract() should write only lines above the boundary."""
        from scripts.extract_strategy import BOUNDARY
        from unittest.mock import MagicMock, patch

        fake_content = (
            "def screen_day(): pass\n"
            f"{BOUNDARY}\n"
            "def run_backtest(): pass\n"
        )

        mock_result = MagicMock()
        mock_result.stdout = fake_content

        out_dir = tmp_path / "strategies"
        out_dir.mkdir()

        with patch("scripts.extract_strategy.STRATEGIES_DIR", out_dir), \
             patch("subprocess.run", return_value=mock_result):
            from scripts.extract_strategy import extract
            extract("fake-tag", "test_strategy")

        out_file = out_dir / "test_strategy.py"
        assert out_file.exists()
        content = out_file.read_text(encoding="utf-8")
        assert "screen_day" in content
        assert "run_backtest" not in content


# ── Integration test (real claude CLI call) ───────────────────────────────────

@pytest.mark.integration
def test_select_strategy_real_claude_code():
    """
    Call the real claude CLI and verify the response is structurally valid
    and contextually reasonable for an energy ticker (XOM).

    XOM is a core energy stock — the only registered strategy is energy-momentum-v1.
    Claude should either select it or return None; both are acceptable.
    The response must be well-formed JSON with the three required keys.
    """
    result = strategy_selector.select_strategy("XOM", make_recent_df(), date(2026, 3, 21))

    # Structural validity
    assert set(result.keys()) == {"strategy", "explanation", "confidence"}, \
        f"Unexpected keys: {result.keys()}"
    assert result["confidence"] in ("high", "medium", "low"), \
        f"Invalid confidence: {result['confidence']}"
    assert isinstance(result["explanation"], str) and len(result["explanation"]) > 0, \
        "explanation must be a non-empty string"

    # Strategy must be a registered name or None
    valid_strategies = set(strategy_selector.REGISTRY.keys()) | {None}
    assert result["strategy"] in valid_strategies, \
        f"strategy '{result['strategy']}' not in REGISTRY and not None"
