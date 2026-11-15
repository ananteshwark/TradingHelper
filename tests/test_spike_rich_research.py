"""Causality and data-quality checks for the exploratory rich spike inputs."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research' / 'spike_rich_gpu'))
from build_days import read_sessions  # noqa: E402
from enrich import decision_features  # noqa: E402


def test_future_candles_cannot_change_decision_features():
    bars = [(555 + 5 * i, 100 + i, 101 + i, 99 + i, 100.5 + i, 1000 + i * 10)
            for i in range(16)]
    market = [(555 + 5 * i, 200 + i, 201 + i, 199 + i, 200.5 + i, 5000)
              for i in range(16)]
    prior = {'prevclose': 99., 'd_atr14_pct': .02, 'd_prev_ret': .01}
    before = decision_features(bars, [900.] * 16, 8, prior, market, {})
    later_bars = bars[:9] + [(t, o * 10, h * 10, low * 10, c * 10, v * 100)
                             for t, o, h, low, c, v in bars[9:]]
    later_market = market[:9] + [(t, o * 10, h * 10, low * 10, c * 10, v * 100)
                                 for t, o, h, low, c, v in market[9:]]
    after = decision_features(later_bars, [900.] * 9 + [90000.] * 7, 8, prior,
                              later_market, {})
    assert before == after
    assert before['r_lag0_close_position'] is not None
    assert before['r_nifty_ret30'] is not None


def test_incomplete_or_impossible_candles_are_excluded(tmp_path):
    rows = [[f'2026-01-01T{(555 + 5 * i) // 60:02d}:{(555 + 5 * i) % 60:02d}:00+05:30',
             100, 101, 99, 100, 1000] for i in range(75)]
    rows[10][2] = 98  # high below open
    rows.extend([[f'2026-01-02T{(555 + 5 * i) // 60:02d}:{(555 + 5 * i) % 60:02d}:00+05:30',
                  100, 101, 99, 100, 1000] for i in range(74)])
    source = tmp_path / 'candles.json'
    source.write_text(json.dumps(rows))
    sessions, _, bad = read_sessions(source)
    assert sessions == []
    assert bad == 2
