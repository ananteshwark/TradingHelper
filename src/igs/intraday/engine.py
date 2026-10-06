"""Closed-candle signals. No future bars, synthetic volume, or probability claims."""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from igs.timeutil import IST, require_aware

BAR = dt.timedelta(minutes=5)
# Farthest a close may be from VWAP, in five-minute true ranges (the stop is 1.5).
# A starting point, not fitted.
MAX_VWAP_DISTANCE_ATR = 3.0


@dataclass(frozen=True)
class Candle:
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self):
        require_aware(self.start)
        numbers = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(x) for x in numbers) or self.volume < 0:
            raise ValueError('Invalid candle values')
        if not 0 < self.low <= min(self.open, self.close) <= max(
                self.open, self.close) <= self.high:
            raise ValueError('Invalid candle OHLC')
        t = self.start.astimezone(IST)
        if t.second or t.microsecond or t.minute % 5:
            raise ValueError('Expected five-minute candle boundary')


def levels(reference, direction, risk, tick=None):
    """Stop and target for a call at `reference`. With the exchange tick (rupees), both are
    whole ticks, as orders must be: the stop rounded away from the entry, the target
    toward it (down for a buy, up for a sell). Without it, to the paisa."""
    stop, target = reference - direction * risk, reference + direction * risk * 2
    if tick is None:
        return round(stop, 2), round(target, 2)
    return (float(to_tick(stop, tick, direction)), float(to_tick(target, tick, direction)))


def to_tick(price, tick, direction):
    """`price` in whole ticks: rounded down for a buy (direction 1), up for a sell.
    Binary float noise (101.35000000000001) is dropped first, so it cannot add a tick."""
    tick = Decimal(str(tick))
    exact = Decimal(str(price)).quantize(Decimal('0.000001'))
    whole = (exact / tick).to_integral_value(
        rounding=ROUND_FLOOR if direction == 1 else ROUND_CEILING)
    return whole * tick


def trading_window(now):
    now = require_aware(now).astimezone(IST)
    # Fresh current-session bars are additionally required, covering holidays/closures.
    return now.weekday() < 5 and dt.time(9, 30) <= now.time() < dt.time(15, 15)


def session(bars, now):
    now = require_aware(now).astimezone(IST)
    closed = {b.start: b for b in bars if b.start + BAR <= now
              and b.start.astimezone(IST).date() == now.date()
              and dt.time(9, 15) <= b.start.astimezone(IST).time() < dt.time(15, 30)}
    return sorted(closed.values(), key=lambda b: b.start)


