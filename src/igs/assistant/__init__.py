"""Optional research assistant on the Claude API (config/assistant.yaml).

It answers questions about a stored score run through read-only tools (ask.py), writes
plain-language briefs of one stock's ranking (brief.py) and reads new announcements
(announcements.py). It never feeds rankings, tiers, checks or backtests: a language model
knows what happened after a run's date, which would bring look-ahead into point-in-time
results, and its output is not reproducible. Scoring code may not import this package
(tests/test_lookahead.py enforces it).
"""
