"""Command line entry point: `igs <group> <command>`."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from igs.guardrails import DISCLAIMER

REPO_ROOT = Path(__file__).resolve().parents[2]


def raw_root() -> Path:
    return Path(os.environ.get("IGS_RAW_ROOT", REPO_ROOT / "data" / "raw"))


def _db_migrate(args: argparse.Namespace) -> int:
    from igs.db import connect, migrate

    with connect() as conn:
        applied = migrate(conn)
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


def _db_status(args: argparse.Namespace) -> int:
    """What is loaded: rows per table and the latest price day and filing, so a long
    backfill can be checked and resumed from the right day."""
    from igs.db import connect, database_url
    from igs.ingest.jobs import snapshot_counts
    from igs.timeutil import IST

    url = database_url()
    shown = url.split("@", 1)[-1] if "@" in url else url
    from igs import service
    from igs.config import load_universe
    from igs.universe import price_series
    with connect() as conn:
        latest_price = conn.execute("select max(trade_date) from price_eod").fetchone()[0]
        latest_filing = conn.execute("select max(filed_at) from filing").fetchone()[0]
        counts = snapshot_counts(conn)
        history = service.price_coverage(conn, price_series(load_universe()))
    print(f"database        {shown}")
    print(f"latest price    {latest_price or 'none loaded'}")
    if history["days"]:
        h = history
        gap = h["longest_gap"]
        print(f"price days      {h['days']:,} from {h['first']} to {h['latest']}; "
              + (f"{h['missing']:,} trading days missing, the longest gap {gap[0]} to "
                 f"{gap[1]} ({gap[2]} days)" if gap else "no trading day missing"))
        print(f"price history   {h['trading_now']:,} stocks trading now; "
              f"{h['sessions_127']:,} have the 127 sessions 6-month factors need, "
              f"{h['sessions_253']:,} the 253 of 12-month ones")
    print("latest filing   " + (f"{latest_filing.astimezone(IST):%Y-%m-%d %H:%M} IST"
                               if latest_filing else "none loaded"))
    for table, rows in counts.iter_rows():
        print(f"  {table:24} {rows:>12,}")
    return 0


def _raw_reindex(args: argparse.Namespace) -> int:
    from igs.db import connect
    from igs.ingest.raw_store import RawStore

    with connect() as conn:
        n = RawStore(raw_root()).reindex_into_db(conn)
        conn.commit()
    print(f"indexed {n} new fetch records")
    return 0


def _sources_list(args: argparse.Namespace) -> int:
    from igs.config import load_sources
    from igs.ingest.verify import latest_verification

    root = raw_root()
    print(f"{'source':32} {'tier':4} {'kind':10} {'status':10} checked_at / message")
    for s in load_sources().sources:
        v = latest_verification(root, s.id)
        status = "no url" if s.url is None else (v.status if v else "unverified")
        detail = f"{v.checked_at}  {v.message}" if v else ""
        print(f"{s.id:32} {s.tier:<4} {s.kind:10} {status:10} {detail}")
    return 0


def _sources_verify(args: argparse.Namespace) -> int:
    from igs.config import load_sources
    from igs.ingest.http import Fetcher
    from igs.ingest.raw_store import RawStore
    from igs.ingest.verify import verify_source

    cfg = load_sources()
    specs = [cfg.get(i) for i in args.ids] if args.ids else cfg.sources
    fetcher = Fetcher(RawStore(raw_root()))
    failed = 0
    for spec in specs:
        v = verify_source(spec, fetcher)
        failed += v.status != "verified"
        rows = f" rows={v.row_count}" if v.row_count is not None else ""
        print(f"{spec.id:32} {v.status.upper():9} {v.message}{rows}")
        if v.schema:
            print(f"{'':32} schema: {v.schema}")
    return 1 if failed else 0


def _gate_run(args: argparse.Namespace) -> int:
    from igs.pit.gate import GateError, run_gate

    try:
        rec = run_gate()
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"look-ahead gate PASSED ({rec.summary}); fingerprint {rec.fingerprint[:12]}")
    return 0


def _gate_check(args: argparse.Namespace) -> int:
    from igs.pit.gate import GateError, require_gate

    try:
        rec = require_gate()
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"look-ahead gate valid (passed {rec.passed_at})")
    return 0


def _date(s: str):
    import datetime as dt
    return dt.date.fromisoformat(s)


class IngestionBusy(RuntimeError):
    pass


def _context(with_fetcher: bool = True, writer: bool = False):
    from igs.config import load_sources
    from igs.db import connect
    from igs.ingest.http import Fetcher
    from igs.ingest.jobs import Context
    from igs.ingest.raw_store import RawStore

    store = RawStore(raw_root())
    conn = connect()
    if writer:
        from igs.sync import LOCK_KEY
        if not conn.execute("select pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
            conn.close()
            raise IngestionBusy("another ingestion job is running; retry after it finishes")
        conn.commit()
    return Context(conn=conn, store=store, sources=load_sources(),
                   fetcher=Fetcher(store) if with_fetcher else None)


def _finish(ctx, results) -> int:
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    bad = 0
    for r in results:
        flag = "FAIL" if r.note == "failed" else "ok" if r.http_status == 200 else "SKIP"
        bad += r.http_status not in (200, 404)
        print(f"{flag:4} {r.source_id:28} rows={r.rows:<7} HTTP {r.http_status} {r.url}"
              + (f"  [{r.note}]" if r.note else ""))
    print(f"data-quality issues: {ctx.dq.count('error')} error, {ctx.dq.count('warn')} warn")
    return 1 if bad or ctx.dq.count("error") else 0


def _ingest_static(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_static
    ctx = _context(writer=True)
    return _finish(ctx, [ingest_static(ctx, sid) for sid in args.ids])


def _ingest_prices(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import backfill_prices
    ctx = _context(writer=True)
    return _finish(ctx, backfill_prices(ctx, _date(args.start), _date(args.end),
                                        with_delivery=not args.no_delivery))


def _ingest_range(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_range
    ctx = _context(writer=True)
    return _finish(ctx, ingest_range(ctx, args.source, _date(args.start), _date(args.end)))


def _ingest_symbols(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_symbols
    ctx = _context(writer=True)
    symbols = args.symbols
    if not symbols:
        with ctx.conn.cursor() as cur:
            cur.execute("select id_value from security_identifier "
                        "where id_type = 'NSE_SYMBOL' and valid_to is null order by 1")
            symbols = [r[0] for r in cur.fetchall()]
    return _finish(ctx, ingest_symbols(ctx, args.source, symbols))


def _ingest_pages(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_pages
    ctx = _context(writer=True)
    return _finish(ctx, ingest_pages(ctx, args.source, start_page=args.from_page,
                                     max_pages=args.max_pages, until_known=not args.backfill))


def _with_assistant(fn):
    """Run fn(assistant) with the optional research assistant; a clear message, not a
    traceback, when it is off, not installed, over budget or the API fails."""
    try:
        from igs.assistant.llm import Assistant, AssistantError, AssistantUnavailable
    except ImportError:
        print("the research assistant needs the Anthropic SDK: run `uv sync --all-groups`")
        return 2
    from igs.db import connect
    try:
        with connect() as conn:
            return fn(Assistant.open(conn))
    except (AssistantUnavailable, AssistantError) as exc:
        print(f"assistant: {exc}")
        return 2


def _ask(args: argparse.Namespace) -> int:
    from igs.assistant.ask import ask

    def run(assistant) -> int:
        a = ask(assistant, " ".join(args.question), args.run_id)
        print(a.text)
        print(f"\n[run {a.run_id} as of {a.as_of:%Y-%m-%d}; {len(a.tool_calls)} lookups; "
              f"~${a.cost_usd:.3f}; {a.model}]" + "".join(f"\n[{n}]" for n in a.notes))
        print(DISCLAIMER)
        return 0
    return _with_assistant(run)


def _news_import(args: argparse.Namespace) -> int:
    import json

    from igs.db import connect
    from igs.geopolitical import import_articles
    try:
        data = json.loads(Path(args.path).read_text())
        with connect() as conn:
            count = import_articles(conn, data)
            conn.commit()
    except (OSError, ValueError) as exc:
        print(f"News import failed: {exc}", file=sys.stderr)
        return 2
    print(f"imported {count} news articles")
    return 0


def _news_collect(args: argparse.Namespace) -> int:
    from igs.db import connect
    from igs.news import collect_news
    with connect() as conn:
        report = collect_news(conn, force=args.force)
    print(report)
    return 1 if report.errors else 0


def _brokers_collect(args: argparse.Namespace) -> int:
    from igs import brokers
    from igs.config import load_assistant
    from igs.db import connect
    with connect() as conn:
        got = brokers.collect(conn, force=args.force)
        print(got)
        if load_assistant().enabled and not args.no_read:
            from igs.assistant import news_tone
            from igs.assistant.brokers import read_new
            from igs.assistant.llm import Assistant, AssistantUnavailable
            try:
                assistant = Assistant.open(conn)
                print(read_new(assistant))
                if load_assistant().features.news_tone.max_per_run:
                    print(news_tone.read_new(assistant))
            except AssistantUnavailable as exc:
                print(f"not read: {exc}", file=sys.stderr)
                return 1
    return 1 if got.errors else 0


def _brokers_add(args: argparse.Namespace) -> int:
    import datetime as dt

    from igs import brokers, service
    from igs.db import connect
    from igs.timeutil import IST
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(IST).date()
    with connect() as conn:
        try:
            added = brokers.add_manual(conn, args.symbol, args.broker, args.call, args.target,
                                       day, rating=args.rating or "", url=args.url or "",
                                       note=args.note or "",
                                       kind="trading" if args.trading else "research")
        except (ValueError, service.NotFound) as exc:
            print(exc, file=sys.stderr)
            return 2
    print("added" if added else "already recorded")
    return 0


def _brokers_import(args: argparse.Namespace) -> int:
    import datetime as dt
    from pathlib import Path

    from igs import brokers
    from igs.db import connect
    from igs.timeutil import IST
    day = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(IST).date()
    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    with connect() as conn:
        got = brokers.import_pasted(conn, text, day)
    print(got)
    return 0 if got.found else 1


def _brokers_list(args: argparse.Namespace) -> int:
    from igs import brokers
    from igs.db import connect
    with connect() as conn:
        rows = brokers.recent(conn, args.days)
    for r in rows:
        target = f" target Rs {r['target_price']:,.0f}" if r["target_price"] else ""
        ai = f"; AI: {r['ai_action']}" if r["ai_action"] else ""
        verdict = (f"; AI's verdict: {r['ai_verdict']} - {r['ai_reason']}"
                   if r["ai_verdict"] else "")
        print(f"{r['called_on']:%Y-%m-%d}  {r['symbol'] or '(not matched)':12} "
              f"{r['broker']}: {r['rating']} ({r['stance']}){target}  [{r['source']}]{ai}"
              f"{verdict}")
    print(f"{len(rows)} broker calls in {args.days} days")
    return 0


def _brokers_match(args: argparse.Namespace) -> int:
    from igs import brokers, service
    from igs.db import connect
    with connect() as conn:
        if args.id is None:
            rows = brokers.unmatched(conn, args.days)
            for r in rows:
                print(f"{r['broker_call_id']:>7}  {r['called_on']:%Y-%m-%d}  "
                      f"{r['stock_name']!r} - {r['broker']}: {r['rating']}")
            print(f"{len(rows)} calls of the last {args.days} days not matched to a company; "
                  "`igs brokers match ID SYMBOL` links one")
            return 0
        if not args.symbol:
            print("give the NSE symbol to link the call to")
            return 2
        try:
            added = brokers.match_call(conn, args.id, args.symbol)
        except service.NotFound as exc:
            print(exc)
            return 1
    print(f"call {args.id} linked to {args.symbol.upper()}" if added else
          f"{args.symbol.upper()} already had that call; the unmatched copy was removed")
    return 0


def _screener_check(args: argparse.Namespace) -> int:
    from igs import screener, service
    from igs.db import connect
    from igs.timeutil import IST
    with connect() as conn:
        try:
            company = service._company(conn, args.symbol)
        except service.NotFound as exc:
            print(exc)
            return 1
        c = screener.check(conn, company["company_id"])
    if c is None:
        print(f"no Screener.in export imported for {company['symbol']}: download it with "
              f"Export to Excel on {screener.page_url(company['symbol'])} and run "
              "`igs import screener FILE`")
        return 1
    print(f"{company['symbol']}: {c['file']} imported "
          f"{c['imported_at'].astimezone(IST):%Y-%m-%d %H:%M} IST")
    for r in c["rows"]:
        app = "" if r["app_cr"] is None else f"  app {r['app_cr']:>12,.2f} ({r['basis']})"
        diff = "" if r["diff_pct"] is None else f"  {r['diff_pct']:+.1f}%"
        print(f"  {r['period']}  {r['line']:<34} Screener {r['screener_cr']:>12,.2f}{app}"
              f"{diff}  {r['status']}")
    print(screener.summary(c))
    return 1 if c["differ"] else 0


def _screener_wanted(args: argparse.Namespace) -> int:
    from igs import screener, service
    from igs.db import connect
    with connect() as conn:
        runs = service.runs(conn, limit=1)
        rows = screener.wanted(conn, runs[0]["run_id"] if runs else None, args.days)
    for r in rows:
        print(f"{r['symbol']:<12} {r['why']}  {r['url']}")
    print(f"{len(rows)} stocks with brokers' calls whose data is thin: download each "
          "one's Export to Excel from its page and run `igs import screener FILE...`")
    return 0


def _news_assess(args: argparse.Namespace) -> int:
    def run(assistant):
        from igs.assistant.geopolitical import assess_pending
        try:
            count = assess_pending(assistant, args.limit)
        except ValueError as exc:
            print(f"News assessment rejected: {exc}", file=sys.stderr)
            return 2
        print(f"stored {count} company impact assessments; run `igs score` to update ratings")
        return 0
    return _with_assistant(run)


def _news_process(args: argparse.Namespace) -> int:
    from igs.config import load_assistant, load_scoring
    collected = _news_collect(args)
    if not load_assistant().enabled or not load_scoring().geopolitical.enabled:
        print("AI assessment is disabled; automatic collection completed")
        return collected
    return max(collected, _news_assess(args))


def _assistant_status(args: argparse.Namespace) -> int:
    from igs import settings
    from igs.config import load_assistant
    cfg = load_assistant()
    local = settings.assistant_path()
    print(f"enabled         {cfg.enabled}  (config/assistant.yaml"
          + (f", changed by the Settings page in {local})" if local.is_file() else ")"))
    print(f"model           {cfg.model}; fallbacks {cfg.fallbacks or 'off'}")
    print(f"daily budget    ${cfg.daily_budget_usd:.2f}")
    key = any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
    print(f"credentials     {'set in the environment or .env' if key else 'none found'}")
    try:
        import anthropic
        print(f"sdk             anthropic {anthropic.__version__}")
    except ImportError:
        print("sdk             not installed: run `uv sync --all-groups`")
    import psycopg

    from igs.db import connect
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""select feature, count(*), coalesce(sum(cost_usd), 0)::float8
                           from llm_call where called_at >= now() - interval '1 day'
                           group by feature order by feature""")
            rows = cur.fetchall()
    except psycopg.errors.UndefinedTable:
        print("database        not migrated for the assistant: run `igs db migrate`")
        return 1
    except psycopg.OperationalError as exc:
        print(f"database        unreachable: {str(exc).splitlines()[0]}")
        return 1
    for feature, n, cost in rows:
        print(f"last 24 hours   {feature}: {n} calls, ~${cost:.3f}")
    return 0


