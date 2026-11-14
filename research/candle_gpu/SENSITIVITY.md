# Data-quality sensitivity check — 14 November 2026

The full real-candle feature comparison found 506 mismatches in 205,784 rows the
production module could rebuild. All 23 invalid OHLC bars found in the downloaded
current-member stock files occur at 09:15 on 25 June 2024. The production candle
validator rejects these bars; the older research feature builder included them.
Each affected stock's subsequent 22 sessions may therefore contain a different
prior-session window. The primary GPU study has already been observed, so this is
a **post-result sensitivity check**, never an independent holdout.

Before fitting the sensitivity models, exclude the malformed session and its next
22 feature rows for each of the 23 affected stocks. Keep the entire 2025 and 2026
evaluation rows, the original train/test dates, both feature sets, XGBoost settings,
gate, charges and slippage unchanged. Compare the filtered-data models with the
original models on matching evaluation dates, especially 2026 net daily P&L and
rank IC. This tests whether the original conclusion depends on a small set of
malformed training rows. It does not reconstruct the exact production feature
window, and any model still requires a later prospective paper comparison.
