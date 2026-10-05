# tests/test_r2_sync.py
# scripts/r2_sync.py against an in-memory fake S3 client: no network, no moto, tmp dirs only.
# ACT_GLOBAL_DIR / ACT_THESIS_CACHE_DIR always point at tmp; the real <global> is never touched.
from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import r2_sync
from scripts.r2_sync import R2Config, R2Error

CFG = R2Config("acct", "key-id", "secret", "bkt")


class _NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_on_upload_n: int | None = None
        self.corrupt_keys: set[str] = set()
        self._uploads = 0

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        self._uploads += 1
        if self.fail_on_upload_n == self._uploads:
            raise ConnectionError("boom")
        self.calls.append(("upload", Key))
        self.objects[Key] = Path(Filename).read_bytes()

    def put_object(self, Bucket, Key, Body, **kw):
        self.calls.append(("put", Key))
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        self.calls.append(("get", Key))
        if Key not in self.objects:
            raise _NoSuchKey()
        return {"Body": io.BytesIO(self.objects[Key])}

    def download_file(self, Bucket, Key, Filename):
        self.calls.append(("download", Key))
        data = self.objects[Key]
        if Key in self.corrupt_keys:
            data = data[:-1] + b"X"
        Path(Filename).write_bytes(data)

    def uploads(self):
        return [k for op, k in self.calls if op == "upload"]


class DeadClient:
    def __getattr__(self, name):
        raise AssertionError(f"network call attempted: {name}")


def write_pq(path: Path, n: int, start: str = "2026-10-01 13:30", close: float = 1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    pd.DataFrame({"Close": close}, index=idx).to_parquet(path)


@pytest.fixture
def machines(tmp_path, monkeypatch):
    """use('a'|'b') switches the process to that machine's <global>; returns the root."""
    monkeypatch.setattr(r2_sync, "_orchestrator_pid", lambda: None)
    monkeypatch.setattr(r2_sync, "REPO_ROOT", tmp_path / "repo_a")
    (tmp_path / "repo_a").mkdir()
    monkeypatch.setenv("MNQ_CONID", "111")
    monkeypatch.setenv("MES_CONID", "222")

    def use(name):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        monkeypatch.setenv("ACT_GLOBAL_DIR", str(root))
        monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(root / "thesis_cache"))
        return root

    return use


def populate(root: Path):
    main = root / "general" / "main"
    live = root / "general" / "live"
    write_pq(main / "MNQ_1m.parquet", 10)
    write_pq(main / "2026-09" / "MNQ_1m.parquet", 4)
    (main / "rollover_ledger.json").write_text("[]")
    write_pq(live / "MNQ_1m.parquet", 10)
    write_pq(live / "MES_1s.parquet", 10)
    (live / "global.json").write_text('{"ath": 1}')
    (live / "pending_smts.json").write_text('{"entries": []}')
    # never uploaded
    (live / "last_tick.json").write_text("{}")
    (live / "orchestrator.pid").write_text("1")
    (live / "MNQ_1m.parquet.bak").write_bytes(b"x")
    (main / "MNQ_1m.preroll.bak").write_bytes(b"x")
    (main / "MNQ_1s.parquet.promote.tmp").write_bytes(b"x")
    (main / "x.tmp").write_bytes(b"x")
    (live / "MNQ_1s_gaps_preview.parquet").write_bytes(b"x")
    sess = root / "sessions"
    (sess / "2026-10-01").mkdir(parents=True)
    (sess / "2026-10-01" / "events.jsonl").write_text("a")
    (sess / "2026-10-02").mkdir(parents=True)
    (sess / "2026-10-02" / "events.jsonl").write_text("b")
    th = root / "thesis_cache"
    th.mkdir()
    (th / "k1.json").write_text("{}")
    (root / "studies").mkdir()
    (root / "studies" / "s.txt").write_text("study")


def publish(client, **kw):
    return r2_sync.publish(client=client, cfg=CFG, **kw)


def sync(client, **kw):
    return r2_sync.sync(client=client, cfg=CFG, **kw)


def sha(p: Path) -> str:
    return r2_sync._sha256(p)


# --------------------------------------------------------------------------- happy path