def _assistant_brief(args: argparse.Namespace) -> int:
    from igs.assistant.brief import brief

    def run(assistant) -> int:
        b = brief(assistant, args.symbol, args.run_id, refresh=args.refresh)
        print(b.text)
        print(f"\n[{b.symbol}, run {b.run_id}; {b.model}"
              + ("; stored brief" if b.cached else f"; ~${b.cost_usd:.3f}") + "]")
        print(DISCLAIMER)
        return 0
    return _with_assistant(run)


def print_call(c: dict) -> None:
    print(f"{c['symbol']}: {c['action'].upper()}  (confidence {c['confidence']:.0%}, "
          f"{c['horizon_months']} months)")
    print(f"\n{c['summary']}")
    for title, key in (("Reasons", "reasons"), ("Risks", "risks"), ("When to buy", "buy_when"),
                       ("When to sell", "sell_when"), ("Data gaps", "data_gaps")):
        if c[key]:
            print(f"\n{title}:")
            for item in c[key]:
                print(f"  - {item}")


def _assistant_call(args: argparse.Namespace) -> int:
    from igs.assistant.calls import make_call

    def run(assistant) -> int:
        from igs import service
        try:
            c = make_call(assistant, args.symbol, args.run_id)
        except service.NotFound as exc:
            print(exc)
            return 1
        print_call(c)
        print(f"\n[AI call {c['call_id']}, run {c['run_id']}, last close "
              f"{c['price_close']} on {c['price_date']}; {c['model']}; ~${c['cost_usd']:.3f}]")
        print(DISCLAIMER)
        return 0
    return _with_assistant(run)


