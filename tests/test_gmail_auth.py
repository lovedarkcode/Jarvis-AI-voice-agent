import json
from http.server import HTTPServer
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import httpx

from server.gmail_auth import GmailAuth, SCOPE
from core.mcp_gateway import MCPGateway, MCPServer


class GmailAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.gateway = Mock()
        self.gateway.servers = {'gmail': SimpleNamespace(enabled=True, ready=False, env={})}
        self.auth = GmailAuth(self.gateway)
        self.auth._client = {'client_id': 'client', 'client_secret': 'secret'}
        self.auth._verifier = 'verifier'
        self.auth._credentials = self.path / 'credentials.json'

    def test_success_stores_mcp_tokens_and_restarts_only_after_profile_validation(self):
        client = Mock()
        client.post.return_value.json.return_value = {'access_token': 'access', 'refresh_token': 'refresh', 'expires_in': 3600}
        client.get.return_value.json.return_value = {'emailAddress': 'chosen@example.com'}
        with patch('server.gmail_auth.httpx.Client') as factory:
            factory.return_value.__enter__.return_value = client
            self.auth._finish({'code': ['code']})
        self.assertEqual(self.auth.phase, 'connected')
        self.assertEqual(self.auth.account, 'chosen@example.com')
        saved = json.loads(self.auth._credentials.read_text())
        self.assertIn('expiry_date', saved)
        self.assertNotIn('expires_in', saved)
        self.gateway.restart_gmail.assert_called_once()
        self.assertNotIn('refresh', str(self.auth.status()))
        self.assertEqual(client.post.call_args.kwargs['data']['code_verifier'], 'verifier')
        self.assertTrue(SCOPE.endswith('gmail.readonly'))

    def test_denial_preserves_previous_credentials(self):
        self.auth._credentials.write_text('old account')
        self.auth._finish({'error': ['access_denied']})
        self.assertEqual(self.auth.phase, 'error')
        self.assertEqual(self.auth._credentials.read_text(), 'old account')
        self.gateway.restart_gmail.assert_not_called()

    def test_token_failure_does_not_leak_upstream_secrets(self):
        with patch('server.gmail_auth.httpx.Client', side_effect=ValueError('secret-token')):
            self.auth._finish({'code': ['secret-code']})
        self.assertEqual(self.auth.phase, 'error')
        self.assertNotIn('secret', self.auth.message)
        self.assertFalse(self.auth._credentials.exists())

    def test_disabled_and_hosted_never_open_browser(self):
        with patch('server.gmail_auth.webbrowser.open') as browser:
            self.gateway.servers['gmail'].enabled = False
            self.assertEqual(self.auth.start()['phase'], 'error')
            self.gateway.servers['gmail'].enabled = True
            with patch.dict('os.environ', {'VERCEL': '1'}):
                self.assertEqual(self.auth.start()['phase'], 'error')
            browser.assert_not_called()

    def test_restart_keeps_other_servers_and_read_only_policy(self):
        gateway = MCPGateway(self.path / 'unused.json')
        old = MCPServer('gmail', {'command': 'npx', 'args': ['gmail'], 'enabled': True})
        other = Mock()
        gateway.servers = {'gmail': old, 'playwright': other}
        with patch.object(MCPServer, 'start', return_value=True):
            self.assertTrue(gateway.restart_gmail())
        self.assertIs(gateway.servers['playwright'], other)
        other.stop.assert_not_called()
        self.assertIsNot(gateway.servers['gmail'], old)
        self.assertEqual(gateway.servers['gmail'].allowed_tools, {'search_emails', 'read_email'})

    def test_browser_flow_selects_account_and_rejects_wrong_state(self):
        keys = self.path / 'keys.json'
        keys.write_text(json.dumps({'web': self.auth._client}))
        self.gateway.servers['gmail'].env = {'GMAIL_OAUTH_PATH': str(keys)}
        listeners = []
        original_init = HTTPServer.__init__

        def bind_ephemeral(listener, address, handler):
            original_init(listener, ('127.0.0.1', 0), handler)
            listeners.append(listener)

        with patch.object(HTTPServer, '__init__', bind_ephemeral), patch('server.gmail_auth.webbrowser.open', return_value=True) as browser:
            self.auth.start()
            try:
                # start() spawns a thread; wait for its browser-open notification.
                import time
                for _ in range(100):
                    if browser.called:
                        break
                    time.sleep(.01)
                params = parse_qs(urlsplit(browser.call_args.args[0]).query)
                self.assertEqual(params['prompt'], ['select_account consent'])
                self.assertEqual(params['scope'], [SCOPE])
                self.assertEqual(params['code_challenge_method'], ['S256'])
                url = 'http://127.0.0.1:' + str(listeners[0].server_port) + '/oauth2callback'
                with httpx.Client(trust_env=False) as client:
                    response = client.get(url, params={'state': 'wrong', 'code': 'private'})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.auth.phase, 'waiting')
                    response = client.get(url, params={'state': params['state'][0], 'error': 'access_denied'})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(self.auth.phase, 'error')
                    self.assertNotIn(params['state'][0], response.text)
                self.gateway.restart_gmail.assert_not_called()
            finally:
                self.auth.close()


if __name__ == '__main__':
    unittest.main()
