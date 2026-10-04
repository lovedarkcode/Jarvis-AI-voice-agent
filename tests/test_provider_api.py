"""BYOK endpoints: mock provider responses; never use real credentials."""
import unittest
import base64
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from fastapi import HTTPException
from server.provider_api import app, MODELS

KEYS = {
    'openai_key': 'sk-' + 'a' * 32,
    'claude_key': 'sk-ant-' + 'b' * 32,
    'sarvam_key': 'c' * 32,
    'gemini_key': 'AIza' + 'd' * 32,
}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_empty_keys_cannot_start(self):
        for keys in ({}, {'openai_key': '', 'claude_key': '', 'sarvam_key': '', 'gemini_key': ''}):
            response = self.client.post('/api/keys/verify', json=keys)
            self.assertEqual(response.status_code, 400)

    def test_each_provider_can_start_alone(self):
        for provider in ('openai', 'claude', 'sarvam', 'gemini'):
            with self.subTest(provider=provider), patch('server.provider_api.provider_call',
                    new=AsyncMock(return_value={'data': [{'id': MODELS[provider]}]})) as call:
                response = self.client.post('/api/keys/verify', json={provider + '_key': KEYS[provider + '_key']})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['providers'], [provider])
                self.assertEqual(call.await_count, 1)
                self.assertEqual(call.call_args.args[0], provider)
                self.assertNotIn(KEYS[provider + '_key'], response.text)

    def test_failed_optional_provider_does_not_block_valid_key(self):
        async def result(provider, *args, **kwargs):
            if provider == 'openai':
                raise HTTPException(401, 'Rejected')
            return {}
        with patch('server.provider_api.provider_call', side_effect=result):
            response = self.client.post('/api/keys/verify', json={
                'openai_key': KEYS['openai_key'], 'sarvam_key': KEYS['sarvam_key']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['providers'], ['sarvam'])
        self.assertEqual(len(response.json()['warnings']), 1)

    def test_verification_calls_each_provider_and_returns_only_metadata(self):
        async def result(provider, *args, **kwargs):
            return {'data': [{'id': MODELS[provider]}]} if provider != 'sarvam' else {'audios': ['sample']}
        with patch('server.provider_api.provider_call', side_effect=result) as call:
            response = self.client.post('/api/keys/verify', json=KEYS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(call.call_count, 4)
        for key in KEYS.values():
            self.assertNotIn(key, response.text)

    def test_provider_auth_errors_are_redacted_and_report_401(self):
        upstream = httpx.Response(401, json={'error': KEYS['openai_key']})
        with patch('httpx.AsyncClient.request', new=AsyncMock(return_value=upstream)):
            response = self.client.post('/api/chat', json={'provider': 'openai', 'key': KEYS['openai_key'],
                                                         'messages': [{'role': 'user', 'content': 'Hi'}]})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['provider'], 'openai')
        self.assertNotIn(KEYS['openai_key'], response.text)

    def test_quota_errors_do_not_invalidate_credentials(self):
        with patch('httpx.AsyncClient.request', new=AsyncMock(return_value=httpx.Response(429))):
            response = self.client.post('/api/chat', json={'provider': 'claude', 'key': KEYS['claude_key'],
                                                         'messages': [{'role': 'user', 'content': 'Hi'}]})
        self.assertEqual(response.status_code, 429)

    def test_upstream_errors_identify_operation_and_status_without_exposing_body(self):
        for status, expected in [(400, 'parameters'), (402, 'credits'), (403, 'permissions'),
                                 (404, 'endpoint or model'), (422, 'parameters'), (500, 'temporarily')]:
            with self.subTest(status=status), patch('httpx.AsyncClient.request',
                    new=AsyncMock(return_value=httpx.Response(status, json={'error': KEYS['sarvam_key']}))):
                response = self.client.post('/api/keys/verify', json={'sarvam_key': KEYS['sarvam_key']})
                self.assertEqual(response.status_code, 401 if status == 403 else 502 if status == 500 else status)
                self.assertEqual(response.json()['upstream_status'], status)
                self.assertEqual(response.json()['operation'], 'chat')
                self.assertIn(expected, response.json()['error'])
                self.assertNotIn(KEYS['sarvam_key'], response.text)

    def test_voice_errors_identify_the_failing_stage(self):
        with patch('httpx.AsyncClient.request', new=AsyncMock(return_value=httpx.Response(422))):
            speech = self.client.post('/api/speech', json={'key': KEYS['gemini_key'], 'text': 'Hello'})
            transcription = self.client.post('/api/transcribe', data={'key': KEYS['gemini_key']},
                files={'audio': ('recording.webm', b'audio', 'audio/webm')})
        self.assertEqual(speech.json()['operation'], 'speech playback')
        self.assertEqual(transcription.json()['operation'], 'speech recognition')
        self.assertEqual(speech.json()['provider'], 'gemini')
        self.assertEqual(transcription.json()['provider'], 'gemini')
        self.assertEqual(speech.json()['upstream_status'], 422)
        self.assertEqual(transcription.json()['upstream_status'], 422)

    def test_all_chat_adapters(self):
        cases = [
            ('openai', {'output': [{'content': [{'type': 'output_text', 'text': 'Hello'}]}]}),
            ('claude', {'content': [{'type': 'text', 'text': 'Hello'}]}),
            ('sarvam', {'choices': [{'message': {'content': 'Hello'}}]}),
            ('gemini', {'candidates': [{'content': {'parts': [{'text': 'Hello'}]}}]}),
        ]
        for provider, output in cases:
            with self.subTest(provider=provider), patch('server.provider_api.provider_call', new=AsyncMock(return_value=output)) as call:
                response = self.client.post('/api/chat', json={'provider': provider, 'key': KEYS[provider + '_key'],
                    'messages': [{'role': 'user', 'content': 'Hi'}]})
                self.assertEqual(response.json()['text'], 'Hello')
                if provider == 'openai':
                    self.assertFalse(call.call_args.kwargs['json']['store'])

    def test_gemini_transcription_and_speech(self):
        with patch('server.provider_api.provider_call', new=AsyncMock(return_value={
                'candidates': [{'content': {'parts': [{'text': 'Hello'}]}}]})) as call:
            response = self.client.post('/api/transcribe', data={'key': KEYS['gemini_key']},
                                        files={'audio': ('recording.webm', b'audio', 'audio/webm')})
        self.assertEqual(response.json()['text'], 'Hello')
        self.assertEqual(call.call_args.args[0], 'gemini')
        self.assertIn('speech recognition', call.call_args.kwargs['operation'])
        pcm = b'\x00\x01' * 4
        encoded = base64.b64encode(pcm).decode()
        with patch('server.provider_api.provider_call', new=AsyncMock(return_value={
                'candidates': [{'content': {'parts': [{'inlineData': {
                    'mimeType': 'audio/L16;rate=24000', 'data': encoded}}]}}]})) as call:
            response = self.client.post('/api/speech', json={'key': KEYS['gemini_key'], 'text': 'Hello'})
        audio = base64.b64decode(response.json()['audios'][0])
        self.assertTrue(audio.startswith(b'RIFF'))
        self.assertIn(pcm, audio)
        self.assertEqual(call.call_args.args[0], 'gemini')
        self.assertEqual(call.call_args.kwargs['operation'], 'speech playback')

    def test_request_limits_and_cache_headers(self):
        response = self.client.post('/api/chat', content=b'x' * 4_000_001)
        self.assertEqual(response.status_code, 413)
        response = self.client.get('/api/healthz')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json()['version'], 'byok-v4')
        self.assertIn('gemini', response.json()['providers'])

    def test_new_gemini_key_format_is_accepted(self):
        aq_key = 'AQ.' + 'e' * 40
        with patch('server.provider_api.provider_call', new=AsyncMock(return_value={'candidates': []})):
            response = self.client.post('/api/keys/verify', json={'gemini_key': aq_key})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['providers'], ['gemini'])
        self.assertNotIn(aq_key, response.text)


if __name__ == '__main__':
    unittest.main()
