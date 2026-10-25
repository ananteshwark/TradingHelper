"""Authenticated loopback gateway from the public app to local Bionic inference.

Install beside bridge.json, tunnel_ed25519 and pinned known_hosts. No cloud fallback.
"""
import hmac
import http.server
import json
import pathlib
import subprocess
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = {}
BIONIC = 'http://127.0.0.1:1235'
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BUSY = threading.BoundedSemaphore(1)


def bionic_request(path, data=None):
    headers = {'Content-Type': 'application/json'}
    if CONFIG.get('bionic_api_key'):
        headers['Authorization'] = 'Bearer ' + CONFIG['bionic_api_key']
    request = urllib.request.Request(BIONIC + path, data=data, headers=headers)
    return NO_PROXY.open(request, timeout=600 if data is not None else 10)


def loaded_models():
    with bionic_request('/api/v0/models') as response:
        catalog = json.load(response)
    return [row['id'] for row in catalog.get('data', [])
            if row.get('type') == 'llm' and row.get('state') == 'loaded'
            and row.get('compatibility_type') == 'gguf' and isinstance(row.get('id'), str)]


def adapt_body(body):
    """Bionic accepts json_schema rather than the legacy json_object format.

    Preserve the application's schema from its system prompt, including the GPU lab's
    existing prompt. Never interpret filing/user text as configuration or instructions.
    """
    if not isinstance(body, dict):
        raise ValueError('request must be an object')
    if body.get('stream'):
        raise ValueError('Use non-streaming analytics requests')
    if (body.get('response_format') or {}).get('type') == 'json_object':
        schema = {'type': 'object'}
        for message in body.get('messages', []):
            if message.get('role') != 'system' or not isinstance(message.get('content'), str):
                continue
            for marker in ('Return only JSON matching this schema:\n', 'JSON schema:\n'):
                if marker in message['content']:
                    candidate = json.loads(message['content'].rsplit(marker, 1)[1])
                    if isinstance(candidate, dict):
                        schema = candidate
        body = {**body, 'response_format': {'type': 'json_schema',
                'json_schema': {'name': 'result', 'schema': schema, 'strict': False}}}
    return body


class Gateway(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log credentials or prompts.

    def reply(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_request(self):
        if not hmac.compare_digest(self.headers.get('Authorization', ''),
                                   'Bearer ' + CONFIG['api_token']):
            self.reply(401, {'error': 'Private AI authentication required'})
            return
        if (self.command, self.path) not in {('GET', '/v1/models'),
                                           ('POST', '/v1/chat/completions')}:
            self.reply(404, {'error': 'Unsupported private AI route'})
            return
        acquired = False
        try:
            models = loaded_models()
            if self.command == 'GET':
                self.reply(200, {'object': 'list', 'data': [
                    {'id': model, 'object': 'model', 'owned_by': 'local-bionic'}
                    for model in models]})
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8 * 1024 * 1024:
                self.reply(413, {'error': 'Invalid or oversized request'})
                return
            body = adapt_body(json.loads(self.rfile.read(length)))
            if body.get('model') not in models:
                self.reply(503, {'error': 'Configured local model is not loaded in Bionic'})
                return
            acquired = BUSY.acquire(blocking=False)
            if not acquired:
                self.reply(503, {'error': 'Local AI is busy; retry later'})
                return
            with bionic_request(self.path, json.dumps(body).encode()) as response:
                self.reply(response.status, json.load(response))
        except urllib.error.HTTPError as exc:
            self.reply(exc.code, {'error': 'Bionic rejected the request'})
        except (ValueError, TypeError, AttributeError):
            self.reply(400, {'error': 'Invalid local inference request or response'})
        except (OSError, RuntimeError, urllib.error.URLError):
            self.reply(503, {'error': 'Bionic is unavailable; no cloud fallback'})
        finally:
            if acquired:
                BUSY.release()

    do_GET = handle_request
    do_POST = handle_request


def main():
    global CONFIG
    CONFIG = json.loads((ROOT / 'bridge.json').read_text(encoding='utf-8-sig'))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 18081), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # A forced Windows task stop can leave its SSH child alive. Once this process
    # owns the gateway port, clear only tunnels using this bridge's dedicated key.
    cleanup = (r"Get-CimInstance Win32_Process -Filter \"Name = 'ssh.exe'\" | "
               r"Where-Object { $_.CommandLine -like '*TradingHelperBridge\tunnel_ed25519*' "
               r"-and $_.CommandLine -like '*trading-ai-tunnel@95.135.254.85*' } | "
               r"ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }")
    cleanup = cleanup.replace('\\"', '"')
    subprocess.run([r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
                    '-NoProfile', '-NonInteractive', '-Command', cleanup],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    command = [r'C:\Windows\System32\OpenSSH\ssh.exe', '-N', '-T',
               '-i', str(ROOT / 'tunnel_ed25519'), '-o', 'BatchMode=yes',
               '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UserKnownHostsFile=' + str(ROOT / 'known_hosts'),
               '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=20',
               '-o', 'ServerAliveCountMax=3', '-o', 'ConnectTimeout=15',
               '-R', '127.0.0.1:18080:127.0.0.1:18081',
               'trading-ai-tunnel@95.135.254.85']
    while True:
        # SSH output contains connection diagnostics only; rotate the small log.
        log = ROOT / 'tunnel.log'
        if log.exists() and log.stat().st_size > 1024 * 1024:
            log.replace(ROOT / 'tunnel.previous.log')
        with log.open('ab') as output:
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(15)


if __name__ == '__main__':
    main()
