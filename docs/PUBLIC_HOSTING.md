# Public hosting at https://stocks.ednis.ai

The dashboard signs people in with **its own accounts and passwords**
(`IGS_AUTH_MODE=password`, the default). Access is closed until an administrator account
exists. Auth0 sign-in with MFA is still available as an alternative (`IGS_AUTH_MODE=oidc`,
[below](#alternative-auth0-sign-in-with-mfa)).

## 1. Password sign-in

### How passwords are kept

Passwords are never stored, not even encrypted. Each one is kept as a **salted scrypt
hash** (`scrypt$32768$8$1$<salt>$<hash>`, 32 MiB of memory per check):
- A hash can check a password but can't be turned back into it. Encryption could be
  reversed by anyone holding the key, which would have to sit on the same server.
- Each password has its own random salt, so equal passwords get different hashes.
- Each guess against a stolen hash costs about 0.1 s and 32 MiB, which makes guessing
  slow and expensive.

They live in `data/settings/accounts.sqlite3`. The file is readable by the app user only,
is not in Git, and goes into the private backups with the rest of `data/settings`.

### Signing in

1. A browser without a session that opens the dashboard is sent to `/signin`, a plain
   page served by the same process. None of the app loads before signing in.
2. A correct username and password set a session cookie: a random token,
   `HttpOnly; Secure; SameSite=Lax`. The server stores only the token's SHA-256, so the
   account file can't be used to sign in.
3. Every page and every administrator action looks the session up again. Signing out,
   a password change or removing the account ends it at the next click, in every tab.

A session ends after `idle_minutes` without activity (30) or `session_hours` after
signing in (8), from `config/access.yaml`. Settings > Access changes them. Table
refreshes on a timer don't count as activity.

### Guessing and forgery

- **Repeated failures:** 5 failures for an account from one address in 15 minutes block
  that address for the rest of the 15 minutes. 20 failures from anywhere in an hour
  block the account for the rest of the hour. The owner can still sign in from another
  address in the first case.
- **Unknown usernames:** an unknown username and a wrong password get the same message
  and take as long.
- **Logs:** sign-ins, failures and blocks are logged with the account and address
  (`journalctl --user -u igs-ui`). What was typed as an unknown username is never logged,
  since it may be a password typed in the wrong box. Passwords are never logged.
- **Forged sign-ins:** the form carries a one-time token that another site can't read.
- **Plain HTTP:** the cookies are sent only over HTTPS, except to 127.0.0.1 on the
  server itself.

### Password rules

At least 12 characters, at most 1,024, and not containing the account name. A
passphrase of four or more random words, or a password manager's, is strong. Don't reuse
the server, database or GitHub password.

### Accounts

On the server, as the app user (never root, or the app can't read the file):

```bash
sudo -iu anant
cd ~/TradingHelper
uv run igs users add anant --role admin      # asks for the password twice, hidden
uv run igs users list
uv run igs users passwd anant                # new password; ends its sessions
uv run igs users add reader --role viewer
uv run igs users remove reader               # the last administrator can't be removed
```

A password is typed at a hidden prompt, never given on the command line, where it would
stay in the shell history. **Settings > Access** does the same for a signed-in
administrator: add, change a password, remove, and set the session limits.

- **Administrators** manage settings, imports, AI requests, Upstox and shared watchlists.
- **Viewers** read rankings, stock research, calls, news and data quality, but change
  nothing.

All accounts see the same research data; this is not isolation between unrelated
customers.

A forgotten password can't be recovered, only replaced, with `igs users passwd` on the
server.

### Switching from Auth0

Before switching, create the administrator account (step 1 under Accounts above),
otherwise no one can sign in. Then, as the app user:

```bash
install -m 0644 scripts/deploy/igs-ui.service ~/.config/systemd/user/igs-ui.service
systemctl --user daemon-reload
systemctl --user restart igs-ui.service
curl -fsS http://127.0.0.1:8501/_stcore/health
curl -sI http://127.0.0.1:8501/ | grep -i location        # location: /signin
```

The unit now sets `IGS_AUTH_MODE=password`, and `igs ui` then serves `src/igs/ui/server.py`:
the app plus the `/signin` page. The Auth0 files (`.streamlit/secrets.toml`, the issuer
and emails in `data/settings/access.yaml`) are no longer used. Keep them to switch back,
or delete the Auth0 application. The Caddyfile is unchanged.

**What it gives up:** Auth0 required a second factor (an authenticator app). Password
sign-in has none, so the password alone protects settings, Upstox tokens and live order
approvals. Use a long unique password, and keep the account list short.

## 2. Install the application and the first account

First use [MIGRATION.md](MIGRATION.md) to copy the existing data onto the new server.
After updating code/dependencies, as the app user:

```bash
cd ~/TradingHelper
uv sync --locked --all-groups
uv run igs users add anant --role admin
```

Keep `data/settings/` (with `accounts.sqlite3`) in your private backups.

## 3. Install the locked-down UI service

On the destination server, as the app user (the examples assume `anant`):

```bash
mkdir -p ~/.config/systemd/user
install -m 0644 scripts/deploy/igs-ui.service ~/.config/systemd/user/igs-ui.service
sudo loginctl enable-linger anant
systemctl --user daemon-reload
systemctl --user enable --now igs-ui.service
systemctl --user status igs-ui.service --no-pager
curl -fsS http://127.0.0.1:8501/_stcore/health
```

Stop any earlier manually launched UI before starting the service. It binds only
to `127.0.0.1:8501`, sets `IGS_AUTH_MODE=password`, keeps XSRF/CORS protection enabled,
and disables static-directory serving. It uses `--no-sync`; restore the five
separate ingestion/review timers using the migration guide. Do not enable old
and new server schedules simultaneously.

`IGS_AUTH_MODE=local` is only an explicit desktop development escape hatch on a
loopback listener. It is not safe behind a public reverse proxy. The supplied
public service overrides it with `password`. Never publish a local-mode instance.

## 4. Point DNS and configure HTTPS

Set the DNS **A** record for `stocks.ednis.ai` to your new server's IPv4 address.
Only add an AAAA record if the server is correctly reachable over IPv6 too.
Allow inbound TCP 80/443 and restrict SSH to your administrative access. Keep
ports 8501, 8000 and 5432 private; do not open them in the cloud firewall.

Install Caddy following its official Linux package instructions, then:

```bash
cd ~/TradingHelper
sudo install -m 0644 scripts/deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

These commands assume a dedicated Caddy instance. If the server hosts other
sites, add this site's block to the existing configuration instead of replacing
it. Caddy obtains/renews HTTPS certificates and proxies WebSocket traffic to the
loopback dashboard. DNS and ports 80/443 must reach this server for certificate
issuance. The provided configuration adds HSTS, anti-framing, no-sniff,
no-referrer and no-store headers and does not expose the service API.

## 5. Keep the API private and authenticated

The separate FastAPI service is no longer anonymously readable/writable.
Every route, including its schema/docs and CSV routes, requires a bearer token,
except `/health`, which returns no application data. With no token configured,
private routes remain closed. Browser sessions do not use or need this token.

If you need server-to-server API access, create a random token without printing
it, inside `.env` using the existing helper:

```bash
uv run python - <<'PY'
import secrets
from igs import envfile
envfile.set_value(envfile.default_path(), 'IGS_API_TOKEN', secrets.token_urlsafe(48))
print('Service API credential saved privately. Restart the API service.')
PY
```

Send `Authorization: Bearer <token>` from authorized clients over a private
network or authenticated TLS deployment. Treat this as an administrator service
credential; do not embed it in frontend JavaScript, use it as a user password,
or reuse the Auth0 Client Secret. The example Caddyfile intentionally routes
only Streamlit, not the API. The minimum accepted API-token length is 32
characters; generation above provides a high-entropy credential.

## 6. Verify before calling the server ready

Use a private/incognito browser at `https://stocks.ednis.ai`:

1. Opening the dashboard shows only the sign-in page, with no rankings or settings.
2. A wrong password says "Wrong username or password." After five wrong tries, the
   next says to try again in 15 minutes, and even the right password is refused until
   then.
3. Sign in as the administrator. Rankings and settings appear and actions work.
4. Sign out. The page asks to sign in again, and reloading goes to the sign-in page.
5. Add a test viewer in Settings > Access. It can read but not change settings,
   watchlists, imports or AI requests. Remove it: its open page is signed out at its next
   click.
6. Leave a page idle past `idle_minutes`: its next click asks to sign in again.
7. Direct public access to ports 8501/8000/5432 fails. API data routes without a bearer
   token get 401, even on the private network.
8. Check TLS, scheduled feeds, Telegram and database migration status.

Native dataframe toolbar exports remain available after login. Explicit
server-hosted CSV download links are restricted to local development mode;
private server-to-server exports use the authenticated API. Do not turn on
Streamlit static file serving for the repository or its data directory.

Automated tests cover the password hashes, sessions and their limits, blocking after
failures, the sign-in page and its cookies, read-only viewers, API denial before database
access, local/public mode, and the Auth0 checks.

## Alternative: Auth0 sign-in with MFA

Set `Environment=IGS_AUTH_MODE=oidc` in `~/.config/systemd/user/igs-ui.service` and
restart it. Sign-in then goes through Auth0, which can require a second factor, and the
accounts above are not used. The intended administrator is **ananteshwark@gmail.com**.

### Create and configure the Auth0 application

In your Auth0 tenant, create a **Regular Web Application** named TradingHelper.
Set these exact addresses (no wildcard callbacks):

- Allowed Callback URLs: `https://stocks.ednis.ai/oauth2callback`
- Allowed Logout URLs: `https://stocks.ednis.ai`
- Allowed Web Origins: `https://stocks.ednis.ai`

Use a database connection for email/password accounts and enable it for this
application. Create/invite `ananteshwark@gmail.com` and complete email verification.
Disable public signups for this private workspace. Other accounts are still
rejected by the application's allowlist even if they can authenticate at Auth0.

Configure password protection on the Auth0 database connection:

- Require at least 16 characters (use the flexible password policy/minimum-length
  configuration supported by your tenant).
- Enable common-password/dictionary and personal-data checks, password history,
  and breached-password protection where available.
- Use a unique password generated by a password manager; do not reuse the Linux,
  database or GitHub password. Set it through Auth0's invitation/reset flow,
  never in this repository or a chat message.
- Enable brute-force blocking and suspicious-IP protection. Configure blocking,
  not monitoring-only. A threshold of five failed attempts is a starting point.

Enable MFA **Always**, with an authenticator or WebAuthn/security key. Enroll the
administrator and store recovery codes privately. Do not enable a remembered
browser bypass: the app requires `amr` to contain `mfa` on the ID token.
For an application-specific enforcement Action in the Auth0 Login flow:

```javascript
exports.onExecutePostLogin = async (event, api) => {
  if (event.client.client_id === 'YOUR_TRADINGHELPER_CLIENT_ID') {
    api.multifactor.enable('any', { allowRememberBrowser: false });
  }
};
```

Replace the Client ID and deploy/attach the Action to the Login flow. Tenant
features depend on your Auth0 plan; verify MFA and password/attack protections
are actually enabled. The application refuses identities without MFA evidence
rather than silently dropping that requirement.

### Install the Auth0 settings

After section 2, as the app user:

```bash
cd ~/TradingHelper
uv run python scripts/configure-public-auth.py
```

Enter the Auth0 tenant hostname (for example, `tenant.eu.auth0.com`), application
Client ID, `ananteshwark@gmail.com`, and the **Client Secret at the hidden prompt**.
The script creates:

- `.streamlit/secrets.toml`: OIDC credentials and a random cookie-signing key;
- `data/settings/access.yaml`: issuer, approved identities, and session settings.

Both are private mode-0600 files excluded from Git. The script refuses to
overwrite existing configuration. To rotate credentials, edit the files
privately, preserve valid settings, and restart the service. Rotate the signing
key if identity cookies must be invalidated globally.

The configuration uses `https://stocks.ednis.ai/oauth2callback`, forces a fresh
provider login, and requests OIDC identity/email claims. It exposes no access or
ID tokens to the frontend. Keep these files in your private migration backups.

Access rules are loaded on every check. Administrators can manage settings,
imports, AI requests and shared watchlists. To allow read-only users, add their
verified addresses to `viewer_emails` in `data/settings/access.yaml`. These users
can read rankings, stock research, calls, news and data quality, but cannot
change settings, manage the shared watchlist/screens, import data or run paid AI
requests. All users see the same research dataset; this is **not** tenant/data
isolation for unrelated customers.

By default, access expires at the earlier of the provider token's expiration or
8 hours since authentication/token issuance. A session inactive for 30 minutes
must sign in again; automatic table refreshes do not extend that idle period.
The app checks identity, issuer, approved account and MFA before database access
and again before administrator callbacks. Removing an account from the allowlist
revokes its next access, without waiting for cookie expiry.

### Deployments without MFA

MFA remains required by default. An administrator can explicitly set
`require_mfa: false` in the private `data/settings/access.yaml` policy when MFA
is unavailable. This only disables the app's MFA claim requirement; issuer,
approved account, verified email (for email-based access), and session checks
remain active. Auth0's own MFA policy is managed separately in its dashboard.
Restore `require_mfa: true` when MFA is available.

## Primary references

- [NIST SP 800-63B, passwords](https://pages.nist.gov/800-63-4/sp800-63b.html)
- [OWASP password storage (scrypt)](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [OWASP session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [Streamlit authentication](https://docs.streamlit.io/develop/concepts/connections/authentication)
- [Auth0 Universal Login](https://auth0.com/docs/authenticate/login/auth0-universal-login)
- [Auth0 password policies](https://auth0.com/docs/authenticate/database-connections/password-options)
- [Auth0 brute-force protection](https://auth0.com/docs/secure/attack-protection/brute-force-protection)
- [Auth0 MFA claims](https://auth0.com/docs/secure/multi-factor-authentication/step-up-authentication/configure-step-up-authentication-for-web-apps)
- [Caddy installation](https://caddyserver.com/docs/install#debian-ubuntu-raspbian)
- [Caddy HTTPS reverse proxy](https://caddyserver.com/docs/quick-starts/reverse-proxy)
