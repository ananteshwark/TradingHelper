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

In this study the LightGBM baseline made +Rs 318 a day (t 1.5) on the holdout, against
Study B's +Rs 479 (t 2.4). The cause is LightGBM's random row sampling, not the data:

- **Same data.** Study B's own pipeline, unchanged, on this download reproduces Study B
  exactly: development 2,045 trades, Rs 232 a day, t 1.06; holdout 1,598 trades, Rs 479
  a day, t 2.37.
- **The only difference** was 277 training rows (two whole days in 2022, 8 March and 25
  July, whose previous Nifty session was incomplete) that Study C dropped because they had
  no full candle window. No holdout row differed.
- **Dropping 0.15% of training rows** reshuffles every bagging draw (subsample 0.7), the
  same as changing the seed. The holdout predictions correlated 0.947 between the two
  runs; only 1,016 of about 1,580 trades were shared. Trades only Study B took made
  +Rs 22,347; trades only Study C took lost Rs 48,384.
- **Seed spread** (`baseline_seeds.py`, `baseline_seeds.txt`): Study B rerun unchanged
  with LightGBM seeds 1-6, plus its own seed 7:

  | | Mean of 7 | Range |
  |---|---|---|
  | Holdout Rs a day | 393 (sd 117) | 227 to 519 |
  | Holdout t | 1.94 | 1.06 to 2.60 |
  | 2026 to 8 Oct, Rs a day | 203 | 74 to 463 |
  | Development Rs a day | 248 | 132 to 402 |

  Three of the seven seeds pass Study B's t >= 2 bar; all seven are profitable in both
  holdout years with a profit factor of 1.09 to 1.22.

So Study B's model is profitable on the holdout whichever seed is used, but its pass at
t 2.37 came from a favourable seed; the typical seed gives t about 1.9, just under the bar.
The app's frozen model is one more draw. Its evidence is weaker than the single reported
number suggested, which makes the forward record more important. Averaging several seeds,
which Study C did for its networks, would remove this luck from any future model.

## Files

| Purpose | Files |
|---|---|
| Data | `fetch.py` (Study B's fetcher, to 24 Oct 2026) |
| Sequences | `seq_data.py` (81 x 12 windows at 09:45) |
| Networks, walk-forward, evaluation | `seq_model.py` (`dev.txt`, `holdout.txt`) |
| Baseline seed spread | `baseline_seeds.py` (`baseline_seeds.txt`) |

Run with the repository's `.venv` plus PyTorch for CUDA 12.8 and scikit-learn
(`requirements-finetune.txt`). The walk-forward took about 15 minutes on an RTX 5090
Laptop GPU. The downloaded data is not kept in the repository.
