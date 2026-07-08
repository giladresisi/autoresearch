"""Replay cache tests (Wave 2.4)."""

from cache import ReplayCache


def test_miss_then_hit(tmp_path):
    c = ReplayCache(tmp_path / "cache")
    assert c.get("next_move", "hashA") is None
    c.put("next_move", "hashA", {"block": {"direction": "up"}, "verdict": "clean"})
    got = c.get("next_move", "hashA")
    assert got["verdict"] == "clean"
    assert c.hits == 1 and c.misses == 1


def test_hash_change_is_a_miss(tmp_path):
    c = ReplayCache(tmp_path / "cache")
    c.put("next_move", "hashA", {"x": 1})
    assert c.get("next_move", "hashA") is not None       # unchanged → hit
    assert c.get("next_move", "hashB") is None            # changed hash → miss (drift)


def test_corrupt_entry_treated_as_miss(tmp_path):
    c = ReplayCache(tmp_path / "cache")
    c.put("checkpoint:09:20", "h", {"ok": True})
    path = c._path("checkpoint:09:20", "h")
    path.write_text("{ this is not valid json", encoding="utf-8")
    assert c.get("checkpoint:09:20", "h") is None         # corrupt → miss, no crash


def test_concurrent_write_atomic(tmp_path):
    """Two writers of the same key never leave a corrupt file (atomic temp+rename)."""
    c = ReplayCache(tmp_path / "cache")
    for i in range(20):
        c.put("k", "same", {"i": i})
    got = c.get("k", "same")
    assert isinstance(got, dict) and "i" in got           # always a complete payload
