"""Dashboard accounts for password sign-in (IGS_AUTH_MODE=password): users, their sessions
and failed sign-ins, in data/settings/accounts.sqlite3 (readable by the app user only).

Passwords are never stored, not even encrypted: each is kept as a salted scrypt hash
(`scrypt$N$r$p$salt$hash`), which can check a password but not give it back. A session
is a random token in an HttpOnly cookie; only its SHA-256 is stored, so the file can't be
used to sign in. Sessions end after `idle_minutes` without activity or `session_hours`
in all (config/access.yaml), on signing out, and when the password changes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from igs.config import settings_dir

ROLES = ('admin', 'viewer')
MIN_LENGTH = 12
# scrypt at 32 MiB and ~0.1 s a guess; stored with the hash so it can be raised later.
N, R, P = 2 ** 15, 8, 1
# Failed sign-ins: 5 from one address in 15 minutes, or 20 from anywhere in an hour,
# block that account from that address (or from all) until the window has passed.
PER_ADDRESS, ADDRESS_WINDOW = 5, 15 * 60
PER_ACCOUNT, ACCOUNT_WINDOW = 20, 60 * 60
# This many failed sign-ins in this window, for one account or from one address, send a
# Telegram alert (igs.alerts.operations.sign_in_failures).
ALERT_AFTER, ALERT_WINDOW = 3, 15 * 60
USERNAME = re.compile(r'[a-z0-9][a-z0-9._@-]{0,63}')


class AccountError(ValueError):
    """A username, password or role that can't be used; the message says why."""


@dataclass(frozen=True)
class User:
    username: str
    role: str


def path() -> Path:
    return settings_dir() / 'accounts.sqlite3'


SCHEMA = """
    create table if not exists account(
        username text primary key,
        role text not null check (role in ('admin', 'viewer')),
        password_hash text not null,
        created_at real not null,
        password_changed_at real not null,
        last_sign_in_at real);
    create table if not exists session(
        token_hash text primary key,
        username text not null,
        created_at real not null,
        seen_at real not null,
        address text);
    create table if not exists attempt(
        username text not null, address text not null, at real not null);
    create index if not exists attempt_by_time on attempt(username, at);"""
_ready: set[Path] = set()


@contextmanager
def _db(transaction: bool = True):
    """A connection, inside one write transaction committed on leaving unless
    `transaction` is False (each statement on its own, for the frequent session checks)."""
    file = path()
    if file not in _ready or not file.exists():
        file.parent.mkdir(parents=True, exist_ok=True)
        if not file.exists():
            file.touch(mode=0o600)
        with closing(sqlite3.connect(file, timeout=10, isolation_level=None)) as conn:
            conn.execute('pragma journal_mode=wal')
            conn.executescript(SCHEMA)
        _ready.add(file)
    with closing(sqlite3.connect(file, timeout=10, isolation_level=None)) as conn:
        if not transaction:
            yield conn
            return
        conn.execute('begin immediate')
        try:
            yield conn
        except BaseException:
            conn.execute('rollback')
            raise
        conn.execute('commit')


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32,
                            maxmem=64 * 1024 * 1024)
    b64 = base64.b64encode
    return f'scrypt${N}${R}${P}${b64(salt).decode()}${b64(digest).decode()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split('$')
        if kind != 'scrypt':
            return False
        n, r, p = int(n), int(r), int(p)
        expected = base64.b64decode(digest, validate=True)
        if len(expected) != 32:       # a shorter scrypt output is a prefix of the full one
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=n, r=r,
                             p=p, dklen=len(expected), maxmem=256 * r * n + 2 ** 20)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


@lru_cache(maxsize=1)
def _decoy() -> str:
    """A hash to check unknown usernames against, so they take as long as known ones."""
    return hash_password(secrets.token_urlsafe(16))


def normalize(username: str) -> str:
    name = (username or '').strip().casefold()
    if not USERNAME.fullmatch(name):
        raise AccountError('A username is 1 to 64 letters, digits or . _ @ -, starting '
                           'with a letter or digit.')
    return name