def _assistant_auto_calls(args: argparse.Namespace) -> int:
    """The daily job's automatic calls, now: which stocks are due and why, then the calls
    (not with --dry-run)."""
    from igs import service
    from igs.assistant.calls import due_for_call, scheduled_calls
    from igs.config import load_assistant, load_broker_calls
    from igs.db import connect
    cfg = load_assistant().features.call
    with connect() as conn:
        run = service.resolve_run(conn, args.run_id)
        due = due_for_call(conn, run["run_id"], cfg.top_ranked, cfg.refresh_days,
                           load_broker_calls().cover_days)
    print(f"run {run['run_id']} as of {run['as_of']:%Y-%m-%d}: {len(due)} stocks due for an "
          f"AI call (at most {cfg.max_per_day} automatic calls a day)")
    for d in due:
        print(f"  {d.symbol:<12} {d.reason}")
    if args.dry_run or not due:
        return 0

    def run_calls(assistant) -> int:
        made = scheduled_calls(assistant, run["run_id"])
        for c in made.made:
            print(f"\n{c['symbol']}: {c['action'].upper()} ({c['confidence']:.0%}, "
                  f"{c['horizon_months']} months). {c['summary']}")
        print(f"\n{made}")
        print(DISCLAIMER)
        return 0
    return _with_assistant(run_calls)