def test_publish_uploads_changed_files_then_manifest_last(machines):
    root = machines("a")
    populate(root)
    c = FakeS3()
    rep = publish(c)
    assert rep["ok"] and rep["uploaded"] > 0
    for g in ("main", "live", "sessions", "thesis_cache"):
        puts = [i for i, (op, k) in enumerate(c.calls) if op == "put" and k == f"manifest/{g}.json"]
        assert len(puts) == 1
        prefix = r2_sync._groups()[g].prefix
        ups = [i for i, (op, k) in enumerate(c.calls) if op == "upload" and k.startswith(prefix)]
        assert ups and max(ups) < puts[0]


def test_publish_second_run_uploads_nothing(machines):
    root = machines("a")
    populate(root)
    c = FakeS3()
    publish(c)
    n = len(c.calls)
    rep = publish(c)
    assert rep["uploaded"] == 0
    assert not [x for x in c.calls[n:] if x[0] in ("upload", "put")]


def test_sync_downloads_into_empty_dir_and_hashes_match(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c)
    b = machines("b")
    rep = sync(c)
    assert rep["ok"] and rep["pulled_parquets"]
    for rel in ("general/main/MNQ_1m.parquet", "general/main/2026-09/MNQ_1m.parquet",
                "general/main/rollover_ledger.json", "general/live/MNQ_1m.parquet",
                "general/live/MES_1s.parquet", "general/live/global.json",
                "general/live/pending_smts.json", "sessions/2026-10-01/events.jsonl",
                "thesis_cache/k1.json"):
        assert sha(b / rel) == sha(a / rel), rel


def test_exclusions_never_uploaded(machines):
    populate(machines("a"))
    c = FakeS3()
    publish(c)
    bad = ("last_tick.json", ".pid", ".bak", ".tmp", "gaps_preview")
    assert not [k for k in c.objects if any(b in k for b in bad)]
    assert "general/live/pending_smts.json" in c.objects      # the cross-session SMT carry travels


def test_default_sync_groups_exclude_logs_and_studies(machines, tmp_path):
    assert "logs" not in r2_sync.DEFAULT_SYNC_GROUPS and "studies" not in r2_sync.DEFAULT_SYNC_GROUPS
    assert "logs" in r2_sync.DEFAULT_PUBLISH_GROUPS and "studies" not in r2_sync.DEFAULT_PUBLISH_GROUPS
    a = machines("a")
    populate(a)
    (tmp_path / "repo_a" / "orchestrator_stdout.log").write_text("log")
    c = FakeS3()
    publish(c)
    assert any(k.startswith("logs/") for k in c.objects)
    assert not any(k.startswith("studies/") for k in c.objects)
    publish(c, only="studies")
    assert "studies/s.txt" in c.objects
    machines("b")
    sync(c)
    assert not (tmp_path / "b" / "studies").exists()


def test_manifest_records_git_sha_and_conids(machines):
    populate(machines("a"))
    c = FakeS3()
    publish(c)
    m = json.loads(c.objects["manifest/main.json"])
    assert {"git_sha", "MNQ_CONID", "MES_CONID", "source_host", "published_at"} <= set(m)
    assert m["MNQ_CONID"] == "111" and m["MES_CONID"] == "222"
    assert m["files"]["MNQ_1m.parquet"]["last_ts"]


def test_sync_warns_on_conid_mismatch(machines, monkeypatch):
    populate(machines("a"))
    c = FakeS3()
    publish(c)
    machines("b")
    monkeypatch.setenv("MNQ_CONID", "999")
    rep = sync(c)
    assert rep["ok"]
    assert any("MNQ_CONID" in w for w in rep["warnings"])


def test_sync_warns_on_git_sha_not_in_local_history(machines, monkeypatch):
    populate(machines("a"))
    monkeypatch.setattr(r2_sync, "_git_sha", lambda: "a" * 40)
    c = FakeS3()
    publish(c)
    machines("b")
    monkeypatch.setattr(r2_sync, "_git_sha", lambda: "b" * 40)
    monkeypatch.setattr(r2_sync, "_git_contains", lambda sha: False)
    rep = sync(c)
    assert rep["ok"] and any("not in this checkout" in w for w in rep["warnings"])


# --------------------------------------------------------------------------- baton rules

def _two_machines_synced(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c)
    b = machines("b")
    sync(c)
    return a, b, c


def test_sync_pulls_when_local_untouched_and_remote_moved(machines):
    a, b, c = _two_machines_synced(machines)
    machines("a")
    (a / "general" / "live" / "global.json").write_text('{"ath": 2}')
    publish(c)
    machines("b")
    rep = sync(c)
    assert rep["ok"]
    assert (b / "general" / "live" / "global.json").read_text() == '{"ath": 2}'


