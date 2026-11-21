# Choice (written before the holdout was run)

2026-10-16T16:19:31Z

Development, months held January 2016 to December 2020 (dev.txt). Monthly excess return
over the universe after costs:

| | LightGBM | Ridge | Rule score | Momentum |
|---|---|---|---|---|
| Trading, top 20 | +0.30% (t 0.68) | +0.23% (t 0.46) | +0.35% (t 0.61) | +0.75% (t 1.16) |
| Investing, top-50 cohorts | +0.01% (t 0.03) | +0.15% (t 0.55) | +0.16% (t 0.40) | +0.21% (t 0.73) |

By the protocol, the model with the higher t if positive:
- trading (1 month): LightGBM, gbm_1m;
- investing (12 months): ridge, ridge_12m.

Neither beat the rule score or momentum on development; no rating reached t = 2.

Confirmation to run once: model.py PRED holdout gbm_1m ridge_12m.
