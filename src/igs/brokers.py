"""Brokers' buy / hold / sell calls: a second opinion for the AI's own calls, shown on the
stock page and the AI calls page. In the ranking they count only through the capped stock
sentiment adjustment (igs.sentiment).

They come from two places:
- news: the feeds in config/broker_calls.yaml (Economic Times RSS, Moneycontrol's news
  sitemap) are collected on every NSE check. A headline that states a whole call, as
  Moneycontrol writes them ("Buy Shriram Finance; target of Rs 1220: Motilal Oswal"), is
  recorded as it stands. Other articles that mention a rating, a target or a brokerage are
  read by the AI (igs.assistant.brokers), which records each explicit call;
- the owner: calls read elsewhere (a broker's report, older Moneycontrol pages) entered on
  the stock page, pasted, or added with `igs brokers add`.

A call names its stock as the source wrote it. It is linked to a company only by an exact
NSE symbol or by a name that matches exactly one company; otherwise it is kept unmatched
and shown as such, never guessed.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass, field

import httpx

from igs import service
from igs.config import BrokerCallsConfig, load_broker_calls
from igs.news import feed_client, fetch_feed
from igs.timeutil import IST

LOCK_KEY = 7215460016
STANCES = ("buy", "hold", "sell")
KINDS = ("research", "trading")
KEEP_UNUSED_DAYS = 30
SAME_CALL_DAYS = 3
# Words that go with a broker's call. Articles without any are not sent to the AI.
CANDIDATE = re.compile(
    r"\b(target|rating|rated|overweight|underweight|equal[- ]weight|outperform|"
    r"underperform|accumulate|reduce|neutral|upgrades?|upgraded|downgrades?|downgraded|"
    r"initiat\w*|coverage|reiterat\w*|maintains?|brokerage|top picks?|buy|sell)\b", re.I)
NAME_NOISE = re.compile(r"\b(ltd|limited|the|india|co|company|corporation|corp|inc)\b\.?")


def normalise(name: str) -> str:
    """A company or broker name for comparison: lower case, without "Ltd", punctuation or
    "&"/"and" differences."""
    s = name.lower().replace("&", " and ")
    s = NAME_NOISE.sub(" ", s)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def match_company(conn, name: str, symbol: str = "") -> int | None:
    """The company a call is about: its NSE symbol if one was written and exists, else
    the one company whose name matches, else None (kept unmatched, never guessed)."""
    if symbol.strip():
        hit = [c for c in service.companies(conn, q=symbol.strip(), limit=5)
               if c["symbol"].upper() == symbol.strip().upper()]
        if hit:
            return hit[0]["company_id"]
    if not name.strip():
        return None
    found = service.companies(conn, q=name, limit=20)
    exact = [c for c in found if normalise(c["name"]) == normalise(name)]
    if len({c["company_id"] for c in exact}) == 1:
        return exact[0]["company_id"]
    return found[0]["company_id"] if len({c["company_id"] for c in found}) == 1 else None


def dedupe_key(company_id: int | None, stock_name: str, broker: str, called_on: dt.date,
               stance: str, target: float | None) -> str:
    who = str(company_id) if company_id else normalise(stock_name)
    return (f"{who}|{normalise(broker)}|{called_on:%Y-%m-%d}|{stance}|"
            f"{'' if target is None else f'{target:.2f}'}")


def add_call(conn, *, company_id: int | None, stock_name: str, broker: str, stance: str,
             rating: str, kind: str, target_price: float | None, called_on: dt.date,
             source: str, article_id: int | None = None, url: str | None = None,
             quote: str | None = None, model: str | None = None,
             prompt_version: str | None = None) -> bool:
    """Store one call; False when the same call is already stored."""
    if stance not in STANCES or kind not in KINDS or source not in ("news", "manual",
                                                                       "pasted"):
        raise ValueError(f"not a broker call: stance {stance!r}, kind {kind!r}")
    if not broker.strip() or not stock_name.strip():
        raise ValueError("a broker call needs the broker and the stock")
    if target_price is not None and target_price <= 0:
        raise ValueError("a target price must be above zero")
    if called_on > dt.datetime.now(IST).date():
        raise ValueError("a broker call can't be dated in the future")
    key = dedupe_key(company_id, stock_name, broker, called_on, stance, target_price)
    who, firm, _, _, target = key.split("|")
    # The same call dated a few days apart is one call: a report's date on one page, the
    # day it was published on another, or the next day's article repeating it.
    if conn.execute("""select exists(select 1 from broker_call
            where split_part(dedupe_key, '|', 1) = %s and split_part(dedupe_key, '|', 2) = %s
              and stance = %s and split_part(dedupe_key, '|', 5) = %s
              and called_on between %s and %s)""",
            (who, firm, stance, target, called_on - dt.timedelta(days=SAME_CALL_DAYS),
             called_on + dt.timedelta(days=SAME_CALL_DAYS))).fetchone()[0]:
        return False
    cur = conn.execute("""insert into broker_call (company_id, stock_name, broker, stance,
            rating, kind, target_price, called_on, source, article_id, url, quote, model,
            prompt_version, dedupe_key)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (dedupe_key) do nothing""",
        (company_id, stock_name.strip(), broker.strip(), stance, rating.strip() or stance,
         kind, target_price, called_on, source, article_id, url, quote, model,
         prompt_version, key))
    return cur.rowcount == 1


def add_manual(conn, symbol: str, broker: str, stance: str, target_price: float | None,
               called_on: dt.date, rating: str = "", url: str = "", note: str = "",
               kind: str = "research") -> bool:
    """A call the owner read elsewhere (Moneycontrol, a broker's report)."""
    company = service.companies(conn, q=symbol, limit=5)
    exact = [c for c in company if c["symbol"].upper() == symbol.strip().upper()]
    if not exact:
        raise service.NotFound(f"unknown symbol {symbol!r}")
    added = add_call(conn, company_id=exact[0]["company_id"], stock_name=exact[0]["name"],
                     broker=broker, stance=stance, rating=rating or stance.capitalize(),
                     kind=kind, target_price=target_price, called_on=called_on,
                     source="manual", url=url.strip() or None, quote=note.strip() or None)
    conn.commit()
    if added:
        review_added(conn)
    return added


def match_call(conn, broker_call_id: int, symbol: str) -> bool:
    """Link an unmatched call to the company the owner names. The call is recorded again
    under the company, so it counts from now (the time it became the company's) and past
    runs stay as they were; False when the company already has the same call."""
    company = [c for c in service.companies(conn, q=symbol, limit=5)
               if c["symbol"].upper() == symbol.strip().upper()]
    if not company:
        raise service.NotFound(f"unknown symbol {symbol!r}")
    row = conn.execute("""select stock_name, broker, stance, rating, kind,
                                 target_price::float8, called_on, source, article_id, url,
                                 quote, model, prompt_version
                          from broker_call where broker_call_id = %s and company_id is null""",
                       (broker_call_id,)).fetchone()
    if row is None:
        raise service.NotFound(f"no unmatched broker call {broker_call_id}")
    (stock_name, broker, stance, rating, kind, target, called_on, source, article_id, url,
     quote, model, prompt_version) = row
    with conn.transaction():
        conn.execute("delete from broker_call where broker_call_id = %s", (broker_call_id,))
        added = add_call(conn, company_id=company[0]["company_id"], stock_name=stock_name,
                         broker=broker, stance=stance, rating=rating, kind=kind,
                         target_price=target, called_on=called_on, source=source,
                         article_id=article_id, url=url, quote=quote, model=model,
                         prompt_version=prompt_version)
    conn.commit()
    if added:
        review_added(conn)
    return added


def unmatched(conn, days: int) -> list[dict]:
    """Calls of the last `days` days not linked to a company, newest first."""
    since = dt.datetime.now(IST).date() - dt.timedelta(days=days)
    return _rows(conn, """select broker_call_id, called_on, stock_name, broker, stance,
                                 rating, target_price::float8 as target_price, url
                          from broker_call where company_id is null and called_on >= %s
                          order by called_on desc, broker_call_id desc""", (since,))


def delete_manual(conn, broker_call_id: int) -> None:
    """Only calls the owner entered or pasted can be deleted; calls read from news are the
    record."""
    conn.execute("""delete from broker_call where broker_call_id = %s
                    and source in ('manual', 'pasted')""", (broker_call_id,))
    conn.commit()


# --------------------------------------------------------------------------- headlines

# Moneycontrol states each broker's call whole in a headline, "Buy HDFC Bank; target of
# Rs 1,850: ICICI Securities", in its stock news (read from its news sitemap on every
# check) and on its pages (pasted by the owner for anything older).
MONEYCONTROL_URL = "https://www.moneycontrol.com/news/business/stocks/"
RATING_STANCE = {"buy": "buy", "accumulate": "buy", "add": "buy", "outperform": "buy",
                 "overweight": "buy", "positive": "buy", "hold": "hold", "neutral": "hold",
                 "sell": "sell", "reduce": "sell", "underperform": "sell",
                 "underweight": "sell", "negative": "sell"}
HEADLINE = re.compile(
    r"\b(" + "|".join(RATING_STANCE) + r")\s+([^;\n]{2,80}?)\s*;\s*target\s+of\s*"
    r"(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d+)?)\s*:\s*([^\n|]{2,60})", re.I)
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
MONTH = r"(" + "|".join(MONTHS) + r")[a-z]*\.?"
DATE = re.compile(rf"\b(?:{MONTH}\s+(\d{{1,2}}),?|(\d{{1,2}})\s+{MONTH},?)\s+(20\d\d)\b", re.I)


def _nearby_date(text: str, start: int, end: int, today: dt.date) -> dt.date | None:
    """The first plausible date between a headline and the next one."""
    for m in DATE.finditer(text, start, end):          # "April 21, 2024" or "23 Apr 2024"
        month, mday = (m[1], m[2]) if m[1] else (m[4], m[3])
        try:
            day = dt.date(int(m[5]), MONTHS.index(month[:3].lower()) + 1, int(mday))
        except ValueError:
            continue
        if today - dt.timedelta(days=365) <= day <= today:
            return day
    return None


def _headlines(m: re.Match) -> list[dict]:
    """The calls a headline states: one for each broker it names ("...: Motilal Oswal,
    ICICI Securities" is two calls), so every broker's call is its own record."""
    rating = m[1].strip().capitalize()
    firms = [" ".join(b.split()).rstrip(" .") for b in m[4].split(",")]
    return [{"rating": rating, "stance": RATING_STANCE[rating.lower()],
             "stock_name": " ".join(m[2].split()),
             "target_price": float(m[3].replace(",", "")), "broker": firm,
             "quote": " ".join(m[0].split())} for firm in firms if firm]


def headline_calls(title: str) -> list[dict]:
    """The calls a headline states whole (one for each broker it names), or none."""
    m = HEADLINE.fullmatch(" ".join(title.split()))
    return _headlines(m) if m and float(m[3].replace(",", "")) > 0 else []


def parse_pasted(text: str, default_day: dt.date, today: dt.date | None = None
                 ) -> list[dict]:
    """Brokers' calls in text copied from a Moneycontrol page (or a saved copy of it): one
    per headline, dated by the first date after it within the last year (the report's
    date, on its stock news page), else `default_day`."""
    if "<" in text and ">" in text:            # a saved page: its text, a line per block
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)</?(h\d|p|li|div|br|a|span|time)\b[^>]*>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    today = today or dt.datetime.now(IST).date()
    heads = list(HEADLINE.finditer(text))
    out, seen = [], set()
    for i, m in enumerate(heads):
        stop = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        day = _nearby_date(text, m.end(), stop, today) or default_day
        for call in _headlines(m):
            call["called_on"] = day
            key = (call["stock_name"].lower(), call["broker"].lower(), call["stance"],
                   call["target_price"], call["called_on"])
            if key not in seen:
                seen.add(key)
                out.append(call)
    return out


