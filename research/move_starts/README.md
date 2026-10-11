# Events at the start of big moves

`PROTOCOL.md` (fingerprinted, committed before any result), `EVENTS.md` and
`events_rules.py` (event definitions, committed before any return), `build_events.py`
(event flags and move labels), `analyze.py` (anatomy, development candidates,
confirmation), `fetch/` (the announcement and block-deal downloads).

Outputs: `build.txt` (counts), `dev.txt` (anatomy and development candidates),
`CHOICE.md`, `confirm.txt` (the one confirmation run). Write-up: `docs/MOVE_STARTS.md`.

Addition to the protocol, reported only: the anatomy also shows each event's lift in the
five sessions after the turn (t+1 to t+5), which doesn't enter any choice.

Data notes: block deals hit NSE's 70-row limit on 18 days (16 of them in 2021-2026), which
are incomplete; the 23 August 2021 index review is missing (see `research/index_changes`);
promoter trades run from November 2015 to May 2026.

The scripts ran in a scratch environment (`uv run --with scikit-learn python -I`); the
downloaded data is not kept in the repository.