def test_sync_overrides_truncated_local_parquet_and_keeps_r2prev(machines):
    a, b, c = _two_machines_synced(machines)
    f = b / "general" / "main" / "MNQ_1m.parquet"
    write_pq(f, 3)
    truncated = f.read_bytes()
    rep = sync(c)
    assert rep["ok"]
    assert sha(f) == sha(a / "general" / "main" / "MNQ_1m.parquet")
    assert f.with_name(f.name + ".r2prev").read_bytes() == truncated


def test_sync_missing_local_parquet_is_downloaded(machines):
    a, b, c = _two_machines_synced(machines)
    f = b / "general" / "main" / "MNQ_1m.parquet"
    f.unlink()
    sync(c)
    assert sha(f) == sha(a / "general" / "main" / "MNQ_1m.parquet")


def test_sync_local_parquet_with_extra_row_is_left_alone(machines):
    a, b, c = _two_machines_synced(machines)
    f = b / "general" / "main" / "MNQ_1m.parquet"
    write_pq(f, 12)
    before = f.read_bytes()
    rep = sync(c)
    assert rep["ok"] and f.read_bytes() == before
    assert any(x["path"] == "MNQ_1m.parquet" for x in rep["ahead"])


def test_sync_leaves_live_parquet_alone_when_local_ahead(machines):
    """The analysing machine ran the live: its live parquet is ahead of R2."""
    a, b, c = _two_machines_synced(machines)
    f = b / "general" / "live" / "MNQ_1m.parquet"
    write_pq(f, 25)
    before = sha(f)
    rep = sync(c)
    assert rep["ok"] and sha(f) == before
    assert any(x["group"] == "live" for x in rep["ahead"])


def test_sync_conflict_skips_file_reports_and_exits_nonzero(machines):
    a, b, c = _two_machines_synced(machines)
    machines("a")
    write_pq(a / "general" / "live" / "MNQ_1m.parquet", 11, close=5.0)
    publish(c)
    machines("b")
    f = b / "general" / "live" / "MNQ_1m.parquet"
    write_pq(f, 13, close=7.0)
    before = f.read_bytes()
    rep = sync(c)
    assert not rep["ok"] and f.read_bytes() == before
    assert rep["conflicts"][0]["local_last_ts"] and rep["conflicts"][0]["remote_last_ts"]


def test_sync_force_remote_wins(machines):
    a, b, c = _two_machines_synced(machines)
    machines("a")
    write_pq(a / "general" / "live" / "MNQ_1m.parquet", 11, close=5.0)
    publish(c)
    machines("b")
    f = b / "general" / "live" / "MNQ_1m.parquet"
    write_pq(f, 13, close=7.0)
    rep = sync(c, force=True)
    assert rep["ok"] and sha(f) == sha(a / "general" / "live" / "MNQ_1m.parquet")


def _publish_conflict_setup(machines):
    a, b, c = _two_machines_synced(machines)
    machines("a")
    write_pq(a / "general" / "live" / "MNQ_1m.parquet", 11, close=5.0)
    publish(c)
    machines("b")
    f = b / "general" / "live" / "MNQ_1m.parquet"
    write_pq(f, 13, close=7.0)
    return a, b, c, f


def test_publish_conflict_skips_file_and_exits_nonzero(machines):
    a, b, c, f = _publish_conflict_setup(machines)
    remote_before = c.objects["general/live/MNQ_1m.parquet"]
    rep = publish(c, only="live")
    assert not rep["ok"] and rep["conflicts"]
    assert c.objects["general/live/MNQ_1m.parquet"] == remote_before


def test_publish_force_local_wins(machines):
    a, b, c, f = _publish_conflict_setup(machines)
    rep = publish(c, only="live", force=True)
    assert rep["ok"] and c.objects["general/live/MNQ_1m.parquet"] == f.read_bytes()


def test_publish_refuses_to_clobber_remote_that_is_ahead(machines):
    a, b, c = _two_machines_synced(machines)
    write_pq(b / "general" / "main" / "MNQ_1m.parquet", 3)
    rep = publish(c, only="main")
    assert not rep["ok"] and rep["conflicts"][0]["kind"] == "remote_ahead"


