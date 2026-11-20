# What happened, in order

The protocol (`PROTOCOL.md`) was written and fingerprinted before any return was computed,
but committed only with the results; its SHA-256 shows the text is unchanged since then.

1. **Parsing.** NSE Indices' list of press releases gave 292 on index changes. The 178
   from 2011 to 2026 were downloaded from niftyindices.com and read with pypdf
   (`parse_releases.py`). One, the semi-annual review of 23 August 2021, has no text layer
   (scanned pages) and is missing from the test.
2. **First development run.** It showed stocks joining both indices in one release, which
   can't happen. Some 2011-2014 releases repeat their tables in a second text layer, whose
   stray symbol lines were read as inclusions.
3. **Amendment 1, before any confirmation:** a stock's first appearance in an index's
   section of a release decides whether it was included or excluded, and a Next 50
   inclusion is dropped when the same release puts the stock in the Nifty 50. Four 2012
   inclusions were dropped. The development run was repeated: `dev.txt`.
4. **First confirmation run** (`holdout_first_run.txt`): Nifty 50 joiners -2.97% a trade
   after costs (t -1.11); Next 50 joiners +2.82% (t 2.03), a nominal pass.
5. **Checking that pass**, the data turned out incomplete:
   - in 2024-2026 releases, numbered notes between the exclusion and inclusion tables were
     read as section headings, so the Next 50 inclusions of August 2024, February 2025 and
     August 2025 were lost (18 stocks);
   - the script required the 20 sessions after the effective date, which the protocol
     reports but does not require, so it dropped the August 2026 review.
6. **Amendments 2 and 3, after the first confirmation run:** a numbered line counts as a
   section heading only if it is short (at most eight words), and a bare index name only
   if a table follows it; the 20 sessions after are reported when available. The fixes only
   added changes, all from 2024 to 2026, so the development period is unchanged.
7. **Corrected confirmation run** (`holdout.txt`): Nifty 50 joiners -3.06% (t -1.32);
   Next 50 joiners +1.89% a trade (t 1.75). **Neither passes.**

The first run's pass came from missing data, and the corrected one, which includes every
review that could be read, is the result. Because the corrections were made after a
confirmation result was seen, the confirmation period is now used up for this question.

Without Adani Green Energy's +68% in August-September 2020, the Next 50 average falls to
about +1.0% a trade.
