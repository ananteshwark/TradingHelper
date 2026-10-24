"""NSE corporate announcements, a week per request, 2013 to October 2026, kept compact: one
JSON-lines file per month. Paced, with the project's honest user agent. Resumable."""
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

out = Path(sys.argv[1])
UA = "IndiaGrowthScreener research (ananteshwark/TradingHelper)"
KEEP = ("symbol", "sm_isin", "an_dt", "exchdisstime", "desc", "smIndustry")
d, end = dt.date(2013, 1, 1), dt.date(2026, 10, 10)
if len(sys.argv) > 2:          # start later on the same weekly grid
    d += dt.timedelta(days=7 * ((dt.date.fromisoformat(sys.argv[2]) - d).days // 7))
while d <= end:
    month_file = out / f"{d:%Y-%m}.jsonl"
    week_end = min(d + dt.timedelta(days=6), end)
    done = out / f".done-{d:%Y%m%d}"
    if not done.exists():
        url = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
               f"&from_date={d:%d-%m-%Y}&to_date={week_end:%d-%m-%Y}")
        for attempt in range(4):
            r = subprocess.run(["curl", "-sS", "-m", "180", "-A", UA, "-H",
                                "Accept: application/json", url], capture_output=True)
            try:
                rows = json.loads(r.stdout)
                break
            except ValueError:
                time.sleep(15 * (attempt + 1))
        else:
            print(f"{d} FAILED", flush=True)
            d = week_end + dt.timedelta(days=1)
            continue
        with month_file.open("a") as fh:
            for x in rows:
                rec = {k: x.get(k) for k in KEEP}
                rec["text"] = (x.get("attchmntText") or "")[:400]
                fh.write(json.dumps(rec) + "\n")
        done.touch()
        print(f"{d} {len(rows)}", flush=True)
        time.sleep(3)
    d = week_end + dt.timedelta(days=1)
print("done", flush=True)
