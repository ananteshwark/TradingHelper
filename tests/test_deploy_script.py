"""Exercise the deployment orchestration with fake host commands, never a server."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/deploy-from-github.sh"


@pytest.fixture
def deploy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands"
    fake = bin_dir / "runuser"
    fake.write_text('''#!/usr/bin/env python3
import os, sys
args = sys.argv[4:]
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as f:
    f.write(' '.join(args) + '\\n')
if args[0] == 'git':
    command = args[3:]
    if command[0] == 'branch': print('claude/india-growth-screener-32wchm')
    elif command[0] == 'status' and os.environ.get('DEPLOY_TEST_FAIL') == 'dirty':
        print(' M src/edited.py')
    elif command[0] == 'rev-parse': print('abc123')
elif args[0] == 'env':
    if 'list-units' in args:
        if '--type=timer' in args:
            if os.environ.get('DEPLOY_TEST_TIMERS') != 'none':
                print('igs-news.timer loaded active waiting')
        else: print('igs-ui.service loaded active running')
elif args[0] == 'bash':
    if 'db migrate' in args[-1] and os.environ.get('DEPLOY_TEST_FAIL') == 'migration':
        sys.exit(7)
elif args[0] == 'pg_dump': print('fake database backup')
''')
    fake.chmod(0o755)
    for name in ('pg_dump', 'pg_restore', 'curl'):
        tool = bin_dir / name
        tool.write_text('#!/bin/sh\nexit 0\n')
        tool.chmod(0o755)
    script = tmp_path / "deploy.sh"
    # Sandbox the fixed production paths, root guard and app user's uid (no 'anant'
    # account exists on CI runners); all host actions are mocks.
    script.write_text(SCRIPT.read_text()
                      .replace('$EUID', '0')
                      .replace('$(id -u anant)', '1000')
                      .replace('/home/anant/TradingHelper', str(repo))
                      .replace('/home/anant/deploy-backups', str(tmp_path / 'backups'))
                      .replace('/run/lock/igs-deploy.lock', str(tmp_path / 'lock')))

    def run(failure="", check=False, timers=""):
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                   DEPLOY_TEST_LOG=str(log), DEPLOY_TEST_FAIL=failure,
                   DEPLOY_TEST_TIMERS=timers)
        result = subprocess.run(['bash', str(script), *(['--check'] if check else [])],
                                env=env, capture_output=True, text=True, timeout=10)
        return result, log.read_text()
    return run


def test_success_backs_up_before_update_and_resumes_after_validation(deploy):
    result, commands = deploy()
    assert result.returncode == 0, result.stderr
    assert commands.index('pg_dump') < commands.index('merge --ff-only')
    assert commands.index('gate run') < commands.index('start igs-ui.service')
    assert commands.index('start igs-ui.service') < commands.index('start igs-news.timer')
    assert 'sync --locked --all-groups' in commands


def test_dirty_checkout_does_not_stop_services(deploy):
    result, commands = deploy('dirty')
    assert result.returncode != 0
    assert 'Local changes found' in result.stderr
    assert 'stop ' not in commands
    assert 'merge --ff-only' not in commands


def test_failed_migration_keeps_app_and_schedules_stopped(deploy):
    result, commands = deploy('migration')
    assert result.returncode == 7
    assert 'App and schedules remain stopped' in result.stderr
    assert 'start igs-' not in commands
    assert 'gate run' not in commands


def test_check_only_does_not_deploy(deploy):
    result, commands = deploy(check=True)
    assert result.returncode == 0, result.stderr
    assert 'stop ' not in commands
    assert 'merge --ff-only' not in commands


def test_a_rerun_restarts_the_schedules_a_failed_deployment_left_stopped(deploy, tmp_path):
    pending = tmp_path / 'backups' / 'stopped-timers.txt'
    failed, before = deploy('migration')
    assert failed.returncode == 7 and pending.read_text().split() == ['igs-news.timer']
    assert 'next successful deployment restarts' in failed.stderr
    # No timer is active any more, yet the one the failed run stopped comes back.
    result, after = deploy(timers='none')
    assert result.returncode == 0, result.stderr
    assert 'start igs-news.timer' in after[len(before):]
    assert not pending.exists()
