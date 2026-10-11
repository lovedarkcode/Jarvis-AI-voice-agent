"""Local Google consent flow; tokens use the Gmail MCP server's file format."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import httpx

REDIRECT = 'http://localhost:3000/oauth2callback'
SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'


class GmailAuth:
    def __init__(self, gateway):
        self.gateway = gateway
        self.phase = 'idle'
        self.message = ''
        self.account = ''
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None

    def status(self):
        server = self.gateway.servers.get('gmail')
        return {'phase': self.phase, 'message': self.message, 'account': self.account,
                'ready': bool(server and server.ready and self.phase not in ('waiting', 'connecting', 'error'))}

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            server = self.gateway.servers.get('gmail')
            if os.getenv('VERCEL') or not server or not server.enabled:
                return self._error('Enable Gmail in mcp_servers.json and run Jarvis locally first.')
            directory = Path.home() / '.gmail-mcp'
            key_path = Path(server.env.get('GMAIL_OAUTH_PATH') or os.getenv('GMAIL_OAUTH_PATH') or directory / 'gcp-oauth.keys.json')
            self._credentials = Path(server.env.get('GMAIL_CREDENTIALS_PATH') or os.getenv('GMAIL_CREDENTIALS_PATH') or directory / 'credentials.json')
            try:
                keys = json.loads(key_path.read_text(encoding='utf-8'))
                self._client = keys.get('installed') or keys['web']
                if not self._client.get('client_id') or not self._client.get('client_secret'):
                    raise ValueError('Missing client')
            except (OSError, ValueError, KeyError):
                return self._error('Save your Google OAuth client file as ~/.gmail-mcp/gcp-oauth.keys.json first. See GMAIL-MCP.md.')
            self._state = secrets.token_urlsafe(32)
            self._verifier = secrets.token_urlsafe(64)
            self._cancel.clear()
            owner = self

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # Authorization codes must never enter access logs.

                def do_GET(self):
                    parsed = urlsplit(self.path)
                    query = parse_qs(parsed.query)
                    valid = (bool(owner._state) and parsed.path == '/oauth2callback' and
                             secrets.compare_digest(query.get('state', [''])[0], owner._state))
                    if not valid:
                        code, message = 400, 'Invalid sign-in response. Return to Jarvis and try again.'
                    else:
                        owner._state = ''  # A callback can be consumed only once.
                        owner._finish(query)
                        code, message = 200, owner.message + ' You can close this tab and return to Jarvis.'
                        owner._cancel.set()
                    data = message.encode('utf-8')
                    self.send_response(code)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            try:
                class Listener(HTTPServer):
                    def get_request(self):
                        connection, address = super().get_request()
                        connection.settimeout(5)
                        return connection, address

                listener = Listener(('127.0.0.1', 3000), Callback)
                listener.timeout = 0.5
            except OSError:
                return self._error('Gmail sign-in needs port 3000. Close any previous Gmail auth command and retry.')
            challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b'=').decode()
            url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
                'client_id': self._client['client_id'], 'redirect_uri': REDIRECT,
                'response_type': 'code', 'scope': SCOPE, 'access_type': 'offline',
                'prompt': 'select_account consent', 'state': self._state,
                'code_challenge': challenge, 'code_challenge_method': 'S256',
            })
            self.phase, self.message = 'waiting', 'Choose your Gmail account in Google sign-in and allow read-only access.'
            self._thread = threading.Thread(target=self._serve, args=(listener, url), daemon=True)
            self._thread.start()
            return self.status()

    def _error(self, message):
        self.phase, self.message = 'error', message
        return self.status()

    def _serve(self, listener, url):
        try:
            if not webbrowser.open(url):
                self._error('Could not open Google sign-in. Set a default browser and click Connect Gmail again.')
                return
            deadline = time.monotonic() + 300
            while not self._cancel.is_set() and time.monotonic() < deadline:
                listener.handle_request()
            if self.phase == 'waiting':
                self._error('Gmail sign-in timed out or was cancelled. Click Connect Gmail to retry. If Google says the app is being tested, add this account as a test user in Google Cloud.')
        except Exception:
            self._error('Gmail sign-in could not complete. Please retry.')
        finally:
            listener.server_close()

    def _finish(self, query):
        if query.get('error') or not query.get('code'):
            self._error('Google did not grant Gmail access. Try again with an allowed test account and approve access.')
            return
        self.phase, self.message = 'connecting', 'Connecting your Gmail account...'
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post('https://oauth2.googleapis.com/token', data={
                    'client_id': self._client['client_id'], 'client_secret': self._client['client_secret'],
                    'code': query['code'][0], 'code_verifier': self._verifier,
                    'grant_type': 'authorization_code', 'redirect_uri': REDIRECT,
                })
                response.raise_for_status()
                token = response.json()
                if not token.get('access_token') or not token.get('refresh_token'):
                    raise ValueError('Incomplete token')
                profile = client.get('https://gmail.googleapis.com/gmail/v1/users/me/profile',
                                     headers={'Authorization': 'Bearer ' + token['access_token']})
                profile.raise_for_status()
                account = profile.json()['emailAddress']
            token['expiry_date'] = int((time.time() + token.pop('expires_in', 3600)) * 1000)
            if self._cancel.is_set():
                return
            # Atomic replacement preserves the previous account if consent fails.
            self._credentials.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self._credentials.parent, prefix='.gmail-token-')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as output:
                    json.dump(token, output)
                os.replace(temporary, self._credentials)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.account = account
            if self._cancel.is_set():
                return
            if not self.gateway.restart_gmail():
                self._error('Google connected, but Gmail MCP could not start. Check Node.js/npx and retry Connect Gmail.')
                return
            self.phase, self.message = 'connected', 'Gmail connected: ' + account
        except Exception:
            self._error('Gmail authorization failed. Check Gmail API access, your test-user account and the localhost redirect URI, then retry.')

    def close(self):
        self._cancel.set()
        if self._thread:
            self._thread.join(timeout=2)
