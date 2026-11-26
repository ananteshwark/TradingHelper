"""Downloading a backup to your computer, and uploading one back (igs.backup).

Backups can be many gigabytes, more than Streamlit's own download and upload widgets hold
(they keep a file in memory), so these are plain routes next to /signin (server.py),
streamed straight from and to disk:

    GET  /backups/download/<name>   the backup file, as a download
    GET  /backups/upload            a page to choose a backup file on your computer
    POST /backups/upload            its bytes, streamed into data/backups

Only a signed-in administrator can use them (the session cookie of /signin). The upload
also needs the page's own token (a cookie and a header that must match) and the same
origin, so another site can't post a file. An upload is written as a partial file, then
kept only if it is a backup (its manifest first), under its own time with "-uploaded".
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from igs import backup
from igs.ui.signin import COOKIE, _same_origin, _secure, current

UPLOAD_COOKIE = "igs_backup_upload"
MIN_FREE = 1 << 30                  # an upload stops before the disk has less than 1 GB free


def _admin(request: Request) -> bool:
    user = current(request.cookies.get(COOKIE))
    return user is not None and user.role == "admin"


async def download(request: Request):
    if not await run_in_threadpool(_admin, request):
        return RedirectResponse("/signin", 303)
    name = request.path_params["name"]
    path = backup.backup_dir() / name
    if not backup.NAME.match(name) or not path.is_file():
        return HTMLResponse("No such backup.", 404)
    return FileResponse(path, media_type="application/gzip", filename=name,
                        headers={"Cache-Control": "no-store"})


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload a backup · IndiaGrowthScreener</title>
<style>
:root {{ --surface: #f6f6f3; --card: #ffffff; --ink: #0b0b0b; --ink-2: #52514e;
  --line: #d9d8d0; --accent: #2a78d6; --on-accent: #ffffff; --error: #b42318;
  --good: #1f7a3a; color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root {{ --surface: #0e1117; --card: #171a21;
  --ink: #f2f2ef; --ink-2: #a9a8a2; --line: #33363d; --accent: #3987e5;
  --error: #ff8a80; --good: #6fcf8e; color-scheme: dark; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px 16px;
  background: var(--surface); color: var(--ink);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ width: 100%; max-width: 520px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
p {{ color: var(--ink-2); margin: 0 0 16px; font-size: 15px; }}
form {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 24px; display: grid; gap: 16px; }}
input[type=file] {{ font: inherit; color: var(--ink); }}
button {{ font: inherit; font-weight: 600; color: var(--on-accent); background: var(--accent);
  border: 0; border-radius: 8px; padding: 11px; cursor: pointer; }}
button:disabled {{ opacity: .6; cursor: default; }}
progress {{ width: 100%; height: 10px; }}
#status {{ margin: 0; min-height: 1.5em; }}
#status.error {{ color: var(--error); }} #status.good {{ color: var(--good); }}
a {{ color: var(--accent); }}
</style></head>
<body><main>
<h1>Upload a backup</h1>
<p>Choose a backup file (<code>igs-backup-….tar.gz</code>) kept on this computer. It is
copied to the server's backups; restore it from Settings → Maintenance.</p>
<form id="form">
<input id="file" type="file" accept=".gz,application/gzip" required>
<button id="go" type="submit">Upload</button>
<progress id="bar" max="100" value="0" hidden></progress>
<p id="status" role="status" aria-live="polite"></p>
</form>
<p><a href="/">Back to the app</a></p>
</main>
<script nonce="{nonce}">
const form = document.getElementById("form"), file = document.getElementById("file"),
  go = document.getElementById("go"), bar = document.getElementById("bar"),
  status = document.getElementById("status");
function say(text, kind) {{ status.textContent = text; status.className = kind || ""; }}
form.addEventListener("submit", (e) => {{
  e.preventDefault();
  const f = file.files[0];
  if (!f) return;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/backups/upload");
  xhr.setRequestHeader("X-Upload-Token", {token});
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (ev) => {{ if (ev.lengthComputable) {{
    bar.value = 100 * ev.loaded / ev.total;
    say("Uploading: " + Math.round(bar.value) + "%"); }} }};
  xhr.onload = () => {{
    go.disabled = false;
    let r = {{}}; try {{ r = JSON.parse(xhr.responseText); }} catch (_) {{}}
    if (xhr.status === 200)
      say("Uploaded as " + r.name + ". Restore it from Settings → Maintenance.", "good");
    else say("Not uploaded: " + (r.error || ("HTTP " + xhr.status)), "error");
  }};
  xhr.onerror = () => {{
    go.disabled = false; say("The upload stopped (connection lost).", "error"); }};
  go.disabled = true; bar.hidden = false; say("Uploading…");
  xhr.send(f);
}});
</script>
</body></html>
"""