def _assistant_verdicts(args: argparse.Namespace) -> int:
    """The AI's verdict on brokers' calls: one stock's now, or every stock waiting (the
    daily job's reviews), or only the list with --dry-run."""
    from igs import service
    from igs.assistant.verdicts import pending, review, scheduled
    from igs.config import load_assistant
    from igs.db import connect
    cfg = load_assistant().features.verdicts
    if args.symbol:
        def one(assistant) -> int:
            try:
                r = review(assistant, args.symbol, args.run_id)
            except service.NotFound as exc:
                print(exc)
                return 1
            print(f"{r['symbol']}"
                  + ("" if r["in_run"] else " (outside the run's universe)")
                  + (", with a Screener.in export" if r["used_screener"] else "") + ":")
            for v in r["verdicts"]:
                print(f"  {v['called_on']:%Y-%m-%d} {v['broker']}, {v['rating']}: "
                      f"{v['verdict'].upper()}. {v['reason']}")
            for g in r["data_gaps"]:
                print(f"  data gap: {g}")
            print(f"~${r['cost_usd']:.3f} · {r['model']}")
            return 0
        return _with_assistant(one)
    with connect() as conn:
        due = pending(conn, cfg.days)
        try:
            run = service.resolve_run(conn, args.run_id)
        except service.NotFound as exc:
            run = None
            problem = str(exc)
    print(f"{len(due)} stocks with brokers' calls of the last {cfg.days} days waiting for the "
          f"AI's verdict (at most {cfg.max_per_day} stocks a day)")
    for d in due:
        print(f"  {d.symbol:<12} {d.reason}")
    if args.dry_run or not due:
        return 0
    if run is None:
        print(f"no verdicts made: {problem}")
        return 1

    def run_reviews(assistant) -> int:
        made = scheduled(assistant, run["run_id"])
        for r in made.made:
            print(f"\n{r['symbol']}:")
            for v in r["verdicts"]:
                print(f"  {v['broker']}, {v['rating']}: {v['verdict'].upper()}. "
                      f"{v['reason']}")
        print(f"\n{made}")
        return 0
    return _with_assistant(run_reviews)


def _assistant_calls(args: argparse.Namespace) -> int:
    from igs.assistant.calls import track_record
    from igs.db import connect
    with connect() as conn:
        record = track_record(conn)
    if not record["calls"]:
        print("no AI calls yet: `igs assistant call SYMBOL`, the stock page, or the daily job")
        return 0
    for c in record["calls"][:args.limit]:
        so_far = c["outcome"]["so_far"]
        result = "" if not so_far or so_far["excess_pct"] is None else \
            f"; since then {so_far['return_pct']:+.1f}% vs Nifty 500 {so_far['nifty500_pct']:+.1f}%"
        print(f"{c['created_at']:%Y-%m-%d} {c['symbol']:<12} {c['action']:<4} "
              f"{c['confidence']:.0%} {c['horizon_months']}m{result}")
    if record["summary"]:
        print("\nrecord (matured calls): action horizon calls right mean-excess")
        for s in record["summary"]:
            right = "-" if s["right_pct"] is None else f"{s['right_pct']:.0f}%"
            print(f"  {s['action']:<4} {s['horizon']:<3} {s['calls']:>4} {right:>5} "
                  f"{s['mean_excess_pct']:+.1f}%")
    else:
        print("\nno call has reached its first horizon (1 month) yet")
    print(DISCLAIMER)
    return 0


def _assistant_read(args: argparse.Namespace) -> int:
    from igs.assistant.announcements import read_new

    def run(assistant) -> int:
        r = read_new(assistant, args.days, args.limit)
        print(f"read {r.read} announcements, stored {r.stored} notes, ~${r.cost_usd:.3f}")
        for issue in r.issues:
            print(f"  {issue}")
        return 0
    return _with_assistant(run)


def _master_rebuild(args: argparse.Namespace) -> int:
    from igs.normalize.master_db import rebuild_instrument_master
    ctx = _context(with_fetcher=False, writer=True)
    with ctx.conn.transaction():
        stats = rebuild_instrument_master(ctx.conn, ctx.dq)
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    print(stats)
    return 0


