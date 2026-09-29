"""Optional research assistant on the Claude API (config/assistant.yaml).

It answers questions about a stored score run through read-only tools (ask.py), writes
plain-language briefs of one stock's ranking (brief.py) and reads new announcements
(announcements.py). The explicit geopolitical feature stores sourced assessments for
a bounded rating overlay. Scoring code may not import this package or call a model;
it only replays assessments actually recorded by the run date. This prevents today's
model from being used to manufacture historical signals (tests/test_assistant.py).
"""