@dataclass
class PasteImport:
    found: int = 0
    added: int = 0
    unmatched: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.found:
            return ("No brokers' call headlines found. Copy the whole page (Ctrl+A, then "
                    "Ctrl+C) from moneycontrol.com/news/business/stocks and paste it again.")
        return (f"Found {self.found} calls: {self.added} added, "
                f"{self.found - self.added} already recorded"
                + (f"; not matched to a company: {', '.join(self.unmatched)}"
                   if self.unmatched else "") + ".")


def import_pasted(conn, text: str, default_day: dt.date, source_url: str = MONEYCONTROL_URL
                  ) -> PasteImport:
    """Store the calls parse_pasted finds. Stocks are matched as news calls are; the
    unmatched are kept and listed."""
    out = PasteImport()
    for c in parse_pasted(text, default_day):
        out.found += 1
        company_id = match_company(conn, c["stock_name"])
        if add_call(conn, company_id=company_id, stock_name=c["stock_name"],
                    broker=c["broker"], stance=c["stance"], rating=c["rating"],
                    kind="research", target_price=c["target_price"],
                    called_on=c["called_on"], source="pasted", url=source_url,
                    quote=c["quote"]):
            out.added += 1
            if company_id is None:
                out.unmatched.append(c["stock_name"])
    conn.commit()
    if out.added:
        review_added(conn)
    return out