async def upload_page(request: Request):
    if not await run_in_threadpool(_admin, request):
        return RedirectResponse("/signin", 303)
    token, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    body = PAGE.format(nonce=nonce, token=json.dumps(token))   # [A-Za-z0-9_-] only
    response = HTMLResponse(body, headers={
        "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": (f"default-src 'none'; script-src 'nonce-{nonce}'; "
                                    "style-src 'unsafe-inline'; connect-src 'self'; "
                                    "form-action 'self'; frame-ancestors 'none'; "
                                    "base-uri 'none'")})
    response.set_cookie(UPLOAD_COOKIE, token, max_age=6 * 3600, path="/backups/upload",
                        httponly=True, secure=_secure(request), samesite="strict")
    return response


def _keep(partial) -> str:
    """Keep an uploaded file that is a backup, under its own time; refuse anything else."""
    try:
        manifest = backup.read_manifest(partial)
    except backup.BackupError:
        partial.unlink(missing_ok=True)
        raise
    stamp = re.sub(r"[^0-9TZ]", "", manifest.get("name", "")[11:27])
    if not re.fullmatch(r"\d{8}T\d{6}Z", stamp):
        stamp = backup._stamp()
    directory = partial.parent
    for n in range(1, 1000):
        name = f"igs-backup-{stamp}-uploaded{'' if n == 1 else f'-{n}'}.tar.gz"
        if not (directory / name).exists():
            os.replace(partial, directory / name)
            return name
    partial.unlink(missing_ok=True)
    raise backup.BackupError("too many uploads of the same backup")


async def upload(request: Request):
    if not await run_in_threadpool(_admin, request):
        return JSONResponse({"error": "sign in as an administrator"}, 403)
    expected = request.cookies.get(UPLOAD_COOKIE, "")
    if (not _same_origin(request) or not expected
            or not secrets.compare_digest(expected, request.headers.get("x-upload-token", ""))):
        return JSONResponse({"error": "the upload page had expired; open it again"}, 400)
    directory = backup.backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / f"upload-{secrets.token_hex(8)}.partial"
    written, check_at = 0, 0
    try:
        with partial.open("wb") as out:
            os.chmod(partial, 0o600)
            async for chunk in request.stream():
                await run_in_threadpool(out.write, chunk)
                written += len(chunk)
                if written >= check_at:            # every 256 MB
                    check_at = written + (256 << 20)
                    if shutil.disk_usage(directory).free < MIN_FREE:
                        raise backup.BackupError("the server's disk is nearly full")
        name = await run_in_threadpool(_keep, partial)
    except backup.BackupError as exc:
        partial.unlink(missing_ok=True)
        return JSONResponse({"error": str(exc)}, 422)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return JSONResponse({"name": name, "bytes": written})


def routes() -> list[Route]:
    return [Route("/backups/download/{name}", download, methods=["GET"]),
            Route("/backups/upload", upload_page, methods=["GET"]),
            Route("/backups/upload", upload, methods=["POST"])]
