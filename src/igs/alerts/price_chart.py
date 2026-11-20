"""The price chart sent after each call or ranking message on Telegram: one image with the
last week, month and year (call_messages.charts in config/alerts.yaml).

Daily closes come from the exchange's bhavcopies (price_eod), adjusted for splits and
bonuses through the exchange's previous-close chain and shown at today's price level. The
week uses the intraday scan's 5-minute candles when it has at least three of the last
five sessions, else daily closes. Colours and marks follow the app's chart tokens: one
series, so no legend; text in ink colours, never the line's.
"""
from __future__ import annotations

import datetime as dt
import io

from igs.timeutil import IST

SURFACE, INK, INK_2, GRID, BASELINE = '#fcfcfb', '#0b0b0b', '#52514e', '#e1e0d9', '#c3c2b7'
SERIES = '#2a78d6'
PANELS = (('1 week', 7), ('1 month', 31), ('1 year', 366))   # label, calendar days
WIDTH_PX, HEIGHT_PX, DPI = 1000, 1080, 125


def daily(conn, company_id: int, today: dt.date) -> list[tuple[dt.date, float]]:
    """(day, close) over the last year, adjusted for splits and bonuses, at today's level."""
    from igs.assistant.calls import _price_rows, price_index
    rows = _price_rows(conn, company_id, today - dt.timedelta(days=372), today)
    levels = price_index(rows)
    if not levels:
        return []
    scale = rows[-1][1] / levels[-1][1]
    return [(day, level * scale) for day, level in levels]


def intraday(conn, company_id: int, today: dt.date) -> list[tuple[dt.datetime, float]]:
    """(time, close) of the 5-minute candles of the last five sessions the intraday scan
    kept for this stock, or [] if it has fewer than three of them."""
    rows = conn.execute("""select h.session_date, h.candles from intraday_history h
        join security_identifier i on i.id_type = 'ISIN'
         and h.instrument_key = 'NSE_EQ|' || i.id_value
        join security s using (security_id)
        where s.company_id = %s and h.session_date between %s and %s
        order by h.session_date desc limit 5""",
                        (company_id, today - dt.timedelta(days=10), today)).fetchall()
    if len(rows) < 3:
        return []
    out = []
    for _, candles in sorted(rows, key=lambda r: r[0]):
        out += [(dt.datetime.fromisoformat(c['start']), float(c['close'])) for c in candles]
    return out


def _rupees(v: float) -> str:
    """₹ with Indian digit grouping; paise below ₹100."""
    if abs(v) < 100:
        return f'₹{v:,.2f}'
    s = str(int(round(abs(v))))
    if len(s) > 3:
        head, groups = s[:-3], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ','.join([head, *groups, s[-3:]])
    return ('-' if v < 0 else '') + '₹' + s


def _change(points) -> str:
    first, last = points[0][1], points[-1][1]
    pct = (last / first - 1) * 100
    return f"{'▲' if pct >= 0 else '▼'} {pct:+.1f}%   {_rupees(first)} → {_rupees(last)}"


def _style(ax, rupees) -> None:
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    ax.set_facecolor(SURFACE)
    for side in ('top', 'right', 'left'):
        ax.spines[side].set_visible(False)
    ax.spines['bottom'].set_color(BASELINE)
    ax.grid(axis='y', color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.yaxis.set_major_locator(MaxNLocator(4))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: rupees(v)))


def _sessions(ax, points, markers: bool) -> tuple:
    """Prices on a session axis (no gaps for nights and weekends), one label a session."""
    x = list(range(len(points)))
    ax.plot(x, [p for _, p in points], color=SERIES, linewidth=1.4, marker='o' if markers
            else None, markersize=3.5, solid_joinstyle='round', solid_capstyle='round')
    day = [(t.astimezone(IST).date() if isinstance(t, dt.datetime) else t) for t, _ in points]
    starts = [i for i in range(len(day)) if i == 0 or day[i] != day[i - 1]]
    ends = starts[1:] + [len(day)]
    if not markers:
        for s in starts[1:]:
            ax.axvline(s - 0.5, color=GRID, linewidth=0.6)
    ax.set_xticks([(s + e - 1) / 2 for s, e in zip(starts, ends, strict=True)])
    ax.set_xticklabels([f'{day[s]:%a %d %b}' for s in starts])
    ax.set_xlim(-0.6 if markers else -1, len(points) - (0.4 if markers else 0))
    return x[-1], points[-1][1]


def _dates(ax, points, fmt: str, locator) -> tuple:
    from matplotlib.dates import DateFormatter
    xs = [d for d, _ in points]
    ax.plot(xs, [p for _, p in points], color=SERIES, linewidth=1.4,
            solid_joinstyle='round', solid_capstyle='round')
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(DateFormatter(fmt))
    return xs[-1], points[-1][1]