def test_first_sync_local_parquet_newer_than_remote_is_kept(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c)
    b = machines("b")
    f = b / "general" / "live" / "MNQ_1m.parquet"
    write_pq(f, 30)
    before = f.read_bytes()
    sync(c)
    assert f.read_bytes() == before


def test_first_sync_remote_newer_overwrites(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c)
    b = machines("b")
    f = b / "general" / "live" / "MNQ_1m.parquet"
    write_pq(f, 4)
    sync(c)
    assert sha(f) == sha(a / "general" / "live" / "MNQ_1m.parquet")


def test_state_file_updated_only_after_verified_download_or_successful_upload(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    c.fail_on_upload_n = 2
    rep = publish(c, only="main")
    assert not rep["ok"]
    assert "manifest/main.json" not in c.objects
    state = json.loads((a / ".r2_state.json").read_text())
    assert not [k for k in state if k.startswith("general/main/")]     # nothing recorded
    c.fail_on_upload_n = None
    publish(c, only="main")
    state = json.loads((a / ".r2_state.json").read_text())
    assert any(k.startswith("general/main/") for k in state)
    # download side: a corrupt download must not be recorded
    b = machines("b")
    c.corrupt_keys = {"general/main/rollover_ledger.json"}
    rep = sync(c, only="main")
    state_b = json.loads((b / ".r2_state.json").read_text())
    assert "general/main/rollover_ledger.json" not in state_b
    assert "general/main/MNQ_1m.parquet" in state_b


def test_thesis_cache_group_follows_ACT_THESIS_CACHE_DIR(machines, tmp_path, monkeypatch):
    machines("a")
    custom = tmp_path / "elsewhere"
    custom.mkdir()
    (custom / "rec.json").write_text("{}")
    monkeypatch.setenv("ACT_THESIS_CACHE_DIR", str(custom))
    c = FakeS3()
    publish(c, only="thesis_cache")
    assert "thesis_cache/rec.json" in c.objects


# --------------------------------------------------------------------------- error paths

def test_publish_dies_midway_keeps_old_manifest(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c, only="main")
    old = c.objects["manifest/main.json"]
    write_pq(a / "general" / "main" / "MNQ_1m.parquet", 20)
    (a / "general" / "main" / "rollover_ledger.json").write_text('["x"]')
    c._uploads = 0
    c.fail_on_upload_n = 2
    rep = publish(c, only="main")
    assert not rep["ok"]
    assert c.objects["manifest/main.json"] == old


def test_sync_rejects_corrupt_download(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c)
    b = machines("b")
    c.corrupt_keys = {"general/live/global.json"}
    rep = sync(c)
    assert not rep["ok"] and rep["errors"]
    dest = b / "general" / "live" / "global.json"
    assert not dest.exists() and not dest.with_name("global.json.r2tmp").exists()


def test_sync_never_deletes_without_prune(machines):
    a, b, c = _two_machines_synced(machines)
    extra = b / "sessions" / "2026-09-30" / "old.txt"
    extra.parent.mkdir(parents=True)
    extra.write_text("keep")
    sync(c)
    assert extra.exists()


def test_sync_prune_deletes_only_group_files(machines):
    a, b, c = _two_machines_synced(machines)
    extra = b / "sessions" / "2026-09-30" / "old.txt"
    extra.parent.mkdir(parents=True)
    extra.write_text("x")
    outside = b / "general" / "live" / "orchestrator.pid"     # not a live-group file
    outside.write_text("1")
    rep = sync(c, prune=True)
    assert not extra.exists() and outside.exists()
    assert "sessions/2026-09-30/old.txt" in rep["pruned"]


def test_publish_and_sync_refused_while_orchestrator_running(machines, monkeypatch):
    a = machines("a")
    populate(a)
    monkeypatch.setattr(r2_sync, "_orchestrator_pid", lambda: 4242)
    c = FakeS3()
    p = publish(c)
    s = sync(c)
    assert not p["ok"] and not s["ok"]
    assert "4242" in p["errors"][0] and "4242" in s["errors"][0]
    assert c.calls == []


# --------------------------------------------------------------------------- validation

@pytest.mark.parametrize("missing", r2_sync.R2_VARS)
def test_missing_r2_env_names_the_variable(missing, monkeypatch):
    env = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
           "R2_BUCKET": "b"}
    env.pop(missing)
    with pytest.raises(R2Error, match=missing):
        r2_sync.load_config(env)


