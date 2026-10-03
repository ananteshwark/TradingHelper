#!/usr/bin/env python3
"""Write private Auth0 configuration interactively; never print secrets."""
import getpass
import json
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    tenant = input('Auth0 tenant hostname (no https://): ').strip().lower()
    client_id = input('Auth0 application Client ID: ').strip()
    email = input('Administrator email (verified in Auth0): ').strip().lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', tenant) or '.' not in tenant:
        raise SystemExit('Enter a valid Auth0 hostname.')
    if not client_id or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise SystemExit('Client ID and a valid administrator email are required.')
    secret = getpass.getpass('Auth0 Client Secret (hidden): ').strip()
    if len(secret) < 16:
        raise SystemExit('Client Secret is missing or too short.')
    secrets_path = ROOT / '.streamlit/secrets.toml'
    policy_path = ROOT / 'data/settings/access.yaml'
    if secrets_path.exists() or policy_path.exists():
        raise SystemExit('Configuration already exists. Edit it privately; no files overwritten.')
    # JSON is valid YAML. JSON string escaping also safely quotes these TOML values.
    auth = {'redirect_uri': 'https://stocks.ednis.ai/oauth2callback',
            'cookie_secret': secrets.token_urlsafe(48), 'client_id': client_id,
            'client_secret': secret,
            'server_metadata_url': f'https://{tenant}/.well-known/openid-configuration'}
    content = '[auth]\n' + ''.join(f'{key} = {json.dumps(value)}\n' for key, value in auth.items())
    content += ('\n[auth.client_kwargs]\nscope = "openid profile email"\n'
                'prompt = "login"\nmax_age = 0\n')
    policy = {'issuer': f'https://{tenant}/', 'admin_emails': [email], 'viewer_emails': [],
              'session_hours': 8, 'idle_minutes': 30}
    for path, value in [(secrets_path, content), (policy_path, json.dumps(policy, indent=2)+'\n')]:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(value)
    print('Private configuration written. Enable Auth0 MFA and password/attack protection, '
          'then restart.')


if __name__ == '__main__':
    main()
