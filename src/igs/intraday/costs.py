"""Intraday order sizing and charges, shared by real orders (igs.intraday.trading) and
the paper record (igs.intraday.outcomes)."""
from __future__ import annotations

from decimal import Decimal

from igs.config import load_costs


def intraday_rates():
    """Intraday charge rates, config/costs.yaml `intraday`."""
    return load_costs().intraday


def charges(quantity, entry, exit_price, action, rates):
    """Estimated round-trip charges (rupees) of `quantity` shares entered at `entry` and
    closed at `exit_price`: brokerage on each order, STT on the sell, exchange and SEBI
    fees on both, stamp duty on the buy, GST on brokerage and fees."""
    q = Decimal(quantity)
    buy, sell = (entry, exit_price) if action == 'buy' else (exit_price, entry)
    buy_value, sell_value = q * Decimal(str(buy)), q * Decimal(str(sell))

    def pct(name):
        return Decimal(str(getattr(rates, name))) / 100

    cap = Decimal(str(rates.brokerage_max_inr))
    brokerage = sum(min(v * pct('brokerage_pct'), cap) for v in (buy_value, sell_value))
    fees = (buy_value + sell_value) * (pct('exchange_txn_pct') + pct('sebi_fee_pct'))
    total = (brokerage + fees + (brokerage + fees) * pct('gst_pct')
             + sell_value * pct('stt_sell_pct') + buy_value * pct('stamp_duty_buy_pct'))
    return total.quantize(Decimal('0.01'))


def size(price, stop, cfg):
    """Shares for an entry at `price` with its stop at `stop`, by both caps in the
    trading settings: the amount per trade, and what the stop would lose. May be 0."""
    price, stop = Decimal(str(price)), Decimal(str(stop))
    return min(int(Decimal(str(cfg['max_trade_rupees'])) // price),
               int(Decimal(str(cfg['max_risk_rupees'])) // abs(price - stop)))


def net_result(quantity, entry, exit_price, action, rates):
    """(gross, charges, net) rupees of `quantity` shares entered at `entry` and closed at
    `exit_price`, the charges estimated with `rates`."""
    direction = 1 if action == 'buy' else -1
    gross = (direction * (Decimal(str(exit_price)) - Decimal(str(entry)))
             * quantity).quantize(Decimal('0.01'))
    cost = charges(quantity, entry, exit_price, action, rates)
    return gross, cost, gross - cost
