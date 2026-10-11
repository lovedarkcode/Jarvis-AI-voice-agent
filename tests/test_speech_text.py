import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from server.provider_api import app
from server.speech_text import spoken_text


class SpeechTextTests(unittest.TestCase):
    def test_markdown_email_summary_becomes_plain_speech(self):
        text = '## Your inbox\n1. **Angel One** — Your portfolio is ₹475.65 (-4.87%).\n2. *Zoom* — [Join workshop](https://example.com/long-link).'
        self.assertEqual(spoken_text(text), 'Your inbox Angel One — Your portfolio is ₹475.65 (-4.87%). Zoom — Join workshop.')

    def test_html_links_and_code_are_not_read_aloud(self):
        self.assertEqual(spoken_text('<b>Hello</b> &amp; welcome. https://example.com\n```python\nsecret()\n```'),
                         'Hello & welcome. Code omitted.')

    def test_nvidia_receives_clean_text(self):
        with patch('server.nvidia_speech.synthesize', return_value=b'audio') as synthesize:
            response = TestClient(app).post('/api/speech', json={
                'provider': 'nvidia', 'key': 'nvapi-' + 'a' * 30,
                'text': '1. **Alex** wants a meeting.', 'language': 'en-IN'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(synthesize.call_args.args[1], 'Alex wants a meeting.')

    def test_sarvam_receives_clean_text(self):
        call = AsyncMock(return_value={'audios': ['audio']})
        with patch('server.provider_api.provider_call', call):
            response = TestClient(app).post('/api/speech', json={
                'provider': 'sarvam', 'key': 'a' * 30,
                'text': '**Alex** wants a meeting.', 'language': 'en-IN'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(call.call_args.kwargs['json']['text'], 'Alex wants a meeting.')
