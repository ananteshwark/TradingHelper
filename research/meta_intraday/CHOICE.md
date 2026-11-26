# Choice on the development years

From `dev.txt` (walk-forward predictions, 2023-2024), the candidate with the highest t of
daily rupees after charges among those with a positive mean and at least 100 calls, as the
protocol requires:

- **Ridge regression, taking calls predicted at R 0.10 or more after charges:** 319 of the
  38,190 rule calls, on 175 days; won 49.2%; +0.164 R a call after charges; Rs 348 a day on
  Rs 1 lakh a call; t 1.35. 2023 lost (Rs -75 a day), 2024 made Rs 658 a day.

The ridge at 0.25 had a slightly higher t (1.38) but took only 50 calls, under the minimum
of 100. The trees lost money at every threshold. All rule calls lost Rs 5,201 a day
(t -6.29).

It is short of t 2 on the years it was chosen on. The holdout (January 2025 to the end of
the data) is run once for it.