def render(symbol: str, name: str | None, days: list[tuple[dt.date, float]],
           week: list[tuple[dt.datetime, float]] | None = None) -> bytes | None:
    """The PNG, or None with fewer than two closes."""
    if len(days) < 2:
        return None
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.dates import MonthLocator, WeekdayLocator
    from matplotlib.figure import Figure

    fig = Figure(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    fig.text(0.04, 0.975, symbol + (f' · {name}' if name else ''), ha='left', va='top',
             fontsize=13, fontweight='bold', color=INK)
    last_day, last_close = days[-1]
    fig.text(0.04, 0.945, f'Last close {_rupees(last_close)} on {last_day:%d %b %Y}. '
             'Adjusted for splits and bonuses.', ha='left', va='top', fontsize=9,
             color=INK_2)
    grid = fig.add_gridspec(3, 1, left=0.12, right=0.95, top=0.88, bottom=0.05, hspace=0.55)
    for row, (label, span) in enumerate(PANELS):
        ax = fig.add_subplot(grid[row, 0])
        _style(ax, _rupees)
        cut = last_day - dt.timedelta(days=span)
        points = [(d, p) for d, p in days if d >= cut]
        if len(points) < 2:
            points = days[-2:]
        if span == 7 and week:
            points, note = week, '5-minute prices'
            end = _sessions(ax, points, markers=False)
            # The week's change runs from the close before its first session.
            first = week[0][0].astimezone(IST).date()
            before = [(d, p) for d, p in days if d < first]
            points = before[-1:] + week
        elif span == 7:
            note = 'daily closes'
            end = _sessions(ax, points, markers=True)
        elif span == 31:
            note = 'daily closes'
            end = _dates(ax, points, '%d %b', WeekdayLocator(byweekday=0))
        else:
            note = 'daily closes'
            if points[0][0] > last_day - dt.timedelta(days=340):
                note = f'daily closes since {points[0][0]:%d %b %Y}'
            end = _dates(ax, points, '%b %Y', MonthLocator(bymonth=(1, 4, 7, 10)))
        ax.plot([end[0]], [end[1]], marker='o', markersize=4.5, color=SERIES,
                markeredgecolor=SURFACE, markeredgewidth=1.2)
        # The panel's name on the left and its change on the right, on one line.
        ax.annotate(f'{label}  ·  {note}', (0, 1), xycoords='axes fraction',
                    xytext=(0, 9), textcoords='offset points', ha='left', va='bottom',
                    fontsize=10.5, fontweight='bold', color=INK)
        ax.annotate(_change(points), (1, 1), xycoords='axes fraction', xytext=(0, 9),
                    textcoords='offset points', ha='right', va='bottom', fontsize=9.5,
                    color=INK)
    out = io.BytesIO()
    fig.savefig(out, format='png', facecolor=SURFACE)
    return out.getvalue()


def chart(conn, company_id: int, today: dt.date | None = None) -> tuple[bytes, str] | None:
    """(PNG, caption) for a company, or None without two closes in the year to its latest
    close on or before `today`."""
    today = today or dt.datetime.now(IST).date()
    row = conn.execute("""select c.name, (select i.id_value from security s
            join security_identifier i using (security_id)
            where s.company_id = c.company_id and i.id_type = 'NSE_SYMBOL'
              and (i.valid_to is null or i.valid_to > %s)
            order by i.valid_from desc limit 1)
        from company c where c.company_id = %s""", (today, company_id)).fetchone()
    if not row:
        return None
    name, symbol = row
    # The year to the stock's latest close: today's when prices are current.
    last = conn.execute("""select max(p.trade_date) from price_eod p
        join security_identifier si on si.id_type = 'ISIN' and si.id_value = p.isin
        join security s on s.security_id = si.security_id
        where s.company_id = %s and p.exchange = 'NSE' and p.trade_date <= %s""",
                        (company_id, today)).fetchone()[0]
    if last is None:
        return None
    png = render(symbol or name, name if symbol else None, daily(conn, company_id, last),
                 intraday(conn, company_id, last))
    if png is None:
        return None
    return png, f'{symbol or name}: price over the last week, month and year'


def company_of(conn, symbol: str) -> int | None:
    """The company an NSE symbol belongs to today."""
    row = conn.execute("""select s.company_id from security_identifier i
        join security s using (security_id) where i.id_type = 'NSE_SYMBOL'
        and i.id_value = %s and (i.valid_to is null or i.valid_to > current_date)
        order by i.valid_from desc limit 1""", (symbol,)).fetchone()
    return row[0] if row else None