def check_password(username: str, password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise AccountError(f'A password needs at least {MIN_LENGTH} characters.')
    if len(password) > 1024:
        raise AccountError('A password can have at most 1,024 characters.')
    if username.casefold() in password.casefold():
        raise AccountError("A password can't contain the username.")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def users() -> list[dict]:
    with _db(transaction=False) as conn:
        rows = conn.execute("""select a.username, a.role, a.created_at, a.last_sign_in_at,
                (select count(*) from session s where s.username = a.username)
            from account a order by a.role, a.username""").fetchall()
    return [dict(zip(('username', 'role', 'created_at', 'last_sign_in_at', 'sessions'), r,
                     strict=True)) for r in rows]


def known(username: str) -> str:
    """The account's name for a log line; never echoes what was typed for an unknown one,
    which may be a password typed in the wrong box."""
    try:
        name = normalize(username)
    except AccountError:
        return 'an unknown username'
    with _db(transaction=False) as conn:
        found = conn.execute('select 1 from account where username = ?', (name,)).fetchone()
    return name if found else 'an unknown username'


def add(username: str, password: str, role: str, now: float | None = None) -> str:
    name = normalize(username)
    if role not in ROLES:
        raise AccountError(f'The role is one of: {", ".join(ROLES)}.')
    check_password(name, password)
    now = now or time.time()
    with _db() as conn:
        if conn.execute('select 1 from account where username = ?', (name,)).fetchone():
            raise AccountError(f'{name} already exists.')
        conn.execute('insert into account values (?, ?, ?, ?, ?, null)',
                     (name, role, hash_password(password), now, now))
    return name


def set_password(username: str, password: str, keep: str | None = None,
                 now: float | None = None) -> None:
    """A new password; ends the account's sessions except the one with token `keep`."""
    name = normalize(username)
    check_password(name, password)
    with _db() as conn:
        if not conn.execute('update account set password_hash = ?, password_changed_at = ? '
                            'where username = ?',
                            (hash_password(password), now or time.time(), name)).rowcount:
            raise AccountError(f'There is no account {name}.')
        conn.execute('delete from session where username = ? and token_hash != ?',
                     (name, _token_hash(keep) if keep else ''))


def remove(username: str) -> None:
    """Delete an account and end its sessions; the last administrator can't be removed."""
    name = normalize(username)
    with _db() as conn:
        role = conn.execute('select role from account where username = ?', (name,)).fetchone()
        if role is None:
            raise AccountError(f'There is no account {name}.')
        admins = conn.execute("select count(*) from account where role = 'admin'").fetchone()
        if role[0] == 'admin' and admins[0] == 1:
            raise AccountError(f"{name} is the only administrator; add another first.")
        conn.execute('delete from account where username = ?', (name,))
        conn.execute('delete from session where username = ?', (name,))


def blocked_for(conn, name: str, address: str, now: float) -> float:
    """Seconds until this account can be tried again from this address; 0 if it can."""
    waits = []
    for count, window, where, args in (
            (PER_ADDRESS, ADDRESS_WINDOW, 'and address = ?', (address,)),
            (PER_ACCOUNT, ACCOUNT_WINDOW, '', ())):
        rows = conn.execute(f'select at from attempt where username = ? and at > ? {where} '
                            'order by at desc', (name, now - window, *args)).fetchall()
        if len(rows) >= count:
            waits.append(rows[count - 1][0] + window - now)
    return max(waits, default=0.0)


def sign_in(username: str, password: str, address: str,
            now: float | None = None) -> tuple[str | None, str, float]:
    """(session token, 'ok', 0), or (None, 'wrong' or 'blocked', seconds to wait). An
    unknown username and a wrong password look the same, and take as long."""
    now = now or time.time()
    try:
        name = normalize(username)
    except AccountError:
        name = None
    with _db() as conn:
        conn.execute('delete from attempt where at < ?', (now - 86400,))
        wait = blocked_for(conn, name or '', address, now) if name else 0.0
        if wait > 0:
            return None, 'blocked', wait
        row = conn.execute('select password_hash from account where username = ?',
                           (name,)).fetchone() if name else None
        ok = verify_password(password, row[0] if row else _decoy()) and row is not None
        if not ok:
            # A username that isn't one is kept as '' (never what was typed, which may be a
            # password in the wrong box): it counts towards the address's alert only.
            conn.execute('insert into attempt values (?, ?, ?)', (name or '', address, now))
            return None, 'wrong', 0.0
        token = secrets.token_urlsafe(32)
        conn.execute('insert into session values (?, ?, ?, ?, ?)',
                     (_token_hash(token), name, now, now, address))
        conn.execute('update account set last_sign_in_at = ? where username = ?', (now, name))
        conn.execute('delete from attempt where username = ? and address = ?', (name, address))
    return token, 'ok', 0.0


def failures(username: str, address: str, now: float | None = None) -> dict:
    """Failed sign-ins in the last ALERT_WINDOW, for the alert: those for this account
    from any address, and those from this address for any username. A username without an
    account is counted, never named."""
    now = now or time.time()
    try:
        name = normalize(username)
    except AccountError:
        name = ''
    since = now - ALERT_WINDOW
    with _db(transaction=False) as conn:
        accounts_ = {r[0] for r in conn.execute('select username from account')}
        name = name if name in accounts_ else None
        mine = conn.execute('select address from attempt where username = ? and at > ?',
                            (name, since)).fetchall() if name else []
        here = conn.execute('select username from attempt where address = ? and at > ?',
                            (address, since)).fetchall()
        wait = blocked_for(conn, name, address, now) if name else 0.0
    return {'account': name, 'account_failures': len(mine),
            'account_addresses': sorted({a for (a,) in mine}),
            'address': address, 'address_failures': len(here),
            'address_accounts': sorted({u for (u,) in here if u in accounts_}),
            'address_unknown': sum(1 for (u,) in here if u not in accounts_),
            'blocked_minutes': math.ceil(wait / 60)}


def session(token: str | None, session_hours: int, idle_minutes: int, *, touch: bool = False,
            now: float | None = None) -> User | None:
    """The signed-in user, or None when the token is unknown, idle too long or too old.
    `touch` counts this as activity."""
    if not token:
        return None
    now = now or time.time()
    key = _token_hash(token)
    with _db(transaction=False) as conn:
        row = conn.execute("""select a.username, a.role, s.created_at, s.seen_at
            from session s join account a using (username) where s.token_hash = ?""",
                           (key,)).fetchone()
        if row is None:
            return None
        name, role, created, seen = row
        if now - seen >= idle_minutes * 60 or now - created >= session_hours * 3600:
            conn.execute('delete from session where token_hash = ?', (key,))
            return None
        if touch:
            conn.execute('update session set seen_at = ? where token_hash = ?', (now, key))
    return User(name, role)


def sign_out(token: str | None) -> None:
    if token:
        with _db() as conn:
            conn.execute('delete from session where token_hash = ?', (_token_hash(token),))


def end_expired(session_hours: int, idle_minutes: int, now: float | None = None) -> int:
    now = now or time.time()
    with _db() as conn:
        return conn.execute('delete from session where seen_at <= ? or created_at <= ?',
                            (now - idle_minutes * 60, now - session_hours * 3600)).rowcount
