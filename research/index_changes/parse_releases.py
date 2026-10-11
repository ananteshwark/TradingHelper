"""Nifty 50 and Nifty Next 50 changes from NSE Indices' press releases
(research/index_changes/PROTOCOL.md).

Usage: uv run --no-project --isolated --python 3.12 --with pypdf python -I parse_releases.py \
           RELEASES_JSON PDF_DIR OUT_JSON
RELEASES_JSON lists {date, url, title} from niftyindices.com/press-release.
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

import pypdf

releases, pdf_dir, out = json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]), Path(sys.argv[3])
N50 = {"nifty 50", "nifty50", "cnx nifty", "s&p cnx nifty", "nifty"}
NN50 = {"nifty next 50", "niftynext50", "cnx nifty junior", "nifty junior", "s&p cnx nifty junior",
        "nifty next50"}
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{1,19}$")
STOP = {"LIMITED", "LTD", "INDEX", "SYMBOL", "NAME", "COMPANY", "NO", "AND", "OF", "INDIA"}
DATE = re.compile(r"(?:effective from|with effect from|w\.?\s?e\.?\s?f\.?)\s*:?\s*"
                  r"([A-Z][a-z]+\.?\s+\d{1,2}\s*,\s*\d{4})", re.I)
HEAD = re.compile(r"^(?:\(?\d{1,2}[\).]|\(?[A-Za-z][\).])\s+(.+)$")


def parse_date(s):
    s = re.sub(r"\s+", " ", s.replace(" ,", ",")).replace(".", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d,%Y", "%b %d,%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def which(name):
    n = re.sub(r"\s+", " ", name.lower()).strip(" :.-")
    n = re.sub(r"\s*index$", "", n).strip()
    return "N50" if n in N50 else "NN50" if n in NN50 else "other"


events, report = [], []
for rel in releases:
    f = pdf_dir / Path(rel["url"]).name
    if not f.exists():
        report.append((rel["date"], f.name, "missing"))
        continue
    try:
        text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(f).pages)
    except Exception as e:                              # noqa: BLE001
        report.append((rel["date"], f.name, f"unreadable: {e}"))
        continue
    flat = re.sub(r"\s+", " ", text)
    m = DATE.search(flat) or DATE.search(rel["title"])
    effective = parse_date(m.group(1)) if m else None
    current, mode, found = None, None, 0
    lines = [x for x in (re.sub(r"\s+", " ", raw).strip() for raw in text.splitlines()) if x]
    for i, line in enumerate(lines):
        h = HEAD.match(line)
        if h:                     # "1) NIFTY 50", "(2) CNX Nifty Junior"; table rows are "1 Name SYM"
            if len(h.group(1).split()) <= 8:     # longer: a numbered note, not a section
                current, mode = which(h.group(1)), None
            continue
        # A bare index name is a heading only when a table follows; else it is a wrapped note.
        if which(line) in ("N50", "NN50") and any("being" in x.lower() for x in lines[i + 1:i + 3]):
            current, mode = which(line), None
            continue
        low = line.lower()
        if "being excluded" in low or re.match(r"^exclusions?\b", low):
            mode = "exclude"
            continue
        if "being included" in low or re.match(r"^inclusions?\b", low):
            mode = "include"
            continue
        if current not in ("N50", "NN50") or mode is None or low.startswith("sr"):
            continue
        tok = line.split()[-1]
        if SYMBOL.match(tok) and tok not in STOP and re.search(r"[A-Z]", tok):
            events.append({"release": rel["date"], "file": f.name, "title": rel["title"],
                           "index": current, "action": mode, "symbol": tok,
                           "effective": effective.isoformat() if effective else None})
            found += 1
    report.append((rel["date"], f.name, f"{found} N50/NN50 rows, effective {effective}"))
out.write_text(json.dumps(events, indent=0))
for r in report:
    print(*r)
print(len(events), "rows")