# --------------------------------------------------------------------------- collection


@dataclass
class Collection:
    articles: int = 0
    candidates: int = 0
    headline_calls: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (f"{self.articles} new articles, {self.headline_calls} calls read from "
                f"headlines, {self.candidates} more mention a rating or target; "
                + ("; ".join(self.errors) or "no feed errors"))


def collect(conn, cfg: BrokerCallsConfig | None = None, *, force: bool = False,
            client: httpx.Client | None = None, review_calls: bool = True) -> Collection:
    """Fetch the feeds and keep new articles; the AI reads the candidates later."""
    cfg = cfg or load_broker_calls()
    out = Collection()
    if not cfg.enabled or not cfg.feeds:
        return out
    if not conn.execute("select pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
        conn.commit()
        out.errors.append("another broker-call collection is running")
        return out
    conn.commit()
    own_client = client is None
    client = client or feed_client()
    try:
        for feed in cfg.feeds:
            url = str(feed.url)
            recent = conn.execute("""select exists(select 1 from geopolitical_feed_fetch
                where feed_url = %s and fetched_at > now() - %s * interval '1 minute')""",
                (url, cfg.min_interval_minutes)).fetchone()[0]
            conn.commit()
            if recent and not force:
                continue
            got = fetch_feed(conn, client, feed.name, url, cfg.read_within_days,
                             cfg.stale_after_days, feed.sections)
            if got.error:
                out.errors.append(f"{feed.name}: {got.error}")
            before = out.headline_calls
            with conn.transaction():
                for a in got.articles:
                    heads = headline_calls(a["title"])
                    # A headline call is read here; the AI reads only the other candidates.
                    candidate = not heads and bool(CANDIDATE.search(a["body"]))
                    row = conn.execute("""insert into broker_article (url, title, body,
                            published_at, feed_name, fetch_id, candidate, read_at)
                        values (%s, %s, %s, %s, %s, %s, %s,
                                case when %s then clock_timestamp() end)
                        on conflict (url) do nothing returning article_id""",
                        (a["url"], a["title"], a["body"], a["published_at"], feed.name,
                         got.fetch_id, candidate, bool(heads))).fetchone()
                    if row is None:
                        continue
                    out.articles += 1
                    out.candidates += candidate
                    for head in heads:
                        out.headline_calls += add_call(
                            conn, company_id=match_company(conn, head["stock_name"]),
                            stock_name=head["stock_name"], broker=head["broker"],
                            stance=head["stance"], rating=head["rating"], kind="research",
                            target_price=head["target_price"],
                            called_on=a["published_at"].astimezone(IST).date(),
                            source="news", article_id=row[0], url=a["url"],
                            quote=head["quote"])
            if review_calls and out.headline_calls > before:
                review_added(conn)
        with conn.transaction():
            conn.execute("""delete from broker_article b
                where b.received_at < now() - %s * interval '1 day'
                  and (not b.candidate or b.read_at is not null)
                  and not exists (select 1 from broker_call c
                                  where c.article_id = b.article_id)
                  and not exists (select 1 from stock_news_tone t
                                  where t.article_id = b.article_id)""",
                         (KEEP_UNUSED_DAYS,))
        return out
    finally:
        conn.rollback()
        conn.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
        conn.commit()
        if own_client:
            client.close()


def step(conn) -> str:
    """Collect, then have the AI read what is waiting (when the assistant is on): brokers'
    calls, and the tone of each article for the stock sentiment adjustment. Then the AI
    gives its verdict on the calls just collected (igs.assistant.verdicts; the weekly
    refreshes wait for the daily job)."""
    from igs.config import load_assistant
    got = collect(conn)
    text = str(got)
    cfg = load_assistant()
    if cfg.enabled:
        from igs.assistant import news_tone, verdicts
        from igs.assistant.brokers import read_new
        from igs.assistant.llm import Assistant
        assistant = Assistant.open(conn)
        text += f"; {read_new(assistant)}"
        runs = service.runs(conn, limit=1)
        if cfg.features.verdicts.scheduled and runs:
            text += f"; {verdicts.scheduled(assistant, runs[0]['run_id'], refresh=False)}"
        if cfg.features.news_tone.max_per_run:
            text += f"; {news_tone.read_new(assistant)}"
    else:
        waiting = conn.execute("""select count(*) from broker_article
            where candidate and read_at is null""").fetchone()[0]
        text += f"; assistant off, {waiting} articles waiting to be read"
    if got.errors:
        raise RuntimeError(text)
    return text


# --------------------------------------------------------------------------- reading


def _rows(conn, sql: str, params: tuple) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


# The AI's latest verdict on each broker's call: from an AI call (igs.assistant.calls) or a
# review of the stock (igs.assistant.verdicts).
LATEST_VERDICT = """left join lateral (
                    select v.verdict as ai_verdict, v.reason as ai_reason,
                           v.given_at as ai_verdict_at
                    from ai_broker_verdict v
                    where v.broker_call_id = c.broker_call_id
                    order by v.given_at desc, v.verdict_id desc limit 1) v on true"""
LATEST_CLOSE = """(select p.close::float8 from price_eod p
                    join security_identifier si on si.id_type = 'ISIN' and si.id_value = p.isin
                    join security s using (security_id)
                    where s.company_id = c.company_id and p.exchange = 'NSE'
                      and p.trade_date <= %s
                    order by p.trade_date desc limit 1)"""


def calls_for(conn, company_id: int, since: dt.date, as_of: dt.date | None = None
              ) -> list[dict]:
    """A company's broker calls from `since` to `as_of` (today), newest first, with the
    latest close up to `as_of` for the upside to each target."""
    as_of = as_of or dt.datetime.now(IST).date()
    rows = _rows(conn, f"""select c.broker_call_id, c.called_on, c.broker, c.stance,
            c.rating, c.kind, c.target_price::float8 as target_price, c.source, c.url,
            c.quote, {LATEST_CLOSE} as last_close, v.ai_verdict, v.ai_reason,
            v.ai_verdict_at
        from broker_call c {LATEST_VERDICT}
        where c.company_id = %s and c.called_on between %s and %s
        order by c.called_on desc, c.broker_call_id desc""",
        (as_of, company_id, since, as_of))
    for r in rows:
        r["upside"] = (r["target_price"] / r["last_close"] - 1
                       if r["target_price"] and r["last_close"] else None)
    return rows


def recent(conn, days: int) -> list[dict]:
    """Every broker call of the last `days` days, newest first, with the stock's symbol
    (none if unmatched) and the AI's latest call on it."""
    since = dt.datetime.now(IST).date() - dt.timedelta(days=days)
    return _rows(conn, f"""select c.broker_call_id, c.called_on, c.broker, c.stance,
            c.rating, c.kind,
            c.target_price::float8 as target_price, c.stock_name, c.source, c.url,
            c.company_id, v.ai_verdict, v.ai_reason, v.ai_verdict_at,
            (select si.id_value from security_identifier si join security s
               using (security_id) where s.company_id = c.company_id
               and si.id_type = 'NSE_SYMBOL' order by si.valid_to is null desc,
               si.valid_from desc limit 1) as symbol,
            (select a.action from ai_call a where a.company_id = c.company_id
               order by a.created_at desc limit 1) as ai_action,
            (select a.created_at from ai_call a where a.company_id = c.company_id
               order by a.created_at desc limit 1) as ai_made
        from broker_call c {LATEST_VERDICT} where c.called_on >= %s
        order by c.called_on desc, c.broker_call_id desc""", (since,))


def status(conn) -> dict:
    return _rows(conn, """select count(*) filter (where candidate and read_at is null)
                                 as waiting,
                                 count(*) filter (where read_error is not null
                                                  and read_at is null) as failing,
                                 max(received_at) as last_article
                          from broker_article""", ())[0]


def review_added(conn, assistant=None) -> str:
    """Review committed new calls immediately; failures leave them pending for sync."""
    import logging

    from igs.alerts.delivery import send_agreements
    from igs.assistant import verdicts
    from igs.assistant.llm import Assistant
    from igs.config import load_assistant
    try:
        send_agreements(conn)
        cfg = assistant.cfg if assistant is not None else load_assistant()
        if not cfg.enabled or not cfg.features.verdicts.scheduled:
            return 'Automatic verdicts off; calls remain pending'
        runs = service.runs(conn, limit=1)
        if not runs:
            return 'No score run yet; calls remain pending'
        result = verdicts.scheduled(assistant or Assistant.open(conn, cfg),
                                    runs[0]['run_id'], refresh=False)
        send_agreements(conn)
        return str(result)
    except Exception:  # noqa: BLE001 - ingestion is durable even when the model is unavailable
        conn.rollback()
        logging.getLogger(__name__).warning('Broker verdicts pending; next check will retry')
        return 'AI review unavailable; calls remain pending for the next check'
