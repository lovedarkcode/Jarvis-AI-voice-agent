"""Check web startup without opening a GUI, audio device, or network connection."""
import asyncio
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from api.index import app as deployed_app
from server.local_runner import main
from server.provider_api import app as local_app


class LocalRunnerTests(unittest.TestCase):
    def test_local_and_deployed_apps_are_the_same_instance(self):
        self.assertIs(local_app, deployed_app)

    def test_main_defaults_to_web_without_importing_desktop_dependencies(self):
        entry = Path(__file__).resolve().parents[1] / 'main.py'
        with patch('server.local_runner.main', return_value=0) as launch, \
             patch.object(sys, 'argv', [str(entry)]), \
             patch.dict(sys.modules, {'sounddevice': None, 'ui': None}):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(entry), run_name='__main__')
        self.assertEqual(result.exception.code, 0)
        launch.assert_called_once_with()

    def launch(self, args, started=True):
        config = Mock()
        events = []

        class Server:
            def __init__(self, settings):
                events.append(settings)
                self.started = started

            async def startup(self, sockets=None):
                pass

            def run(self):
                asyncio.run(self.startup())

        with patch.dict(sys.modules, {'uvicorn': SimpleNamespace(Server=Server, Config=config)}), \
             patch('server.local_runner.webbrowser.open') as open_browser:
            self.assertEqual(main(args), 0)
        return config, events, open_browser

    def test_default_launch_opens_browser_after_server_startup(self):
        config, events, browser = self.launch([])
        config.assert_called_once_with('api.index:app', host='127.0.0.1', port=8765)
        self.assertEqual(len(events), 1)
        browser.assert_called_once_with('http://127.0.0.1:8765/')

    def test_no_browser_and_custom_port(self):
        config, _, browser = self.launch(['--port', '8800', '--no-browser'])
        config.assert_called_once_with('api.index:app', host='127.0.0.1', port=8800)
        browser.assert_not_called()

    def test_failed_startup_does_not_open_browser(self):
        _, _, browser = self.launch([], started=False)
        browser.assert_not_called()

    def test_invalid_port_is_rejected(self):
        with self.assertRaises(SystemExit) as result:
            main(['--port', '70000', '--no-browser'])
        self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