def _rebuild(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import rebuild_from_raw
    from igs.sync import LOCK_KEY
    # writer=True holds the NSE-check lock (or stops if a check is running): a background
    # check must not load into tables being emptied. It is taken once, so the one unlock
    # below releases it before the command returns.
    ctx = _context(with_fetcher=False, writer=True)
    try:
        counts = rebuild_from_raw(ctx)
        ctx.dq.persist(ctx.conn)
        ctx.conn.commit()
    finally:
        ctx.conn.rollback()
        with ctx.conn.cursor() as cur:
            cur.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
        ctx.conn.commit()
    for k, v in counts.items():
        print(f"{k:32} {v}")
    return 0


def _recon(args: argparse.Namespace) -> int:
    from igs.recon.checks import worst
    from igs.recon.run import run_reconciliation
    ctx = _context(with_fetcher=False)
    results, path = run_reconciliation(ctx.conn, _date(args.start), _date(args.end),
                                       REPO_ROOT / "reports", dq=ctx.dq)
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    for r in results:
        print(f"{r.status.upper():5} {r.name:24} {r.summary}")
    print(f"report: {path}")
    return 0 if worst(results) != "fail" else 1


def screener_check_line(conn, dq, got) -> str:
    """The new export against the app's results filings; a difference is a data-quality
    warning, never silently passed over."""
    from igs import screener
    c = screener.check(conn, got.company_id)
    text = screener.summary(c)
    if c["differ"] or not c["rows"]:
        dq.emit("warn", "screener_differs", f"{got.symbol} Screener.in export {got.file}: {text}",
                fetch_id=got.fetch_id)
    return text


def _import_screener(args: argparse.Namespace) -> int:
    from igs.ingest.manual import import_screener
    ctx = _context(with_fetcher=False, writer=True)
    bad = 0
    for path in args.path:
        try:
            got = import_screener(ctx.conn, ctx.store, Path(path), ctx.dq, args.nse, args.bse)
        except ValueError as exc:            # not an export, or no single company matches
            ctx.conn.rollback()
            print(f"FAIL {exc}")
            bad += 1
            continue
        ctx.conn.commit()
        print(f"ok   {got}")
        if got.company_id and got.symbol and not got.already:
            text = screener_check_line(ctx.conn, ctx.dq, got)
            print(f"     {text}")
    ctx.dq.persist(ctx.conn)
    ctx.conn.commit()
    print("Screener.in figures are tier 3: they check the app's results figures and fill "
          "gaps in what the AI reads, and never feed the scores (`igs screener check SYMBOL` "
          "compares them)")
    return 1 if bad else 0


def _import_yfinance(args: argparse.Namespace) -> int:
    from igs.ingest.manual import import_yfinance
    ctx = _context(with_fetcher=False, writer=True)
    n = import_yfinance(ctx.conn, ctx.store, args.symbols, _date(args.start), _date(args.end))
    print(f"loaded {n} fallback price rows (tier 3, UNVERIFIED)")
    return 0


def _replay_documents(args: argparse.Namespace) -> int:
    from igs.ingest.documents import replay_documents
    ctx = _context(with_fetcher=False, writer=True)
    return _finish(ctx, replay_documents(ctx, args.kind, args.limit))


def _ingest_documents(args: argparse.Namespace) -> int:
    from igs.ingest.jobs import ingest_documents, ingest_insider_documents
    ctx = _context(writer=True)
    if args.kind == "insider_trading":
        return _finish(ctx, ingest_insider_documents(ctx, args.limit))
    return _finish(ctx, ingest_documents(ctx, args.kind, args.limit))


def _validate_fundamentals(args: argparse.Namespace) -> int:
    from igs.recon.checks import worst
    from igs.xbrl.report import validate_fundamentals
    ctx = _context(with_fetcher=False)
    results, path = validate_fundamentals(ctx.conn, REPO_ROOT / "reports")
    for r in results:
        print(f"{r.status.upper():5} {r.name:24} {r.summary}")
    print(f"report: {path}")
    return 0 if worst(results) != "fail" else 1


def ic_status_path() -> Path:
    return Path(os.environ.get("IGS_IC_STATUS", REPO_ROOT / "data" / "backtest" /
                               "ic_status.json"))


def _backtest(args: argparse.Namespace) -> int:
    from igs.backtest.run import run_configured
    from igs.pit.gate import GateError
    ctx = _context(with_fetcher=False)
    try:
        results = run_configured(ctx.conn, _date(args.start), _date(args.end),
                                 REPO_ROOT / "reports", ic_status_path())
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for freq, (res, path) in results.items():
        kept = res.ic_status.filter(res.ic_status["verdict"] == "KEEP").height \
            if res.ic_status.height else 0
        print(f"{freq:9} {len(res.dates)} rebalances, {kept} factors KEEP -> {path}")
    print(f"IC status for production scoring: {ic_status_path()}")
    return 0


def _score(args: argparse.Namespace) -> int:
    from igs.config import load_universe
    from igs.pit.gate import GateError
    from igs.score.persist import tier_counts
    from igs.score.pipeline import score_from_db, universe_summary
    from igs.timeutil import end_of_day_ist, utc_now
    ctx = _context(with_fetcher=False)
    day = _date(args.as_of) if args.as_of else utc_now().date()
    try:
        run_id, run = score_from_db(ctx.conn, end_of_day_ist(day), ic_status_path())
    except GateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    u = universe_summary(run.universe, load_universe().min_filing_quarters)
    print(f"run {run_id} as of {run.as_of:%Y-%m-%d %H:%M} UTC: {u['included']} names in "
          f"universe, of {u['seen']} with prices")
    for reason, n in u["excluded"].items():
        print(f"  left out: {reason}: {n}")
    if u["shares_from_capital"]:
        print(f"  market cap from paid-up capital / face value (no shareholding filing "
              f"loaded): {u['shares_from_capital']}")
    for tier, n in sorted(tier_counts(run.results).items()):
        print(f"  {tier:16} {n}")
    for issue in run.dq.issues:
        print(f"  [{issue.severity}] {issue.message}")
    return 0


def _migrate_on_start() -> None:
    """Bring the database up to this version of the code before the app opens, so an update
    (git pull) needs no separate `igs db migrate`. A database that can't be reached is left
    to the app, which explains the problem on its first page."""
    from igs.db import connect, migrate
    try:
        with connect() as conn:
            done = migrate(conn)
    except Exception as exc:  # noqa: BLE001 - reported, and the app shows it again
        print(f"The database was not updated at start: {str(exc).splitlines()[0]}")
        return
    if done:
        print(f"Database updated for this version of the app: {', '.join(done)}")


def _port_in_use(host: str, port: int) -> bool:
    import socket
    with socket.socket() as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def _ui(args: argparse.Namespace) -> int:
    import time

    from igs.config import load_sync
    from igs.sync import start_background_sync
    if _port_in_use(args.host, args.port):
        print(f"Port {args.port} is in use, most likely by the app still running from before "
              "(an old copy keeps its old code). Stop it: Ctrl+C in the terminal where it "
              "runs, or `pkill -f \"streamlit run\"` (Windows: close that window). Then run "
              f"`uv run igs ui` again. To run a second copy instead: `uv run igs ui --port "
              f"{args.port + 1}`.", file=sys.stderr)
        return 1
    _migrate_on_start()
    app = Path(__file__).resolve().parent / "ui" / "app.py"
    # Run from the repo root so .streamlit/config.toml (light/dark accents) is used.
    ui = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(app),
                           "--server.address", args.host, "--server.port", str(args.port)],
                          cwd=REPO_ROOT)
    cfg = load_sync()
    if not args.no_sync:
        # Check NSE for new files when the app starts and every interval_hours while it
        # runs (each check skips itself if another ran within min_interval_minutes).
        print(f"Checking NSE for new files {'now and ' if cfg.check_on_ui_start else ''}"
              f"every {cfg.interval_hours:g} h while the app runs (logs/sync.log; "
              "--no-sync turns this off).")
    next_at = time.monotonic() + (0 if cfg.check_on_ui_start else cfg.interval_hours * 3600)
    trigger, check = "startup", None
    try:
        if args.no_sync:
            ui.wait()
        while ui.poll() is None:
            if time.monotonic() >= next_at and (check is None or check.poll() is not None):
                check = start_background_sync(trigger)
                trigger = "interval"
                next_at = time.monotonic() + cfg.interval_hours * 3600
            time.sleep(5)
    except KeyboardInterrupt:
        ui.wait()
    return ui.returncode or 0


