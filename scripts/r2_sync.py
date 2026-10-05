"""R2 publish / sync of the machine-global data folder (plan 50).

Machine A (the one that ran the live) publishes its finished run to a private Cloudflare R2
bucket; any other machine pulls it with `sync`. Entry points: `trade.py publish`,
`trade.py sync`, and `publish_after_promote()` (called after `trade.py promote`,
`trade.py rollover-prep` and the parquet-check session-end promote).

Shape (see .agents/plans/50.r2-publish-sync.md for the decisions):
  * groups = bucket prefixes, each with `manifest/<group>.json` written LAST, so a reader that
    sees a new manifest sees a complete group and a publish that dies midway leaves the old one.
  * `<global>/.r2_state.json` records, per object key, the sha256 this machine last synced
    from / published to R2 (the "baton"): it stops either side clobbering the other's data.
  * parquets are append-only time series: ahead/behind is decided by the LAST INDEX TIMESTAMP
    (kept in the manifest), not by "changed since last sync".

Nothing under agent/ or automation/ imports this module. boto3 is imported lazily.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

R2_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")

#: Files never uploaded, in any group. `last_tick.json` is run-in-progress state (rewritten
#: every second, only trusted while <= 10 s old). `pending_smts.json` is NOT excluded: it is the
#: deliberate cross-session SMT carry (session_pipeline._ingest_pending_smts) and lives in the
#: `live` group so the next machine to run the live starts with the same carry.
EXCLUDE_SUFFIXES = (".bak", ".tmp", ".pid", ".r2tmp", ".r2prev")
EXCLUDE_NAMES = frozenset({"last_tick.json", "MNQ_1s_gaps_preview.parquet"})

LIVE_FILES = frozenset({
    "MNQ_1s.parquet", "MES_1s.parquet", "MNQ_1m.parquet", "MES_1m.parquet",
    "global.json", "pending_smts.json",
})
LOG_FILES = ("orchestrator_stdout.log", "orchestrator_stderr.log", "orchestrator_crash.log")
# The worktree logs are append-only across many runs (a 2026-09 crash loop left a 945 MB stderr);
# a run's story is in the tail, so a log larger than this is published as its last LOG_TAIL_BYTES.
LOG_TAIL_BYTES = 5 * 1024 * 1024

DEFAULT_PUBLISH_GROUPS = ("main", "live", "sessions", "thesis_cache", "logs")
DEFAULT_SYNC_GROUPS = ("main", "live", "sessions", "thesis_cache")
ALL_GROUPS = DEFAULT_PUBLISH_GROUPS + ("studies",)


class R2Error(Exception):
    """Configuration / transport failure (not a per-file conflict)."""


# ---------------------------------------------------------------------------------------
# configuration + client
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def is_configured(env=None) -> bool:
    """True when ANY R2 variable is set (a partial setup is an error, not 'unconfigured')."""
    env = os.environ if env is None else env
    return any((env.get(v) or "").strip() for v in R2_VARS)


def load_config(env=None) -> R2Config:
    env = os.environ if env is None else env
    vals = {v: (env.get(v) or "").strip() for v in R2_VARS}
    for v in R2_VARS:
        if not vals[v]:
            raise R2Error(f"{v} is not set (R2 needs {', '.join(R2_VARS)})")
    bucket = vals["R2_BUCKET"]
    if "://" in bucket or "/" in bucket:
        raise R2Error("R2_BUCKET must be the bucket NAME, not a URL or path")
    return R2Config(vals["R2_ACCOUNT_ID"], vals["R2_ACCESS_KEY_ID"],
                    vals["R2_SECRET_ACCESS_KEY"], bucket)


def make_client(cfg: R2Config):
    try:
        import boto3  # lazy: nothing else needs it
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise R2Error("boto3 is not installed (uv sync / pip install 'boto3>=1.34,<2')") from exc
    return boto3.client(
        "s3", endpoint_url=cfg.endpoint, aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key, region_name="auto",
    )


def _orchestrator_pid():
    import trade
    return trade._orchestrator_pid()


# ---------------------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------------------

def _is_excluded(p: Path) -> bool:
    return p.name in EXCLUDE_NAMES or p.name.endswith(EXCLUDE_SUFFIXES)


def _walk(root: Path, only: Callable[[Path], bool] | None = None) -> dict[str, Path]:
    """POSIX-style relative path -> file, for every non-excluded file under root."""
    out: dict[str, Path] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file() or _is_excluded(p):
            continue
        if only is not None and not only(p):
            continue
        out[p.relative_to(root).as_posix()] = p
    return out


@dataclass
class Group:
    name: str
    prefix: str                       # bucket prefix, e.g. "general/main/"
    scan: Callable[[], dict[str, Path]]
    parquet_ts: bool = True


def _groups(publish_date: str | None = None) -> dict[str, Group]:
    import paths

    def _main():
        return _walk(paths.general_main_dir())

    def _live():
        return _walk(paths.general_live_dir(), lambda p: p.name in LIVE_FILES)

    def _sessions():
        return _walk(paths.sessions_dir())

    def _thesis():
        from agent.trader.thesis_cache import cache_root
        return _walk(cache_root())

    def _studies():
        return _walk(paths.global_root() / "studies")

    def _logs():
        from zoneinfo import ZoneInfo
        day = publish_date or _dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        out: dict[str, Path] = {}
        for n in LOG_FILES:
            src = REPO_ROOT / n
            if not src.is_file():
                continue
            if src.stat().st_size > LOG_TAIL_BYTES:
                tail_dir = paths.global_root() / ".r2_log_tails"
                tail_dir.mkdir(parents=True, exist_ok=True)
                tail = tail_dir / n
                with open(src, "rb") as f_in:
                    f_in.seek(-LOG_TAIL_BYTES, os.SEEK_END)
                    data = f_in.read()
                tail.write_bytes(data[data.find(b"\n") + 1:])  # drop the cut first line
                src = tail
            out[f"{day}/{n}"] = src
        return out

    return {
        "main": Group("main", "general/main/", _main),
        "live": Group("live", "general/live/", _live),
        "sessions": Group("sessions", "sessions/", _sessions),
        "thesis_cache": Group("thesis_cache", "thesis_cache/", _thesis),
        "logs": Group("logs", "logs/", _logs),
        "studies": Group("studies", "studies/", _studies),
    }


def parse_only(only: str | Iterable[str] | None, default: tuple[str, ...]) -> list[str]:
    if only is None:
        return list(default)
    names = [s.strip() for s in (only.split(",") if isinstance(only, str) else only) if s.strip()]
    bad = [n for n in names if n not in ALL_GROUPS]
    if bad:
        raise R2Error(f"unknown group(s): {', '.join(bad)} (valid: {', '.join(ALL_GROUPS)})")
    return names


# ---------------------------------------------------------------------------------------
# hashing, parquet timestamps, hash cache, state
# ---------------------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parquet_last_ts(path: Path) -> str | None:
    """Last index timestamp (ISO, UTC) of a parquet, reading only the index column; None when
    it cannot be determined (the baton rule then falls back to the state-hash rules)."""
    try:
        import pandas as pd
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(path)
        meta = pf.schema_arrow.pandas_metadata or {}
        idx = [c for c in meta.get("index_columns", []) if isinstance(c, str)]
        names = set(pf.schema_arrow.names)
        col = idx[0] if idx else next(
            (c for c in ("timestamp", "time", "ts", "datetime", "date") if c in names), None)
        if col is None:
            return None
        s = pq.read_table(path, columns=[col]).column(0).to_pandas()
        if s.empty:
            return None
        t = pd.Timestamp(s.max())
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        return t.isoformat()
    except Exception:
        return None


def _parquet_rows(path: Path) -> int | None:
    try:
        import pyarrow.parquet as pq
        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        return None


def _ts(s: str | None):
    if not s:
        return None
    import pandas as pd
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _json_load(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _json_save(p: Path, d: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


class _Local:
    """Hash + last_ts of local files, cached on disk by (path, size, mtime_ns)."""

    def __init__(self, global_root: Path, persist: bool = True) -> None:
        self.path = global_root / ".r2_hash_cache.json"
        self.persist = persist
        self.cache = _json_load(self.path)
        self.dirty = False

    def info(self, p: Path, want_ts: bool) -> dict:
        st = p.stat()
        key = str(p)
        e = self.cache.get(key)
        if not (e and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns):
            e = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": _sha256(p)}
            self.cache[key] = e
            self.dirty = True
        if want_ts and p.suffix == ".parquet" and "last_ts" not in e:
            e["last_ts"] = _parquet_last_ts(p)
            self.dirty = True
        out = {"size": e["size"], "sha256": e["sha256"]}
        if e.get("last_ts"):
            out["last_ts"] = e["last_ts"]
        return out

    def save(self) -> None:
        if self.persist and self.dirty:
            _json_save(self.path, self.cache)
            self.dirty = False


# ---------------------------------------------------------------------------------------
# remote helpers
# ---------------------------------------------------------------------------------------

def _is_missing(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(
        exc, "response") else ""
    return code in ("NoSuchKey", "404", "NotFound") or type(exc).__name__ == "NoSuchKey"


def _get_manifest(client, bucket: str, group: str) -> dict | None:
    try:
        body = client.get_object(Bucket=bucket, Key=f"manifest/{group}.json")["Body"].read()
    except Exception as exc:
        if _is_missing(exc):
            return None
        raise R2Error(f"cannot read manifest/{group}.json: {exc}") from exc
    return json.loads(body.decode("utf-8"))


def _git_sha() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _git_contains(sha: str) -> bool:
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=REPO_ROOT,
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _provenance() -> dict:
    return {"git_sha": _git_sha(), "MNQ_CONID": os.environ.get("MNQ_CONID"),
            "MES_CONID": os.environ.get("MES_CONID"), "source_host": socket.gethostname()}


def _scope(group: str, date: str | None) -> Callable[[str], bool]:
    if group == "sessions" and date:
        return lambda rel: rel.startswith(f"{date}/")
    return lambda rel: True


@dataclass
class Report:
    ok: bool = True
    uploaded: int = 0
    downloaded: int = 0
    bytes: int = 0
    skipped_equal: int = 0
    ahead: list = field(default_factory=list)       # local ahead, left alone / uploaded later
    conflicts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    pruned: list = field(default_factory=list)
    groups: dict = field(default_factory=dict)      # group -> {files, bytes, ...}
    pulled_parquets: bool = False
    parquets: list = field(default_factory=list)    # parquets being sent: rows + last_ts
    manifests: dict = field(default_factory=dict)
    message: str = ""

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def asdict(self) -> dict:
        return dict(self.__dict__)


def _guard(report: Report, dry_run: bool, what: str) -> bool:
    if dry_run:
        return True
    pid = _orchestrator_pid()
    if pid is not None:
        report.fail(f"{what} refused: the live orchestrator is running (pid {pid}) — "
                    "stop it first (trade.py terminate)")
        return False
    return True


# ---------------------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------------------

def _classify_publish(rel, L, R, S, force):
    """-> 'equal' | 'upload' | 'skip' | 'conflict' | 'remote_ahead' for one file (decision 7)."""
    if R is None:
        return "upload"
    if L["sha256"] == R["sha256"]:
        return "equal"
    if force:
        return "upload"
    is_pq = rel.endswith(".parquet")
    Tl, Tr = _ts(L.get("last_ts")), _ts(R.get("last_ts"))
    if is_pq and Tl is not None and Tr is not None:
        if Tl > Tr:
            return "conflict" if (S is not None and S != R["sha256"]) else "upload"
        if Tl < Tr:
            return "remote_ahead"
        # equal last timestamp: fall through to the hash rules
    if S == R["sha256"]:
        return "upload"
    if S == L["sha256"]:
        return "skip"      # remote moved, local untouched: nothing to publish
    return "conflict"      # incl. no state yet: never silently clobber the other machine's file


def publish(only=None, date: str | None = None, dry_run: bool = False, force: bool = False,
            client=None, cfg: R2Config | None = None, groups_default=DEFAULT_PUBLISH_GROUPS,
            ) -> dict:
    """Upload the selected groups; manifest of each group LAST. Returns a Report dict."""
    import paths
    rep = Report()
    names = parse_only(only, groups_default)
    groups = _groups()

    if dry_run:  # local scan only: no hashing, no network, nothing written
        for n in names:
            files = groups[n].scan()
            sc = _scope(n, date)
            sel = {r: p for r, p in files.items() if sc(r)}
            rep.groups[n] = {"files": len(sel), "bytes": sum(p.stat().st_size for p in sel.values()),
                             "file_list": sorted(sel)}
            rep.bytes += rep.groups[n]["bytes"]
        rep.message = "dry run: nothing uploaded"
        return rep.asdict()

    if not _guard(rep, dry_run, "publish"):
        return rep.asdict()
    cfg = cfg or load_config()
    client = client or make_client(cfg)
    bucket = cfg.bucket
    gr = paths.global_root()
    state_path = gr / ".r2_state.json"
    state = _json_load(state_path)
    local = _Local(gr)
    prov = _provenance()

    try:
        for n in names:
            g = groups[n]
            sc = _scope(n, date)
            files = {r: p for r, p in g.scan().items() if sc(r)}
            remote = _get_manifest(client, bucket, n)
            rfiles = dict((remote or {}).get("files", {}))
            infos = {r: local.info(p, g.parquet_ts) for r, p in files.items()}
            to_upload, pending_state, new_files = [], {}, dict(rfiles)
            for rel, L in infos.items():
                key = g.prefix + rel
                S = state.get(key)
                R = rfiles.get(rel)
                verdict = _classify_publish(rel, L, R, S, force)
                if verdict == "equal":
                    rep.skipped_equal += 1
                    pending_state[key] = L["sha256"]
                elif verdict == "upload":
                    to_upload.append(rel)
                elif verdict == "skip":
                    pass
                elif verdict == "remote_ahead":
                    rep.conflicts.append({"group": n, "path": rel, "kind": "remote_ahead",
                                          "local_last_ts": L.get("last_ts"),
                                          "remote_last_ts": R.get("last_ts")})
                else:
                    rep.conflicts.append({"group": n, "path": rel, "kind": "conflict",
                                          "local_last_ts": L.get("last_ts"),
                                          "remote_last_ts": (R or {}).get("last_ts")})
            gbytes = 0
            for rel in to_upload:
                key = g.prefix + rel
                L = infos[rel]
                client.upload_file(str(files[rel]), bucket, key,
                                   ExtraArgs={"Metadata": {"sha256": L["sha256"]}})
                new_files[rel] = L
                pending_state[key] = L["sha256"]
                rep.uploaded += 1
                gbytes += L["size"]
                if rel.endswith(".parquet"):
                    rep.parquets.append({"group": n, "path": rel, "rows": _parquet_rows(files[rel]),
                                         "last_ts": L.get("last_ts")})
            rep.bytes += gbytes
            rep.groups[n] = {"files": len(files), "uploaded": len(to_upload), "bytes": gbytes}
            if to_upload or remote is None:
                manifest = {"group": n, "published_at": _dt.datetime.now(
                    _dt.timezone.utc).isoformat(), **prov, "files": new_files}
                client.put_object(Bucket=bucket, Key=f"manifest/{n}.json",
                                  Body=json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"))
            state.update(pending_state)   # only after the group's manifest is in place
    except R2Error:
        raise
    except Exception as exc:
        rep.fail(f"publish failed: {type(exc).__name__}: {exc}")
    finally:
        local.save()
        _json_save(state_path, state)
    if rep.conflicts:
        rep.ok = False
    return rep.asdict()


# ---------------------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------------------

def _classify_sync(rel, L, R, S, force):
    """-> 'equal' | 'pull' | 'replace' | 'ahead' | 'conflict' (L is None: no local file)."""
    if L is None:
        return "pull"
    if L["sha256"] == R["sha256"]:
        return "equal"
    if force:
        return "replace"
    is_pq = rel.endswith(".parquet")
    Tl, Tr = _ts(L.get("last_ts")), _ts(R.get("last_ts"))
    if is_pq and Tl is not None and Tr is not None:
        if Tl > Tr:
            return "conflict" if (S is not None and S != R["sha256"]) else "ahead"
        if Tl < Tr:
            return "replace"
    if S is None:
        return "replace"
    if S == L["sha256"] and S != R["sha256"]:
        return "replace"
    if S != L["sha256"] and S == R["sha256"]:
        return "ahead"
    return "conflict"


def _download(client, bucket, key, dest: Path, R: dict, keep_prev: bool) -> None:
    tmp = dest.with_name(dest.name + ".r2tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(bucket, key, str(tmp))
        if tmp.stat().st_size != R["size"] or _sha256(tmp) != R["sha256"]:
            raise R2Error(f"corrupt download for {key}: size/sha256 mismatch")
        prev = dest.with_name(dest.name + ".r2prev")
        moved = False
        if keep_prev and dest.exists():
            os.replace(dest, prev)
            moved = True
        try:
            os.replace(tmp, dest)
        except Exception:
            if moved and not dest.exists():
                os.replace(prev, dest)
            raise
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def sync(only=None, date: str | None = None, dry_run: bool = False, prune: bool = False,
         force: bool = False, client=None, cfg: R2Config | None = None,
         groups_default=DEFAULT_SYNC_GROUPS) -> dict:
    """Pull the selected groups from R2 into <global>. Additive; never deletes without prune."""
    import paths
    rep = Report()
    names = parse_only(only, groups_default)
    if not _guard(rep, dry_run, "sync"):
        return rep.asdict()
    cfg = cfg or load_config()
    client = client or make_client(cfg)
    bucket = cfg.bucket
    groups = _groups()
    gr = paths.global_root()
    state_path = gr / ".r2_state.json"
    state = _json_load(state_path)
    local = _Local(gr, persist=not dry_run)
    roots = {"main": paths.general_main_dir(), "live": paths.general_live_dir(),
             "sessions": paths.sessions_dir(), "studies": gr / "studies"}

    try:
        for n in names:
            g = groups[n]
            if n == "logs":
                rep.warnings.append("logs are publish-only; skipped")
                continue
            remote = _get_manifest(client, bucket, n)
            if remote is None:
                rep.warnings.append(f"{n}: no manifest in R2 (nothing published yet)")
                continue
            rep.manifests[n] = {k: remote.get(k) for k in
                                ("published_at", "source_host", "git_sha", "MNQ_CONID", "MES_CONID")}
            if n == "thesis_cache":
                from agent.trader.thesis_cache import cache_root
                root = cache_root()
            else:
                root = roots[n]
            sc = _scope(n, date)
            rfiles = {r: v for r, v in remote.get("files", {}).items() if sc(r)}
            existing = {r: p for r, p in g.scan().items() if sc(r)}
            gstat = {"files": len(rfiles), "downloaded": 0, "bytes": 0}
            for rel, R in sorted(rfiles.items()):
                if rel.startswith("/") or ".." in Path(rel).parts or ":" in rel:
                    rep.fail(f"{n}: unsafe manifest path {rel!r} skipped")
                    continue
                key = g.prefix + rel
                dest = root / rel
                p = existing.get(rel)
                if p is None and dest.is_file():
                    p = dest
                L = local.info(p, g.parquet_ts) if p is not None else None
                S = state.get(key)
                verdict = _classify_sync(rel, L, R, S, force)
                if verdict == "equal":
                    rep.skipped_equal += 1
                    state[key] = R["sha256"]
                elif verdict == "ahead":
                    rep.ahead.append({"group": n, "path": rel, "local_last_ts": L.get("last_ts"),
                                      "remote_last_ts": R.get("last_ts")})
                elif verdict == "conflict":
                    rep.conflicts.append({"group": n, "path": rel,
                                          "local_last_ts": L.get("last_ts"),
                                          "remote_last_ts": R.get("last_ts")})
                else:  # pull / replace
                    if dry_run:
                        rep.downloaded += 1
                        rep.bytes += R["size"]
                        gstat["downloaded"] += 1
                        gstat["bytes"] += R["size"]
                        continue
                    try:
                        _download(client, bucket, key, dest, R,
                                  keep_prev=(verdict == "replace"))
                    except Exception as exc:
                        rep.fail(f"{key}: {type(exc).__name__}: {exc}")
                        continue
                    state[key] = R["sha256"]   # only after the verified download
                    rep.downloaded += 1
                    rep.bytes += R["size"]
                    gstat["downloaded"] += 1
                    gstat["bytes"] += R["size"]
                    if g.name in ("main", "live") and rel.endswith(".parquet"):
                        rep.pulled_parquets = True
            if prune:
                for rel, p in existing.items():
                    if rel not in rfiles:
                        rep.pruned.append(f"{n}/{rel}")
                        if not dry_run:
                            p.unlink()
            rep.groups[n] = gstat
    finally:
        if not dry_run:
            local.save()
            _json_save(state_path, state)
    if rep.conflicts:
        rep.ok = False
    _provenance_warnings(rep)
    return rep.asdict()


def _provenance_warnings(rep: Report) -> None:
    here = _provenance()
    seen = set()
    for n, m in rep.manifests.items():
        for var in ("MNQ_CONID", "MES_CONID"):
            theirs, mine = m.get(var), here.get(var)
            if theirs and mine != theirs and (var, theirs) not in seen:
                seen.add((var, theirs))
                rep.warnings.append(
                    f"WARNING: {var} here is {mine!r} but the publisher had {theirs!r} — a "
                    "contract roll happened on the other machine; edit this machine's .env by hand")
        sha = m.get("git_sha")
        if sha and sha != here.get("git_sha") and ("sha", sha) not in seen:
            seen.add(("sha", sha))
            if not _git_contains(sha):
                rep.warnings.append(
                    f"WARNING: publisher's commit {sha[:10]} is not in this checkout's history "
                    "(thesis cache is keyed per code version; replays may differ)")


def checklist(rep: dict) -> list[str]:
    """Short two-machine checklist printed after a sync that pulled main/live parquets."""
    here = _provenance()
    m = next(iter(rep.get("manifests", {}).values()), {})
    return [
        "Two-machine checklist:",
        f"  .env conids here MNQ={here['MNQ_CONID']} MES={here['MES_CONID']} | "
        f"publisher MNQ={m.get('MNQ_CONID')} MES={m.get('MES_CONID')}",
        f"  HEAD here {str(here['git_sha'])[:10]} | publisher {str(m.get('git_sha'))[:10]}",
        f"  published_at {m.get('published_at')} from {m.get('source_host')}",
    ]


# ---------------------------------------------------------------------------------------
# post-promote tail + CLI formatting
# ---------------------------------------------------------------------------------------

def publish_after_promote(stream=None, client=None, cfg: R2Config | None = None) -> dict:
    """Shared tail of `trade.py promote`, `trade.py rollover-prep` and the parquet-check
    session-end promote. Never raises. -> {"configured", "success", "error", "report"}.

    Not configured -> one line, success=True (promote keeps working offline). A failed upload
    does not undo the local promote; the caller turns `success=False` into a non-zero exit."""
    stream = stream or sys.stdout
    if client is None and cfg is None and not is_configured():
        print("R2 not configured — not published", file=stream)
        return {"configured": False, "success": True, "error": None, "report": None}
    try:
        rep = publish(client=client, cfg=cfg)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        print(f"R2 publish FAILED: {msg}", file=stream)
        return {"configured": True, "success": False, "error": msg, "report": None}
    for line in format_report(rep, "publish"):
        print(line, file=stream)
    err = None if rep["ok"] else "; ".join(
        rep["errors"] + [f"conflict {c['group']}/{c['path']}" for c in rep["conflicts"]]) or "failed"
    return {"configured": True, "success": rep["ok"], "error": err, "report": rep}


def format_report(rep: dict, verb: str) -> list[str]:
    lines = []
    for n, s in rep.get("groups", {}).items():
        extra = "".join(f" {k}={v}" for k, v in s.items() if k not in ("file_list",))
        lines.append(f"[r2 {verb}] {n}:{extra}")
    lines.append(f"[r2 {verb}] uploaded={rep.get('uploaded', 0)} downloaded={rep.get('downloaded', 0)} "
                 f"bytes={rep.get('bytes', 0)} unchanged={rep.get('skipped_equal', 0)}")
    for q in rep.get("parquets", []):
        lines.append(f"[r2 {verb}] sending {q['group']}/{q['path']}: rows={q['rows']} "
                     f"last_ts={q['last_ts']}")
    for a in rep.get("ahead", []):
        lines.append(f"[r2 {verb}] AHEAD, left alone: {a['group']}/{a['path']} "
                     f"(local last_ts {a['local_last_ts']} > remote {a['remote_last_ts']})")
    for c in rep.get("conflicts", []):
        lines.append(f"[r2 {verb}] CONFLICT, skipped: {c['group']}/{c['path']} "
                     f"local last_ts={c['local_last_ts']} remote last_ts={c['remote_last_ts']} "
                     f"({c.get('kind', 'both sides moved')}; --force picks a side)")
    for p in rep.get("pruned", []):
        lines.append(f"[r2 {verb}] pruned {p}")
    for w in rep.get("warnings", []):
        lines.append(f"[r2 {verb}] {w}")
    for e in rep.get("errors", []):
        lines.append(f"[r2 {verb}] ERROR: {e}")
    if rep.get("message"):
        lines.append(f"[r2 {verb}] {rep['message']}")
    return lines
