# Security

TradingHelper is a private research workspace, not a multi-tenant hosted service.
Use the current branch and keep its dependencies and operating system updated.

Public deployment requires the Auth0 login/MFA and HTTPS configuration in
[docs/PUBLIC_HOSTING.md](docs/PUBLIC_HOSTING.md). Authentication denies access by
default. Approved administrators manage shared settings and API credentials;
viewers have read-only research access. The API uses a separate random bearer
token and should remain private. Never publicly proxy `IGS_AUTH_MODE=local`.

Do not commit `.env`, `.streamlit/secrets.toml`, database dumps or private settings.
Keep backups encrypted/access-restricted and include the settings and identity
cookie signing key in recovery planning. Rotate exposed credentials immediately.

If you find a vulnerability, use this repository's GitHub private vulnerability
reporting facility if enabled, or contact its owner privately. Do not post tokens,
user data or exploit details in a public issue. No response-time SLA or independent
security certification is claimed.
