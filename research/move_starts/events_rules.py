"""Keyword rules for research/move_starts (EVENTS.md): announcement groups and
institutional names in block deals. Written after listing the categories and names with
their counts, before any return was computed."""
import re

I = re.IGNORECASE
NON_EVENTS = re.compile(
    r"trading window|record date|book closure|dividend|agm|annual general|extraordinary general|"
    r"shareholders meeting|postal ballot|newspaper|loss of share|registrar|share transfer|"
    r"address change|code of conduct|trading plan|limited review|voting pattern|"
    r"esop|esos|esps|allotment of esop|committee meeting|corrigendum|monitoring agency|"
    r"statement of deviation|related party|insider trading|\(pit\)|pit reg|company secretary|"
    r"compliance officer|financial result|results update|^results|publish audited|"
    r"outcome of board meeting|board meeting|meeting updates|court convened|interest rates", I)
GROUPS = [
    ("DISTRESS", "desc", re.compile(r"default|insolvency|winding up|strike|lockout|disturbance", I)),
    ("QUERY", "desc", re.compile(r"price movement|spurt in volume|news verification|clarification", I)),
    ("PLEDGE", "both", re.compile(r"pledge|encumbrance", I)),
    ("ORDER", "both", re.compile(
        r"(bag|receiv|receipt|award|\bwin\b|\bwon\b|secur|bagging|bagged).{0,40}"
        r"\b(order|orders|contract|contracts|loa|letter of award|letter of intent)\b|"
        r"\b(order|orders|contract|contracts)\b.{0,20}(bag|receiv|award|win|won|secur)", I)),
    ("BUYBACK", "both", re.compile(r"buy-?\s?back", I)),
    ("BONUS_SPLIT", "both", re.compile(r"\bbonus\b|sub-?division|split of", I)),
    ("FUNDRAISE", "both", re.compile(
        r"allotment of securities|issue of securities|preferential|qualified institutional|"
        r"\bqip\b|rights issue|fund\s?rais|raising of funds", I)),
    ("ACQ", "desc", re.compile(r"acquisition|amalgamation|merger|scheme of arrangement|"
                               r"diversification|disinvestment|memorandum of understanding", I)),
    ("RATING", "desc", re.compile(r"credit rating", I)),
    ("EXIT", "desc", re.compile(r"resignation|cessation|change in management|change in auditors", I)),
    ("INVESTOR", "desc", re.compile(r"analysts|institutional investor|investor presentation", I)),
    ("SAST", "desc", re.compile(r"takeover", I)),
    ("PRESS", "desc", re.compile(r"press release", I)),
]
EMPLOYEE = re.compile(r"esop|esos|esps|stock option|employee|exercise of option", I)
SENIOR = re.compile(r"chief financial|\bcfo\b|managing director|\bceo\b|chief executive|"
                    r"auditor|independent director|whole[- ]time director|chairman", I)
EXCLUDE_ORDER = re.compile(r"court|tribunal|nclt|sebi|tax|gst|demand|penalty|show cause|"
                           r"arbitration award|interim order", I)
RATING_UP = re.compile(r"upgrad|revised upward|outlook.{0,20}positive", I)
RATING_DN = re.compile(r"downgrad|revised downward|outlook.{0,20}negative|watch.{0,20}negative|"
                       r"\bdefault\b", I)


def announcement_group(desc: str, text: str) -> str | None:
    """The group of an NSE announcement, or None for routine filings."""
    for name, where, rx in GROUPS:
        hay = desc if where == "desc" else f"{desc} {text}"
        if not rx.search(hay):
            continue
        if name == "ORDER" and EXCLUDE_ORDER.search(f"{desc} {text}"):
            continue
        if name == "FUNDRAISE" and EMPLOYEE.search(f"{desc} {text}"):
            return None
        if name == "EXIT":
            return "EXIT" if SENIOR.search(f"{desc} {text}") else None
        if name == "RATING":
            return ("RATING_DN" if RATING_DN.search(text) else
                    "RATING_UP" if RATING_UP.search(text) else "RATING")
        return name
    if NON_EVENTS.search(desc):
        return None
    return "OTHER" if desc.strip().lower() in ("updates", "general updates") else None


INSTITUTIONS = re.compile(
    r"mutual fund|\bmf\b|life insurance|insurance|assurance|pension|provident|asset management|"
    r"\bamc\b|goldman sachs|morgan stanley|merrill lynch|j\.?\s?p\.?\s?morgan|jpmorgan|j p m s l|"
    r"citigroup|\bciti\b|bnp paribas|societe generale|\bubs\b|nomura|clsa|hsbc|barclays|"
    r"credit suisse|deutsche|vanguard|blackrock|fidelity|government of singapore|"
    r"abu dhabi investment|norges|monetary authority|kuwait investment|qatar investment|"
    r"\blic\b|sbi |hdfc |icici prudential|nippon|kotak mahindra|axis |\buti\b|\bdsp\b|mirae|"
    r"franklin|aditya birla sun life|birla sun life|tata aia|foreign portfolio|\bfpi\b", I)
BROKERS = re.compile(r"securities|broking|stock ?broker|shares|finstock|trading|traders|"
                     r"commodities|capital services|portfolio", I)
FOREIGN = re.compile(r"\bfund\b|\bplc\b|\bpte\b|mauritius|\bs\.?a\.?\b|gmbh|\bllc\b|\bl\.?p\.?\b|"
                     r"\bltd\.? \(?(cayman|singapore)", I)


def is_institution(client: str) -> bool:
    """A mutual fund, insurer, pension fund, bank's asset arm or foreign investor; not a
    broker or a trading house."""
    if INSTITUTIONS.search(client):
        return True
    if BROKERS.search(client):
        return False
    return bool(FOREIGN.search(client))
