"""Transaction cost model: statutory charges plus market impact."""

from __future__ import annotations

import math

from igs.config import CostsConfig


def statutory_rate(cfg: CostsConfig, side: str, order_value: float) -> float:
    """Charges as a fraction of order value for one delivery order."""
    s, br = cfg.statutory, cfg.brokerage
    brokerage = min(br.pct / 100 * order_value, br.max_inr_per_order) / max(order_value, 1.0) \
        if br.pct else 0.0
    exch, sebi = s.exchange_txn_pct / 100, s.sebi_fee_pct / 100
    gst = s.gst_pct / 100 * (brokerage + exch + sebi)
    if side == "buy":
        return s.stt_buy_pct / 100 + exch + sebi + s.stamp_duty_buy_pct / 100 + brokerage + gst
    if side == "sell":
        dp = s.dp_charge_inr_per_sell / max(order_value, 1.0)
        return s.stt_sell_pct / 100 + exch + sebi + brokerage + gst + dp
    raise ValueError(side)


def impact_rate(cfg: CostsConfig, order_value: float, adv_value: float | None,
                daily_vol: float | None, bucket: str | None) -> tuple[float, bool]:
    """Half-spread by bucket + sqrt(participation) x daily volatility. Returns (rate, illiquid).

    Missing liquidity data is treated as illiquid and charged the maximum."""
    imp = cfg.impact
    if not adv_value or adv_value <= 0 or daily_vol is None or bucket not in imp.half_spread_bps:
        return imp.max_bps / 1e4, True
    participation = order_value / adv_value
    bps = imp.half_spread_bps[bucket] + imp.sqrt_coefficient * daily_vol * 1e4 * math.sqrt(
        participation)
    return min(bps, imp.max_bps) / 1e4, participation > imp.illiquid_participation


def round_trip_rate(cfg: CostsConfig, order_value: float, adv_value: float | None,
                    daily_vol: float | None, bucket: str | None) -> float:
    imp, _ = impact_rate(cfg, order_value, adv_value, daily_vol, bucket)
    return statutory_rate(cfg, "buy", order_value) + statutory_rate(cfg, "sell", order_value) \
        + 2 * imp