def evaluate(bars, history, now, benchmark=(), evidence=(), tick=None, price_band=None):
    """A setup lasts at most ten minutes from its last closed candle, never overnight.
    `tick` is the instrument's exchange tick in rupees; levels are rounded to it.
    `price_band` is today's NSE band (intraday.price_bands.price_band); None skips it."""
    now = require_aware(now).astimezone(IST)
    # Only context received, published and assessed by this scan may influence it.
    evidence = [e for e in evidence if e.get('known_at') and e.get('published_at')
                and require_aware(dt.datetime.fromisoformat(e['known_at'])) <= now
                and now - dt.timedelta(days=3) <= require_aware(
                    dt.datetime.fromisoformat(e['published_at'])) <= now]
    result = {'action': 'wait', 'reason': '', 'evidence': list(evidence)}

    def wait(reason):
        result['reason'] = reason
        return result

    if not trading_window(now):
        return wait('Outside 09:30–15:15 IST entry window')
    today = session(bars, now)
    if len(today) < 6:
        return wait('Need six closed five-minute candles')
    last = today[-1]
    end = last.start + BAR
    result.update(candle_end=end.isoformat(), reference=last.close,
                  expires_at=min(end + dt.timedelta(minutes=10), dt.datetime.combine(
                      now.date(), dt.time(15, 15), IST)).isoformat())
    if now - end > dt.timedelta(minutes=5):
        return wait('Stale Upstox candles')
    expected = int((last.start - dt.datetime.combine(
        now.date(), dt.time(9, 15), IST)) / BAR) + 1
    if len(today) != expected:
        return wait('Incomplete current-session candles')
    # Compare this exact time bucket with previous sessions, not whole-day volume.
    prior = {}
    for b in history:
        t = b.start.astimezone(IST)
        if (now.date() - dt.timedelta(days=30) <= t.date() < now.date()
                and t.time() == last.start.astimezone(IST).time()):
            prior[t.date()] = b.volume
    volumes = [prior[d] for d in sorted(prior)[-20:]]
    if len(volumes) < 5 or statistics.median(volumes) <= 0:
        return wait('Need five prior sessions of matching-time volume')
    rvol = last.volume / statistics.median(volumes)
    total_volume = sum(b.volume for b in today)
    if total_volume <= 0:
        return wait('No traded volume')
    vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in today) / total_volume
    turnover = sum(b.close * b.volume for b in today)
    momentum = (last.close / today[-4].close - 1) * 100
    opening_high = max(b.high for b in today[:3])
    opening_low = min(b.low for b in today[:3])
    pairs = list(zip(today[:-1], today[1:], strict=True))[-14:]
    atr = statistics.mean(max(b.high - b.low, abs(b.high - a.close),
                              abs(b.low - a.close)) for a, b in pairs)
    result.update(rvol=round(rvol, 2), momentum_pct=round(momentum, 2),
                  vwap=round(vwap, 2), turnover_cr=round(turnover / 1e7, 2),
                  baseline_sessions=len(volumes), baseline_volume=statistics.median(volumes),
                  candle_volume=last.volume, opening_high=opening_high, opening_low=opening_low,
                  atr=atr, rule_version='intraday-v3')
    market = session(benchmark, now)
    if not market or now - (market[-1].start + BAR) > dt.timedelta(minutes=5):
        return wait('Fresh Nifty 50 benchmark unavailable')
    if market[0].start.astimezone(IST).time() != dt.time(9, 15):
        return wait('Nifty 50 opening candle unavailable')
    market_return = (market[-1].close / market[0].open - 1) * 100
    result['market_return_pct'] = round(market_return, 2)
    if turnover < 1e7 or last.close * last.volume < 1e6:
        return wait('Liquidity below ₹1 crore/session or ₹10 lakh/latest candle')
    if rvol < 1.8:
        return wait('Volume jump below 1.8× same-time median')
    direction = (1 if momentum >= .3 and last.close > max(vwap, opening_high)
                 and market_return >= -.2 else -1 if momentum <= -.3
                 and last.close < min(vwap, opening_low) and market_return <= .2 else 0)
    if not direction:
        return wait('Momentum, VWAP, opening range and market direction do not align')
    risk = max(atr * 1.5, last.close * .004)
    if risk / last.close > .02:
        return wait('Volatility requires a stop wider than 2%')
    # A close far from VWAP is a chase: a return to VWAP would cost twice the stop.
    if abs(last.close - vwap) > MAX_VWAP_DISTANCE_ATR * atr:
        return wait(f'Price is over {MAX_VWAP_DISTANCE_ATR:g}× the five-minute range from '
                    'VWAP; extended')
    # Context can support or veto a technical setup; it cannot manufacture one.
    known = [e for e in evidence if e.get('direction') in (-1, 1)]
    opposing = [e for e in known if e['direction'] == -direction]
    if opposing:
        return wait('Conflicting recent news or disclosed buying/selling; review evidence')
    stop, target = levels(last.close, direction, risk, tick)
    if not (stop < last.close < target if direction == 1 else target < last.close < stop):
        return wait('Stop and target collapse at the exchange tick')
    if abs(last.close - stop) / last.close > .02:
        return wait('Volatility requires a stop wider than 2%')
    if price_band is not None:
        result['price_band'] = price_band
        if 'unknown' in price_band:
            return wait(price_band['unknown'])
        if price_band.get('band_pct') is not None:
            edge = price_band['upper'] if direction == 1 else price_band['lower']
            side = 'upper' if direction == 1 else 'lower'
            if direction * (last.close - edge) >= 0:
                return wait(f'At the {side} price band, where orders queue unfilled')
            if direction * (target - edge) > 0:
                return wait(f'Target beyond the {side} price band of ₹{edge:.2f}')
    if tick is not None:
        result['tick'] = float(tick)
    result.update(action='buy' if direction == 1 else 'sell', stop=stop, target=target,
                  strength='Supported' if known else 'Technical',
                  reason='Opening-range breakout, VWAP and 15-minute momentum '
                         'confirmed by a same-time volume jump')
    return result