def _sync(args: argparse.Namespace) -> int:
    from igs.config import load_sync
    from igs.sync import run_sync
    ctx = _context()
    rep = run_sync(ctx, args.trigger, load_sync(), force=args.force)
    if rep.skipped:
        print(f"skipped: {rep.skipped}")
        return 0
    for name, status, summary in rep.steps:
        print(f"{status.upper():6} {name:28} {summary}")
    print(f"check {rep.sync_id} ({args.trigger}): {rep.status}, {rep.new_rows} new rows")
    return 1 if rep.status == "failed" else 0


def _api(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("igs.api.app:app", host=args.host, port=args.port)
    return 0


def _daily(args: argparse.Namespace) -> int:
    from igs.daily import run_daily
    from igs.timeutil import IST, utc_now
    ctx = _context()
    day = _date(args.date) if args.date else utc_now().astimezone(IST).date()
    rep = run_daily(ctx, day, ic_status_path(), REPO_ROOT / "reports")
    for name, status, summary in rep.steps:
        print(f"{status.upper():6} {name:28} {summary}")
    print(f"run {rep.run_id}, {rep.alerts_sent} alerts")
    return 1 if rep.failed else 0


def _alerts(args: argparse.Namespace) -> int:
    from igs.daily import send_alerts
    from igs.service import resolve_run
    if args.test_whatsapp:
        from igs.alerts import whatsapp
        from igs.config import load_alerts
        try:
            service = whatsapp.send_test(load_alerts().whatsapp)
        except whatsapp.WhatsAppError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Test message sent through {service}; check WhatsApp.")
        return 0
    if args.test_telegram:
        from igs.alerts import delivery
        try:
            delivery.send_telegram_test()
        except delivery.TelegramError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("Test message sent; check Telegram.")
        return 0
    ctx = _context(with_fetcher=False)
    run = resolve_run(ctx.conn, args.run_id)
    print(send_alerts(ctx.conn, run["run_id"], REPO_ROOT / "reports"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="igs", description="IndiaGrowthScreener. " + DISCLAIMER)
    p.add_argument("-v", "--verbose", action="store_true")
    groups = p.add_subparsers(dest="group", required=True)

    db = groups.add_parser("db").add_subparsers(dest="cmd", required=True)
    db.add_parser("migrate", help="apply pending SQL migrations").set_defaults(fn=_db_migrate)
    db.add_parser("status", help="what is loaded: rows per table, latest price day and filing"
                  ).set_defaults(fn=_db_status)

    raw = groups.add_parser("raw").add_subparsers(dest="cmd", required=True)
    raw.add_parser("reindex", help="rebuild raw_payload from fetch records on disk"
                   ).set_defaults(fn=_raw_reindex)

    src = groups.add_parser("sources").add_subparsers(dest="cmd", required=True)
    src.add_parser("list", help="registry and verification status").set_defaults(fn=_sources_list)
    ver = src.add_parser("verify", help="fetch a sample from each endpoint and record it")
    ver.add_argument("ids", nargs="*", help="source ids (default: all)")
    ver.set_defaults(fn=_sources_verify)

    ing = groups.add_parser("ingest").add_subparsers(dest="cmd", required=True)
    st = ing.add_parser("static", help="fetch and load static sources (masters, lists)")
    st.add_argument("ids", nargs="+")
    st.set_defaults(fn=_ingest_static)
    pr = ing.add_parser("prices", help="bhavcopy + delivery + index closes for a date range")
    pr.add_argument("--start", required=True)
    pr.add_argument("--end", required=True)
    pr.add_argument("--no-delivery", action="store_true")
    pr.set_defaults(fn=_ingest_prices)
    rg = ing.add_parser("range", help="date-range sources (corporate actions, announcements)")
    rg.add_argument("source")
    rg.add_argument("--start", required=True)
    rg.add_argument("--end", required=True)
    rg.set_defaults(fn=_ingest_range)
    sy = ing.add_parser("symbols", help="per-symbol sources (default: all current symbols)")
    sy.add_argument("source")
    sy.add_argument("symbols", nargs="*")
    sy.set_defaults(fn=_ingest_symbols)
    pg = ing.add_parser("pages", help="paged listings, newest first (Integrated Filing)")
    pg.add_argument("source")
    pg.add_argument("--from-page", type=int, default=None,
                    help="page to start from (default: the source's first page)")
    pg.add_argument("--max-pages", type=int, default=None,
                    help="page limit (default: the source's max_pages)")
    pg.add_argument("--backfill", action="store_true",
                    help="do not stop at the first page without new rows")
    pg.set_defaults(fn=_ingest_pages)

    dc = ing.add_parser("documents", help="fetch and load XBRL documents from listings")
    dc.add_argument("kind", choices=["financial_results", "shareholding", "insider_trading"])
    dc.add_argument("--limit", type=int)
    dc.set_defaults(fn=_ingest_documents)
    replay = ing.add_parser("replay-documents", help="retry downloaded XBRL without network")
    replay.add_argument("kind", choices=["financial_results", "shareholding"])
    replay.add_argument("--limit", type=int)
    replay.set_defaults(fn=_replay_documents)

    val = groups.add_parser("validate").add_subparsers(dest="cmd", required=True)
    val.add_parser("fundamentals", help="step 2 sign-off report (20 hand-checked companies)"
                   ).set_defaults(fn=_validate_fundamentals)

    master = groups.add_parser("master").add_subparsers(dest="cmd", required=True)
    master.add_parser("rebuild", help="rebuild instrument master from loaded prices"
                      ).set_defaults(fn=_master_rebuild)
    groups.add_parser("rebuild", help="truncate derived tables and replay the raw store"
                      ).set_defaults(fn=_rebuild)
    rc = groups.add_parser("recon", help="reconciliation report for a date range")
    rc.add_argument("--start", required=True)
    rc.add_argument("--end", required=True)
    rc.set_defaults(fn=_recon)

    imp = groups.add_parser("import").add_subparsers(dest="cmd", required=True)
    sc = imp.add_parser("screener", help="Screener.in exports you downloaded: a company's "
                        "Export to Excel workbook, or a screen's CSV (tier 3)")
    sc.add_argument("path", nargs="+")
    sc.add_argument("--nse", help="the company's NSE symbol, if its name doesn't match one "
                    "company")
    sc.add_argument("--bse")
    sc.set_defaults(fn=_import_screener)
    yf = imp.add_parser("yfinance", help="fallback price history (tier 3, unverified)")
    yf.add_argument("symbols", nargs="+")
    yf.add_argument("--start", required=True)
    yf.add_argument("--end", required=True)
    yf.set_defaults(fn=_import_yfinance)

    bt = groups.add_parser("backtest", help="walk-forward backtest + IC report (gated)")
    bt.add_argument("--start", required=True)
    bt.add_argument("--end", required=True)
    bt.set_defaults(fn=_backtest)

    scr = groups.add_parser("score", help="rank the universe as of a date (gated)")
    scr.add_argument("--as-of", help="YYYY-MM-DD (default: today); signals at 23:59:59 IST")
    scr.set_defaults(fn=_score)
    api = groups.add_parser("api", help="serve the HTTP API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.set_defaults(fn=_api)
    ui = groups.add_parser("ui", help="launch the Streamlit UI (needs the 'ui' group)")
    ui.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default: this computer only)")
    ui.add_argument("--port", type=int, default=8501)
    ui.add_argument("--no-sync", action="store_true",
                    help="do not check NSE for new files while the app runs")
    ui.set_defaults(fn=_ui)
    sy = groups.add_parser("sync", help="check NSE for new files and load them (no scoring)")
    sy.add_argument("--trigger", default="manual",
                    choices=["manual", "startup", "interval", "timer"],
                    help="what started this check (shown in the app)")
    sy.add_argument("--force", action="store_true",
                    help="run even if the last check was within min_interval_minutes")
    sy.set_defaults(fn=_sync)

    ak = groups.add_parser("ask", help="ask the optional research assistant about a run")
    ak.add_argument("question", nargs="+")
    ak.add_argument("--run-id", type=int)
    ak.set_defaults(fn=_ask)
    asst = groups.add_parser("assistant", help="the optional research assistant (Claude API)"
                             ).add_subparsers(dest="cmd", required=True)
    asst.add_parser("status", help="settings, credentials and recent spend"
                    ).set_defaults(fn=_assistant_status)
    ab = asst.add_parser("brief", help="plain-language brief of one stock's result")
    ab.add_argument("symbol")
    ab.add_argument("--run-id", type=int)
    ab.add_argument("--refresh", action="store_true", help="write a new brief")
    ab.set_defaults(fn=_assistant_brief)
    ac = asst.add_parser("call", help="the AI's buy / hold / sell call on one stock, with "
                         "when to buy and when to sell")
    ac.add_argument("symbol")
    ac.add_argument("--run-id", type=int)
    ac.set_defaults(fn=_assistant_call)
    aa = asst.add_parser("auto-calls", help="the daily job's automatic AI calls, now: the "
                         "stocks with new data since their last call, then the calls")
    aa.add_argument("--run-id", type=int)
    aa.add_argument("--dry-run", action="store_true", help="only list the stocks due, and why")
    aa.set_defaults(fn=_assistant_auto_calls)
    av = asst.add_parser("verdicts", help="the AI's verdict on brokers' calls: on every "
                         "stock with calls waiting for one (the daily job's reviews), or on "
                         "one stock's calls now")
    av.add_argument("symbol", nargs="?")
    av.add_argument("--run-id", type=int)
    av.add_argument("--dry-run", action="store_true",
                    help="only list the stocks waiting, and why")
    av.set_defaults(fn=_assistant_verdicts)
    al = asst.add_parser("calls", help="past AI calls and how they did against the Nifty 500")
    al.add_argument("--limit", type=int, default=50)
    al.set_defaults(fn=_assistant_calls)
    ar = asst.add_parser("read-announcements", help="note category, materiality and "
                         "concerns of new announcements")
    ar.add_argument("--days", type=int)
    ar.add_argument("--limit", type=int)
    ar.set_defaults(fn=_assistant_read)

    brk = groups.add_parser("brokers", help="brokers' buy / hold / sell calls, a second "
                            "opinion for the AI's calls").add_subparsers(dest="cmd",
                                                                          required=True)
    bc = brk.add_parser("collect", help="fetch the news feeds, record calls stated in "
                        "headlines, have the AI read other articles that mention a rating "
                        "or target, and read each article's tone for the sentiment score")
    bc.add_argument("--force", action="store_true", help="ignore the feed polling interval")
    bc.add_argument("--no-read", action="store_true", help="fetch only; no AI")
    bc.set_defaults(fn=_brokers_collect)
    ba = brk.add_parser("add", help="record a call you read elsewhere, e.g. on Moneycontrol")
    ba.add_argument("symbol")
    ba.add_argument("--broker", required=True)
    ba.add_argument("--call", required=True, choices=["buy", "hold", "sell"])
    ba.add_argument("--target", type=float, help="target price, Rs per share")
    ba.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ba.add_argument("--rating", help="the rating as written, e.g. Accumulate")
    ba.add_argument("--url")
    ba.add_argument("--note")
    ba.add_argument("--trading", action="store_true",
                    help="a short-term trading idea rather than a research rating")
    ba.set_defaults(fn=_brokers_add)
    bi = brk.add_parser("import", help="read older calls from a Moneycontrol stock news "
                        "page saved from your browser (text or HTML)")
    bi.add_argument("file")
    bi.add_argument("--date", help="date for headlines shown without one (default: today)")
    bi.set_defaults(fn=_brokers_import)
    bl = brk.add_parser("list", help="recent broker calls, with the AI's latest call")
    bl.add_argument("--days", type=int, default=30)
    bl.set_defaults(fn=_brokers_list)
    bm = brk.add_parser("match", help="list the calls not matched to a company, or link one "
                        "(by its id) to a company, so the AI can judge it")
    bm.add_argument("id", type=int, nargs="?")
    bm.add_argument("symbol", nargs="?")
    bm.add_argument("--days", type=int, default=30)
    bm.set_defaults(fn=_brokers_match)

    scr = groups.add_parser("screener", help="Screener.in exports you downloaded (import "
                            "them with `igs import screener FILE...`)"
                            ).add_subparsers(dest="cmd", required=True)
    sk = scr.add_parser("check", help="a stock's latest export against the app's results "
                        "filings")
    sk.add_argument("symbol")
    sk.set_defaults(fn=_screener_check)
    sw = scr.add_parser("wanted", help="stocks with brokers' calls whose data is thin, with "
                        "the Screener.in page to export each from")
    sw.add_argument("--days", type=int, default=30)
    sw.set_defaults(fn=_screener_wanted)

    news = groups.add_parser("news", help="geopolitical news and AI rating inputs")
    news_sub = news.add_subparsers(dest="news_command", required=True)
    nc = news_sub.add_parser("collect", help="automatically collect public RSS news")
    nc.add_argument("--force", action="store_true", help="ignore feed polling interval")
    nc.set_defaults(fn=_news_collect)
    np = news_sub.add_parser("process", help="collect and assess news for scheduled runs")
    np.add_argument("--limit", type=int, default=10)
    np.add_argument("--force", action="store_true", help="ignore feed polling interval")
    np.set_defaults(fn=_news_process)
    ni = news_sub.add_parser("import", help="import sourced news and company exposures from JSON")
    ni.add_argument("path")
    ni.set_defaults(fn=_news_import)
    na = news_sub.add_parser("assess", help="use the configured AI to assess recent imported news")
    na.add_argument("--limit", type=int, default=10)
    na.set_defaults(fn=_news_assess)

    dl = groups.add_parser("daily", help="ingest, score and send alerts (for cron)")
    dl.add_argument("--date", help="YYYY-MM-DD (default: today, IST)")
    dl.set_defaults(fn=_daily)
    al = groups.add_parser("alerts", help="evaluate and deliver alerts for a score run")
    al.add_argument("--run-id", type=int)
    al.add_argument("--test-whatsapp", action="store_true",
                    help="send a sample WhatsApp message to check the set-up, and stop")
    al.add_argument("--test-telegram", action="store_true",
                    help="send a sample Telegram message to check the set-up, and stop")
    al.set_defaults(fn=_alerts)

    gate = groups.add_parser("gate").add_subparsers(dest="cmd", required=True)
    gate.add_parser("run", help="run look-ahead tests and record a pass").set_defaults(fn=_gate_run)
    gate.add_parser("check", help="is the recorded pass valid for current code?"
                    ).set_defaults(fn=_gate_check)
    return p


def main(argv: list[str] | None = None) -> int:
    import psycopg

    from igs import envfile
    from igs.db import connection_help, database_url
    from_shell = "IGS_DATABASE_URL" in os.environ
    env_path = envfile.default_path()
    from_file = "IGS_DATABASE_URL" in envfile.load(env_path)
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Data-quality notes (e.g. XBRL elements the mapping does not use, one long line per
    # document) are stored in dq_issue and shown on the Data quality page; on the console
    # they would bury the warnings and errors, so they print only with -v.
    logging.getLogger("igs.dq").setLevel(logging.NOTSET if args.verbose else logging.WARNING)
    from igs.ingest.http import FetchError
    from igs.ingest.jobs import MasterNotBuilt
    try:
        return args.fn(args)
    except MasterNotBuilt as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 2
    except IngestionBusy as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except FetchError as exc:
        print(f"Stopped: {exc}\nNSE refuses requests from this address for a while after "
              "bursts; try again later (a scheduled `igs sync` will).", file=sys.stderr)
        return 2
    except psycopg.OperationalError as exc:
        source = ("the IGS_DATABASE_URL environment variable, which overrides .env"
                  if from_shell else str(env_path) if from_file
                  else f"the built-in default: no IGS_DATABASE_URL in the environment or "
                       f"in {env_path}")
        print(connection_help(exc, database_url(), source), file=sys.stderr)
        return 2
    except psycopg.errors.UndefinedTable as exc:
        print(f"The database is missing a table ({str(exc).splitlines()[0]}). The app was "
              "updated since the database was last migrated: run `uv run igs db migrate`.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
