# Choices, made on 2023-2024 only (`dev.txt`)

Written before the confirmation period was run. The simulator reproduces the winners'
study on this period exactly: X0 +0.032% a trade (t 0.60), F +0.064% (t 1.34).

- **Trailing stop: failed on development.** T05 t 1.16 and T10 t 0.55, both under F's
  1.34. Trailing at half an ATR closed half the trades early and gave up winners; at one
  ATR it barely acted.
- **Break-even stop: B10 chosen** (t 1.36 against F's 1.34; B05 0.97). It changed little:
  the stop rarely moved before the trade reversed.
- **Time stop: S12 chosen** (t 1.76; S13 1.56): close at the 12:00 open a trade losing at
  11:55.
- **ML exit model: failed on development.** M0 t 0.29 and M10 0.90, both under F. The model
  closed 68–89% of trades early and cut the average to +0.01–0.03% a trade.
- **Daily loss limit: D10 chosen** (t 1.48; D05 1.43): close the day's open trades when the
  day's average is −1.0% or worse.

Confirmation to run once: `exits.py ... holdout T=none B=B10 S=S12 M=none D=D10`.
