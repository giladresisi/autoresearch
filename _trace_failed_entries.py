import json
from pathlib import Path

for date in ["2026-05-14", "2026-05-15"]:
    p = Path("data/regression") / date / "events_1s.jsonl"
    if not p.exists():
        p = Path("data/regression") / date / "events.jsonl"
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    kinds = {"stop-entry", "stopped-out", "market-close", "end-of-session",
             "new-hypothesis", "trend-broken", "stop-entry-cancelled"}
    print(f"=== {date} ===")
    for e in lines:
        if e.get("kind") not in kinds:
            continue
        t = e.get("time", "")[11:19]
        k = e.get("kind", "")
        extra = ""
        if k == "new-hypothesis":
            extra = f"  dir={e.get('direction','')}"
        elif k == "trend-broken":
            extra = f"  level={e.get('level_name','')}  dir={e.get('direction','')}"
        elif k in ("stop-entry", "stopped-out", "market-close", "end-of-session", "stop-entry-cancelled"):
            fe = e.get("failed_entries", "?")
            price = e.get("price", "")
            extra = f"  price={price}  failed_entries={fe}"
        print(f"  {t}  {k}{extra}")
    print()
