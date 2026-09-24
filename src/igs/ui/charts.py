"""Altair chart specs for the UI (pure functions, testable without a browser).

Rules followed (see the data-viz method this project uses):
  * colour by job: categorical slots in a fixed order per entity (never by
    rank), diverging blue/red poles for signed contributions;
  * palettes validated for colour-vision deficiency in light and dark mode;
    two light-mode slots are below 3:1 contrast, so every chart is paired with
    a table view and the shareholding lines carry direct end labels;
  * one y-axis per chart (margin % is its own chart, never a second axis);
  * thin marks: 2px lines, 8px markers, rounded bar ends, 2px gaps;
    recessive grid and axes; text in ink colours, never series colours;
  * a tooltip on every mark.
"""

from __future__ import annotations

import altair as alt
import polars as pl

THEMES = {
    "light": {"text": "#0b0b0b", "text2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
              "axis": "#c3c2b7",
              "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
              "pos": "#2a78d6", "neg": "#e34948"},
    "dark": {"text": "#ffffff", "text2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
             "axis": "#383835",
             "series": ["#3987e5", "#d95926", "#199e70", "#c98500"],
             "pos": "#3987e5", "neg": "#e66767"},
}
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Entities keep their colour whatever is filtered: slot order is fixed per entity.
SHAREHOLDER_ORDER = ["Promoter", "FII", "DII", "Public"]
FINANCIAL_ORDER = ["Revenue", "Profit"]


def _t(theme: str) -> dict:
    return THEMES.get(theme, THEMES["light"])


def _style(chart: alt.Chart, theme: str, height: int = 240) -> alt.Chart:
    t = _t(theme)
    return (chart.properties(height=height, background="transparent")
                 .configure_view(stroke=None)
                 .configure(font=FONT)
                 .configure_axis(gridColor=t["grid"], gridWidth=1, domainColor=t["axis"],
                                 tickColor=t["axis"], labelColor=t["muted"],
                                 titleColor=t["text2"], labelFontSize=11, titleFontSize=11,
                                 titleFontWeight="normal")
                 .configure_legend(labelColor=t["text2"], titleColor=t["text2"], orient="top",
                                   symbolStrokeWidth=2)
                 .configure_title(color=t["text"], fontSize=13, anchor="start",
                                  fontWeight="normal"))


def financials_chart(rows: list[dict], theme: str = "light") -> alt.Chart:
    """Quarterly revenue and profit (INR crore) as grouped bars on one axis."""
    df = pl.DataFrame(rows)
    long = pl.concat([
        df.select(pl.col("period_end").cast(pl.Utf8).alias("quarter"),
                  pl.lit("Revenue").alias("series"), (pl.col("revenue") / 1e7).alias("crore")),
        df.select(pl.col("period_end").cast(pl.Utf8).alias("quarter"),
                  pl.lit("Profit").alias("series"), (pl.col("pat") / 1e7).alias("crore")),
    ]).to_pandas()
    t = _t(theme)
    chart = alt.Chart(long, title="Revenue and profit by quarter (INR crore)").mark_bar(
        cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=alt.X("quarter:N", title=None, axis=alt.Axis(labelAngle=0),
                scale=alt.Scale(paddingInner=0.25)),
        # small surface gap between the two bars of a quarter
        xOffset=alt.XOffset("series:N", sort=FINANCIAL_ORDER,
                            scale=alt.Scale(paddingInner=0.08)),
        y=alt.Y("crore:Q", title="INR crore", axis=alt.Axis(grid=True)),
        color=alt.Color("series:N", sort=FINANCIAL_ORDER, title=None,
                        scale=alt.Scale(domain=FINANCIAL_ORDER, range=t["series"][:2])),
        tooltip=[alt.Tooltip("quarter:N", title="Quarter ending"),
                 alt.Tooltip("series:N", title="Series"),
                 alt.Tooltip("crore:Q", title="INR crore", format=",.1f")],
    ).properties(width="container")
    return _style(chart, theme)


def margin_chart(rows: list[dict], theme: str = "light") -> alt.Chart:
    """Operating margin by quarter: a single series, so no legend box."""
    df = pl.DataFrame(rows).select(pl.col("period_end").cast(pl.Utf8).alias("quarter"),
                                   (pl.col("opm") * 100).alias("margin")).to_pandas()
    t = _t(theme)
    base = alt.Chart(df, title="Operating (EBITDA) margin, %").encode(
        x=alt.X("quarter:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("margin:Q", title="%", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("quarter:N", title="Quarter ending"),
                 alt.Tooltip("margin:Q", title="Margin %", format=".1f")])
    line = base.mark_line(strokeWidth=2, color=t["series"][0])
    points = base.mark_point(size=64, filled=True, color=t["series"][0])
    return _style(alt.layer(line, points).properties(width="container"), theme)


