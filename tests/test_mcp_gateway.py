"""Read-only Gmail policy and bounded stdio transport; no real credentials."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core.mcp_gateway import MCPGateway, MCPServer, _render_result


class GatewayTests(unittest.TestCase):
    def test_gmail_mutations_blocked_even_when_called_directly(self):
        server = MCPServer('gmail', {'command': 'npx'})
        server.ready = True
        with patch.object(server, '_request') as request:
            for tool in ('send_email', 'delete_email', 'batch_delete_emails', 'modify_email',
                         'draft_email', 'create_filter', 'download_attachment', 'new_unknown_tool'):
                self.assertIn('not permitted', server.call(tool, {}))
            request.assert_not_called()

    def test_package_alias_cannot_bypass_read_only(self):
        server = MCPServer('mail', {'args': ['@gongrzhe/server-gmail-autoauth-mcp']})
        self.assertIn('not permitted', server.call('send_email', {}))

    def test_search_capped_and_empty_result_not_done(self):
        server = MCPServer('gmail', {})
        server.ready = True
        with patch.object(server, '_request', return_value={'content': [{'type': 'text', 'text': ''}]}) as call:
            self.assertEqual(server.call('search_emails', {'query': 'is:unread', 'maxResults': 100}), 'No matching emails.')
            self.assertEqual(call.call_args.args[1]['arguments']['maxResults'], 5)

    def test_missing_credentials_never_spawns_or_authenticates(self):
        server = MCPServer('gmail', {'command': 'npx'})
        with patch('pathlib.Path.is_file', return_value=False), patch('subprocess.Popen') as spawn:
            self.assertFalse(server.start())
        spawn.assert_not_called()
        self.assertIn('not authenticated', server.error)

    def test_disabled_booting_and_missing_servers_return_immediately(self):
        gateway = MCPGateway(Path('unused'))
        for enabled in (True, False):
            server = MCPServer('gmail', {'enabled': enabled})
            gateway.servers['gmail'] = server
            with patch.object(server, 'start') as start:
                self.assertIn('unavailable', gateway.call('gmail', 'read_email', {}))
                start.assert_not_called()
            self.assertIn('gmail', gateway.catalog_for_prompt())
        self.assertIn('No such', gateway.call('missing', 'read_email', {}))

    def test_vercel_blocks_gmail_even_if_config_enabled(self):
        with patch.dict(os.environ, {'VERCEL': '1'}), patch('subprocess.Popen') as spawn:
            server = MCPServer('gmail', {'command': 'npx', 'enabled': True})
            self.assertFalse(server.start())
            spawn.assert_not_called()

    def test_other_servers_keep_their_tools_and_results(self):
        for name in ('github', 'playwright'):
            server = MCPServer(name, {'command': 'npx'})
            server.ready = True
            with patch.object(server, '_request', return_value={'content': [{'type': 'text', 'text': 'OK'}]}) as request:
                self.assertEqual(server.call('some_write_tool', {}), 'OK')
                request.assert_called_once()
        self.assertEqual(_render_result({'isError': True, 'content': [{'type': 'text', 'text': 'bad'}]}), 'ERROR: bad')

    def test_stdio_handshake_filters_catalog_and_returns_tool_result(self):
        script = '''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 result={'tools':[{'name':n} for n in ['search_emails','read_email','send_email','delete_email']]}
 if m['method']=='tools/call': result={'content':[{'type':'text','text':'Subject: Test'}]}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp) / 'credentials.json'
            token.write_text('{}')
            server = MCPServer('gmail', {'command': sys.executable, 'args': ['-u', '-c', script],
                               'env': {'GMAIL_CREDENTIALS_PATH': str(token)}}, logger=lambda _: None)
            try:
                self.assertTrue(server.start())
                self.assertEqual([t['name'] for t in server.tools], ['search_emails', 'read_email'])
                self.assertEqual(server.call('read_email', {'messageId': 'abc'}), 'Subject: Test')
            finally:
                server.stop()

    def test_silent_process_has_real_handshake_deadline(self):
        server = MCPServer('test', {'command': sys.executable, 'args': ['-c', 'import time; time.sleep(10)']}, logger=lambda _: None)
        before = time.monotonic()
        with patch('core.mcp_gateway.START_TIMEOUT', 0.1):
            self.assertFalse(server.start())
        self.assertLess(time.monotonic() - before, 3)
        self.assertIn('no reply', server.error)

    def test_gmail_errors_do_not_return_upstream_secrets(self):
        server = MCPServer('gmail', {})
        server.ready = True
        with patch.object(server, '_request', return_value={'isError': True, 'content': [{'type': 'text', 'text': 'SECRET_TOKEN'}]}):
            result = server.call('read_email', {})
            self.assertIn('authentication', result)
            self.assertNotIn('SECRET_TOKEN', result)

    def test_slow_server_does_not_delay_other_server_start(self):
        gateway = MCPGateway(Path('unused'))
        release, fast_started = threading.Event(), threading.Event()
        slow = MCPServer('playwright', {})
        fast = MCPServer('gmail', {})
        gateway.servers = {'playwright': slow, 'gmail': fast}
        try:
            with patch.object(gateway, 'load'), \
                 patch.object(slow, 'start', side_effect=lambda: release.wait(2)), \
                 patch.object(fast, 'start', side_effect=lambda: fast_started.set()):
                gateway.start_all()
                self.assertTrue(fast_started.wait(1))
                self.assertFalse(gateway._started.is_set())
                release.set()
                gateway.wait_ready(2)
                self.assertTrue(gateway._started.is_set())
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
