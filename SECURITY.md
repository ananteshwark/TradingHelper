# Security

TradingHelper is a private research workspace, not a multi-tenant hosted service.
Use the current branch and keep its dependencies and operating system updated.

Public deployment uses the app's password sign-in (or Auth0 with MFA) and HTTPS, as in
[docs/PUBLIC_HOSTING.md](docs/PUBLIC_HOSTING.md). Access is denied until an administrator
account exists. Passwords are stored only as salted scrypt hashes, never in plain text or
reversible encryption; sessions are random tokens in HttpOnly, Secure cookies, stored
only as hashes, and repeated failed sign-ins are blocked. Administrators manage shared
settings and API credentials; viewers have read-only research access. The API uses a
separate random bearer token and should remain private. Never publicly proxy
`IGS_AUTH_MODE=local`.

Do not commit `.env`, `.streamlit/secrets.toml`, database dumps or private settings
(including `data/settings/accounts.sqlite3`). Keep backups encrypted/access-restricted
and include the settings, accounts and identity cookie signing key in recovery planning. Rotate exposed credentials immediately.

If you find a vulnerability, use this repository's GitHub private vulnerability
reporting facility if enabled, or contact its owner privately. Do not post tokens,
user data or exploit details in a public issue. No response-time SLA or independent
security certification is claimed.