def shareholding_chart(rows: list[dict], theme: str = "light") -> alt.Chart:
    """Shareholding by category over time, direct-labelled at the latest quarter."""
    names = {"promoter": "Promoter", "institutions_foreign": "FII",
             "institutions_domestic": "DII", "public": "Public"}
    df = pl.DataFrame(rows)
    long = pl.concat([df.select(pl.col("period_end").cast(pl.Utf8).alias("quarter"),
                                pl.lit(label).alias("holder"), pl.col(col).alias("pct"))
                      for col, label in names.items() if col in df.columns])
    # ~10% of the axis span keeps 11px labels apart at the 300px chart height.
    last = dodge_labels(long.filter(pl.col("quarter") == long["quarter"].max()), "pct",
                        min_gap=max(2.0, 0.1 * float(long["pct"].max() or 1)))
    t = _t(theme)
    enc = {"x": alt.X("quarter:N", title=None, axis=alt.Axis(labelAngle=0)),
           "y": alt.Y("pct:Q", title="% of shares")}
    color = alt.Color("holder:N", title=None, sort=SHAREHOLDER_ORDER,
                      scale=alt.Scale(domain=SHAREHOLDER_ORDER, range=t["series"]))
    tip = [alt.Tooltip("quarter:N", title="Quarter ending"),
           alt.Tooltip("holder:N", title="Holder"),
           alt.Tooltip("pct:Q", title="% of shares", format=".2f")]
    lines = alt.Chart(long.to_pandas()).mark_line(strokeWidth=2).encode(
        **enc, color=color, tooltip=tip)
    points = alt.Chart(long.to_pandas()).mark_point(size=64, filled=True).encode(
        **enc, color=color, tooltip=tip)
    labels = alt.Chart(last.to_pandas()).mark_text(align="left", dx=8, color=t["text2"],
                                                   fontSize=11).encode(
        x=enc["x"], y=alt.Y("label_y:Q"), text="holder:N")
    return _style(alt.layer(lines, points, labels, title="Shareholding pattern")
                  .properties(width="container"), theme, height=300)


def dodge_labels(df: pl.DataFrame, value: str, min_gap: float) -> pl.DataFrame:
    """Direct-label positions pushed apart so neighbouring labels never collide."""
    rows = sorted(df.to_dicts(), key=lambda r: r[value])
    prev = None
    for r in rows:
        y = r[value]
        if prev is not None and y - prev < min_gap:
            y = prev + min_gap
        r["label_y"] = y
        prev = y
    return pl.DataFrame(rows)


def has_margin(rows: list[dict]) -> bool:
    return any(r.get("opm") is not None for r in rows)


def price_chart(rows: list[dict], theme: str = "light") -> alt.Chart:
    df = pl.DataFrame(rows).to_pandas()
    t = _t(theme)
    chart = alt.Chart(df, title="Close price (INR, unadjusted)").mark_line(
        strokeWidth=2, color=t["series"][0]).encode(
        x=alt.X("trade_date:T", title=None),
        y=alt.Y("close:Q", title="INR", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("trade_date:T", title="Date"),
                 alt.Tooltip("close:Q", title="Close", format=",.2f")])
    return _style(chart.properties(width="container"), theme, height=200)


def contribution_chart(factors: list[dict], theme: str = "light") -> alt.Chart:
    """Signed contribution of each factor to the composite (diverging blue/red)."""
    df = pl.DataFrame([f for f in factors if f.get("contribution") is not None])
    df = df.select(pl.col("factor").replace_strict(SHORT, default=pl.col("factor")),
                   "contribution", "peer_percentile", "value").with_columns(
        pl.when(pl.col("contribution") >= 0).then(pl.lit("adds to score"))
          .otherwise(pl.lit("subtracts from score")).alias("direction")).to_pandas()
    t = _t(theme)
    chart = alt.Chart(df, title="Contribution to composite score by factor").mark_bar(
        cornerRadiusEnd=4, height={"band": 0.75}).encode(
        y=alt.Y("factor:N", sort="-x", title=None,
                axis=alt.Axis(labelLimit=240, labelOverlap=False, labelFontSize=11)),
        x=alt.X("contribution:Q", title="contribution (z-score units)"),
        color=alt.Color("direction:N", title=None,
                        scale=alt.Scale(domain=["adds to score", "subtracts from score"],
                                        range=[t["pos"], t["neg"]])),
        tooltip=[alt.Tooltip("factor:N", title="Factor"),
                 alt.Tooltip("contribution:Q", title="Contribution", format="+.3f"),
                 alt.Tooltip("peer_percentile:Q", title="Peer percentile", format=".0%"),
                 alt.Tooltip("value:Q", title="Raw value", format=",.4g")])
    return _style(chart.properties(width="container"), theme,
                  height=max(160, 24 * len(df)))


SHORT = {
    "revenue_cagr_3y": "Revenue CAGR 3y", "revenue_cagr_5y": "Revenue CAGR 5y",
    "ebitda_cagr_3y": "EBITDA CAGR 3y", "ebitda_cagr_5y": "EBITDA CAGR 5y",
    "pat_cagr_3y": "Profit CAGR 3y", "pat_cagr_5y": "Profit CAGR 5y",
    "revenue_ttm_yoy": "Revenue growth TTM", "pat_ttm_yoy": "Profit growth TTM",
    "growth_acceleration_4q": "Growth acceleration", "growth_consistency_12q":
    "Growth consistency", "roce": "ROCE", "roe": "ROE", "opm_level": "Operating margin",
    "opm_trend_8q": "Margin trend", "cash_conversion_3y": "Cash conversion",
    "net_debt_to_ebitda": "Net debt / EBITDA", "interest_coverage": "Interest coverage",
    "working_capital_days_trend": "Working-capital days", "pe_vs_own_5y_median":
    "P/E vs own history", "peg_trailing": "PEG", "ev_ebitda": "EV / EBITDA", "pb": "P/B",
    "risk_adj_return_6m": "Risk-adjusted return 6m",
    "risk_adj_return_12m": "Risk-adjusted return 12m", "volatility_1y": "Volatility 1y",
    "rs_6m_vs_nifty500": "Relative strength 6m", "rs_12m_vs_nifty500": "Relative strength 12m",
    "price_vs_200dma": "Price vs 200-DMA", "dma_50_200_state": "50/200-DMA state",
    "delivery_pct_20d_vs_1y": "Delivery % trend", "promoter_holding_qoq": "Promoter holding",
    "pledge_pct": "Promoter pledge", "pledge_trend": "Pledge trend",
    "fii_dii_holding_change": "FII + DII holding", "institutional_holder_count":
    "Institutional holders"}