def test_missing_r2_env_makes_no_network_call(machines, monkeypatch):
    machines("a")
    for v in r2_sync.R2_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("R2_BUCKET", "b")
    monkeypatch.setattr(r2_sync, "make_client", lambda cfg: pytest.fail("client built"))
    with pytest.raises(R2Error, match="R2_ACCOUNT_ID"):
        r2_sync.publish()


def test_bucket_must_be_a_name_not_a_url():
    env = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
           "R2_BUCKET": "https://x.r2.cloudflarestorage.com/bkt"}
    with pytest.raises(R2Error, match="NAME"):
        r2_sync.load_config(env)


def test_dry_run_makes_no_network_calls_and_writes_nothing(machines):
    a = machines("a")
    populate(a)
    rep = r2_sync.publish(dry_run=True, client=DeadClient())
    assert rep["ok"] and rep["groups"]["main"]["files"] == 3
    assert not (a / ".r2_state.json").exists() and not (a / ".r2_hash_cache.json").exists()
    assert not any("last_tick" in f or f.endswith((".bak", ".tmp"))
                   for g in rep["groups"].values() for f in g["file_list"])


def test_sync_dry_run_downloads_nothing(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c)
    b = machines("b")
    rep = sync(c, dry_run=True)
    assert rep["downloaded"] > 0 and not (b / "general" / "main" / "MNQ_1m.parquet").exists()
    assert not (b / ".r2_state.json").exists()


def test_date_selector_limits_sessions_group(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c, only="sessions", date="2026-10-01")
    assert c.uploads() == ["sessions/2026-10-01/events.jsonl"]
    publish(c, only="sessions", date="2026-10-02")
    m = json.loads(c.objects["manifest/sessions.json"])
    assert set(m["files"]) == {"2026-10-01/events.jsonl", "2026-10-02/events.jsonl"}
    b = machines("b")
    sync(c, only="sessions", date="2026-10-02")
    assert (b / "sessions" / "2026-10-02" / "events.jsonl").exists()
    assert not (b / "sessions" / "2026-10-01").exists()


def test_publish_after_promote_uses_default_groups_and_unconfigured_is_quiet(
        machines, monkeypatch, capsys):
    machines("a")
    for v in r2_sync.R2_VARS:
        monkeypatch.delenv(v, raising=False)
    res = r2_sync.publish_after_promote()
    assert res == {"configured": False, "success": True, "error": None, "report": None}
    assert "R2 not configured" in capsys.readouterr().out
    seen = {}
    monkeypatch.setattr(r2_sync, "publish", lambda **kw: seen.update(kw) or
                        {"ok": True, "groups": {}, "errors": [], "conflicts": []})
    r2_sync.publish_after_promote(client=object(), cfg=CFG)
    assert "only" not in seen            # None -> DEFAULT_PUBLISH_GROUPS


def test_publish_after_promote_reports_failure_without_raising(machines):
    machines("a")
    c = FakeS3()
    c.fail_on_upload_n = 1
    populate(machines("a"))
    res = r2_sync.publish_after_promote(client=c, cfg=CFG, stream=io.StringIO())
    assert res["success"] is False and res["error"]


# --------------------------------------------------------------------------- review fixes

def test_publish_with_no_state_and_differing_remote_nonparquet_is_a_conflict(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c)
    b = machines("b")                       # fresh machine, no state, its own global.json
    (b / "general" / "live").mkdir(parents=True)
    (b / "general" / "live" / "global.json").write_text('{"ath": 99}')
    before = c.objects["general/live/global.json"]
    rep = publish(c, only="live")
    assert not rep["ok"] and rep["conflicts"]
    assert c.objects["general/live/global.json"] == before


def test_sync_replacing_nonparquet_with_no_state_keeps_r2prev(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c)
    b = machines("b")
    f = b / "sessions" / "2026-10-01" / "events.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("local-only")
    sync(c)
    assert f.read_text() == "a"
    assert f.with_name("events.jsonl.r2prev").read_text() == "local-only"


def test_sync_rejects_path_traversal_in_manifest(machines):
    a = machines("a")
    populate(a)
    c = FakeS3()
    publish(c, only="live")
    m = json.loads(c.objects["manifest/live.json"])
    m["files"]["../evil.txt"] = m["files"]["global.json"]
    c.objects["manifest/live.json"] = json.dumps(m).encode()
    b = machines("b")
    rep = sync(c, only="live")
    assert not rep["ok"] and not (b / "general" / "evil.txt").exists()
