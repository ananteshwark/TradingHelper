"""The fine-tuning split must not leak evaluation outcomes into training."""
import datetime as dt
import json

import pytest

from igs.finetune import dataset as d
from igs.finetune import evaluate as e
from igs.finetune.train import records


def row(call_id, made, action='buy', excess=5.0, **changes):
    base = {'call_id': call_id, 'symbol': 'TEST', 'run_id': 1, 'reason': None,
            'created_at': dt.datetime(*made, 10, tzinfo=dt.UTC), 'model': 'cloud',
            'prompt_version': 'call-v9', 'ai_action': None, 'horizon': '6m',
            'excess_pct': excess, 'inputs': {'run': {'run_id': 1, 'as_of': '2025-01-01'},
                                             'market': {'last_close': 100}},
            'action': action, 'confidence': .7, 'horizon_months': 6,
            'stage': 'early in the move', 'target_price': 130.0, 'summary': 's' * 20,
            'reasons': ['r'], 'risks': ['k'], 'buy_when': ['b'], 'sell_when': ['x'],
            'data_gaps': [], 'vs_growth': 'agrees', 'vs_brokers': 'agrees'}
    return {**base, **changes}


def test_horizon_and_realised_label():
    assert [d.horizon_key(m) for m in (1, 3, 6, 12, 24)] == ['1m', '3m', '6m', '12m', '12m']
    assert [d.realised(x, 2) for x in (3, 1, -1, -3)] == ['buy', 'hold', 'hold', 'sell']


@pytest.mark.lookahead
def test_split_embargo_and_filtering():
    split = d.Split(dt.date(2026, 1, 1), band_pct=2, embargo_days=182)
    rows = [row(1, (2025, 3, 1)),                       # right buy, matured: train
            row(2, (2025, 3, 2), excess=-4),            # wrong buy: not trained on
            row(3, (2025, 9, 1)),                       # inside the embargo: excluded
            row(4, (2026, 2, 1), excess=-4),            # evaluation, kept though wrong
            row(5, (2025, 4, 1), action='hold', excess=-9, ai_action='buy')]  # AI said buy
    parts = d.build(rows, split, minimum=15)
    assert [x['meta']['call_id'] for x in parts['train']] == [1]
    assert [x['meta']['call_id'] for x in parts['eval']] == [4]
    msgs = parts['train'][0]['messages']
    assert [m['role'] for m in msgs] == ['system', 'user', 'assistant']
    assert json.loads(msgs[2]['content'])['action'] == 'buy'
    assert parts['eval'][0]['messages'][-1]['role'] == 'user'
    assert '<market>' in msgs[1]['content'] and 'TEST' in msgs[1]['content']


def test_split_rejects_bad_band():
    with pytest.raises(ValueError):
        d.Split(dt.date(2026, 1, 1), band_pct=-1)


def test_training_records_are_prompt_completion(tmp_path):
    item = d.build([row(1, (2024, 1, 1))], d.Split(dt.date(2026, 1, 1)), 15)['train'][0]
    (tmp_path / 'train.jsonl').write_text(json.dumps(item) + '\n', encoding='utf-8')
    [r] = records(tmp_path / 'train.jsonl')
    assert len(r['prompt']) == 2 and r['completion'][0]['role'] == 'assistant'


def test_evaluation_refuses_remote_endpoints():
    assert e.endpoint('http://127.0.0.1:11434/v1/') == 'http://127.0.0.1:11434/v1'
    with pytest.raises(ValueError, match='loopback'):
        e.endpoint('https://api.example.com/v1')


def test_score_judges_against_index():
    s = e.score(['buy', 'sell', 'hold', None], [3.0, 2.0, -5.0, 1.0])
    assert s['scored'] == 2 and s['right_pct'] == 50.0 and s['invalid'] == 1
    assert s['mean_buy_excess_pct'] == 3.0
