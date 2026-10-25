# Study C: a sequence model on raw 5-minute candles, October 2026

Can a neural network reading raw 5-minute candles predict the 09:45-to-15:15 move better
than Study B's LightGBM on 23 hand-made features? Fixed in `PROTOCOL.md` (SHA-256 in
`PROTOCOL.sha256`) before any data was downloaded; one candidate chosen on 2023-2024
alone (`CHOICE.md`), then the holdout computed once.

## Result: failed; the app keeps its LightGBM model

| Holdout, Jan 2025 - 8 Oct 2026 | Chosen: hybrid, k = 3, no gate | Baseline: LightGBM, k = 3, gate |
|---|---|---|
| Trades | 2,602 | 1,566 |
| Per trade, gross / net | +0.10% / -0.02% | +0.21% / +0.09% |
| Profit factor | 0.97 | 1.13 |
| Net per day (Rs 1 lakh a trade) | -Rs 117 (t -0.5) | +Rs 318 (t 1.5) |
| 2025 / 2026 per day | -Rs 101 / -Rs 139 | +Rs 614 / -Rs 69 |
| Daily rank IC | 0.014 (t 2.7) | 0.019 (t 3.6) |

- **Pass 1** (Study B's bar: t >= 2, profit factor >= 1.1, both years positive): failed.
- **Pass 2** (better than LightGBM): failed. It made Rs 435 a day less (t -1.6).
- Its three seeds all lost money (-Rs 140 to -Rs 471 a day).

It had already trailed in development: every one of the eight candidates did worse than
LightGBM, and the candles alone (seq) ranked stocks at half its accuracy (`dev.txt`). The
network does find some signal (positive rank IC), but less than the hand-made features,
and not enough to pay the 0.12% round-trip cost. With about 100,000 training rows of a
very noisy target, the extra flexibility seems to fit noise rather than add information.

After the holdout (9-23 October, 10 sessions, too few to judge): hybrid +Rs 160 a day,
LightGBM -Rs 1,882 a day.

## A finding about the baseline

Refitted with Study B's code on this fresh download, the LightGBM rule does worse than
Study B reported on the same holdout: +Rs 318 a day (t 1.5) against +Rs 479 (t 2.4), and
2026 to 8 October negative (-Rs 69 against +Rs 285). It would not pass Study B's own bar
on this data. The rows differ slightly: 277 stock-days lacked a full sequence, and Upstox's
re-served candles may differ from the first download. Why the gap is this large is not
established here. The app's model is the frozen one trained in Study B and isn't affected
directly, but its forward record matters more than its backtest.

## Files

| Purpose | Files |
|---|---|
| Data | `fetch.py` (Study B's fetcher, to 24 Oct 2026) |
| Sequences | `seq_data.py` (81 x 12 windows at 09:45) |
| Networks, walk-forward, evaluation | `seq_model.py` (`dev.txt`, `holdout.txt`) |

Run with the repository's `.venv` plus PyTorch for CUDA 12.8 and scikit-learn
(`requirements-finetune.txt`). The walk-forward took about 15 minutes on an RTX 5090
Laptop GPU. The downloaded data is not kept in the repository.
