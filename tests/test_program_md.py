# tests/test_program_md.py
# Structural pytest suite for program.md.
# Validates that all required sections, commands, and conventions are present
# so that a wrong grep command or schema change is caught immediately.
import pathlib

PROGRAM_MD = pathlib.Path(__file__).parent.parent / "program.md"


def _content():
    return PROGRAM_MD.read_text(encoding="utf-8")


def test_file_exists():
    assert PROGRAM_MD.exists(), "program.md must exist"


def test_title():
    assert "# autoresearch" in _content()


def test_setup_section():
    c = _content()
    assert "Setup" in c
    assert "autoresearch/" in c  # branch naming pattern


def test_verify_cache_instruction():
    c = _content()
    assert "~/.cache/autoresearch/stock_data" in c
    assert "prepare.py" in c  # tells agent to run prepare.py if cache missing


def test_results_tsv_header():
    c = _content()
    assert "commit\ttrain_pnl\ttest_pnl\ttrain_sharpe\ttotal_trades\twin_rate\tstatus\tdescription" in c


def test_grep_train_pnl_command():
    c = _content()
    assert 'grep "^train_total_pnl:" run.log' in c


def test_grep_total_trades_command():
    c = _content()
    assert 'grep "^train_total_trades:" run.log' in c


def test_output_block_format():
    """Output block must contain the 7 lines from print_results()."""
    c = _content()
    assert "sharpe:" in c
    assert "total_trades:" in c
    assert "win_rate:" in c
    assert "avg_pnl_per_trade:" in c
    assert "total_pnl:" in c
    assert "backtest_start:" in c
    assert "backtest_end:" in c


def test_higher_pnl_is_better():
    c = _content()
    # program.md must state that higher train_total_pnl is the keep criterion
    assert "train_total_pnl" in c
    assert "higher" in c.lower()
    # Must NOT say "lower is better" (val_bpb legacy)
    assert "lower is better" not in c


def test_cannot_modify_prepare():
    c = _content()
    assert "prepare.py" in c
    # Must have restriction on modifying prepare.py
    assert "CANNOT" in c or "cannot" in c.lower()


def test_cannot_modify_run_backtest():
    """Backtest loop is the evaluation harness — must be explicitly off-limits."""
    c = _content()
    assert "run_backtest" in c


def test_can_modify_screen_day():
    c = _content()
    assert "screen_day" in c


def test_can_modify_manage_position():
    c = _content()
    assert "manage_position" in c


def test_cannot_modify_backtest_window():
    c = _content()
    assert "BACKTEST_START" in c
    assert "BACKTEST_END" in c


def test_never_stop_instruction():
    c = _content()
    # NEVER STOP or equivalent
    assert "NEVER" in c or "never stop" in c.lower()


def test_results_tsv_not_committed():
    c = _content()
    assert "untracked" in c.lower() or "do not commit" in c.lower() or "NOT commit" in c


def test_run_command():
    c = _content()
    assert "uv run train.py > run.log 2>&1" in c


def test_git_reset_hard():
    c = _content()
    assert "git reset --hard HEAD~1" in c


def test_no_val_bpb_references():
    c = _content()
    assert "val_bpb" not in c, "Legacy val_bpb metric must not appear in new program.md"


def test_no_vram_references():
    c = _content()
    assert "vram" not in c.lower(), "Legacy VRAM references must not appear"


def test_no_five_minute_budget():
    c = _content()
    assert "5 minute" not in c.lower() and "5-minute" not in c.lower()


def test_no_trades_guidance():
    """Agent must have guidance for the 'zero trades' scenario."""
    c = _content()
    assert "no trade" in c.lower() or "0 trade" in c.lower() or "zero signal" in c.lower() or "no signal" in c.lower()


def test_status_values():
    c = _content()
    assert "keep" in c
    assert "discard" in c
    assert "crash" in c
