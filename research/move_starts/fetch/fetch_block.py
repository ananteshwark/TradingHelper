"""NSE block deals, 2013 to October 2026. The listing returns at most 70 rows a request, so
a month that hits 70 is fetched again by week, and a week that hits 70 by day. Paced, with
the project's honest user agent. Resumable (a file a month)."""
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

out = Path(sys.argv[1])
UA = "IndiaGrowthScreener research (ananteshwark/TradingHelper)"
CAP = 70


def get(a, b):
    url = ("https://www.nseindia.com/api/historicalOR/bulk-block-short-deals?optionType=block_deals"
           f"&from={a:%d-%m-%Y}&to={b:%d-%m-%Y}")
    for attempt in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "120", "-A", UA, "-H", "Accept: application/json",
                            url], capture_output=True)
        time.sleep(3)
        try:
            return json.loads(r.stdout)["data"]
        except (ValueError, KeyError):
            time.sleep(15 * (attempt + 1))
    print(f"FAILED {a} {b}", flush=True)
    return []


def fetch(a, b, unit):
    rows = get(a, b)
    if len(rows) < CAP or unit == "day":
        if len(rows) >= CAP:
            print(f"CAPPED day {a}", flush=True)
        return rows
    rows, step = [], 7 if unit == "month" else 1
    d = a
    while d <= b:
        e = min(d + dt.timedelta(days=step - 1), b)
        rows += fetch(d, e, "week" if unit == "month" else "day")
        d = e + dt.timedelta(days=1)
    return rows


m = dt.date(2013, 1, 1)
while m <= dt.date(2026, 10, 1):
    f = out / f"{m:%Y-%m}.json"
    nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    if not f.exists():
        rows = fetch(m, min(nxt - dt.timedelta(days=1), dt.date(2026, 10, 10)), "month")
        f.write_text(json.dumps({"data": rows}))
        print(m, len(rows), flush=True)
    m = nxt
print("done", flush=True)
