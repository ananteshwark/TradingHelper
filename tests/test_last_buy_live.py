"""A prior live-price buy must retain the timestamp used by the AI calls page."""
import datetime as dt

import pytest
from test_ai_calls import _insert_entry
from test_ai_calls import scored as scored

from igs.assistant import calls as ai
from igs.timeutil import IST


@pytest.mark.db
def test_last_buy_preserves_live_entry_timestamp(scored):
    conn, run_id = scored
    _insert_entry(conn, run_id, 1, 'GROW', dt.date(2022, 6, 1),
                  source='live', price=101.5, benchmark=22000)
    entry_at = conn.execute('select entry_at from ai_call order by call_id desc limit 1')
    stamp = entry_at.fetchone()[0].astimezone(IST)
    result = ai.last_buy(conn, 1)
    assert result is not None
    assert 'Rs 101.50' in result['entry']
    assert f'at {stamp:%H:%M} IST' in result['entry']
    assert '(live)' in result['entry']
