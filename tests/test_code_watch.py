"""Deployment watcher must debounce edits without looping on generated files."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    'watch_app_code', Path(__file__).resolve().parents[1] / 'scripts/watch-app-code.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def test_nested_content_changes_and_removal_but_not_generated_files(tmp_path):
    src = tmp_path / 'src/igs/ui'
    src.mkdir(parents=True)
    code = src / 'page.py'
    code.write_text('old')
    original = watch.fingerprint(tmp_path)
    code.touch()
    assert watch.fingerprint(tmp_path) == original
    cache = src / '__pycache__'
    cache.mkdir()
    (cache / 'page.pyc').write_bytes(b'generated')
    assert watch.fingerprint(tmp_path) == original
    code.write_text('new')
    changed = watch.fingerprint(tmp_path)
    assert changed != original
    code.unlink()
    assert watch.fingerprint(tmp_path) != changed


def test_wait_for_quiet_period_and_restart_only_once():
    restart = Mock()
    state = watch.check({'applied': 'old'}, 'new', 100, restart)
    state = watch.check(state, 'newer', 110, restart)
    state = watch.check(state, 'newer', 125, restart)
    restart.assert_not_called()
    state = watch.check(state, 'newer', 131, restart)
    assert state == {'applied': 'newer'}
    watch.check(state, 'newer', 200, restart)
    restart.assert_called_once()


def test_failed_restart_does_not_mark_code_applied():
    state = {'applied': 'old', 'pending': 'new', 'since': 100}
    restart = Mock(side_effect=RuntimeError('restart failed'))
    with pytest.raises(RuntimeError):
        watch.check(state, 'new', 130, restart)
    assert state['applied'] == 'old'
