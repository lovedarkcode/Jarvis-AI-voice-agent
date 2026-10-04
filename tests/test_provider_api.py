"""BYOK endpoints: mock provider responses; never use real credentials."""
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from fastapi import HTTPException
from server.provider_api import app, MODELS

KEYS = {'openai_key': 'sk-' + 'a' * 32, 'claude_key': 'sk-ant-' + 'b' * 32, 'sarvam_key': 'c' * 32}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_empty_keys_cannot_start(self):
        for keys in ({}, {'openai_key': '', 'claude_key': '', 'sarvam_key': ''}):
            response = self.client.post('/api/keys/verify', json=keys)
            self.assertEqual(response.status_code, 400)

    def test_each_provider_can_start_alone(self):
        for provider in ('openai', 'claude', 'sarvam'):
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
        self.assertEqual(call.call_count, 3)
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

    def test_all_chat_adapters(self):
        for provider, output in [('openai', {'output': [{'content': [{'type': 'output_text', 'text': 'Hello'}]}]}),
                                 ('claude', {'content': [{'type': 'text', 'text': 'Hello'}]}),
                                 ('sarvam', {'choices': [{'message': {'content': 'Hello'}}]})]:
            with self.subTest(provider=provider), patch('server.provider_api.provider_call', new=AsyncMock(return_value=output)) as call:
                response = self.client.post('/api/chat', json={'provider': provider, 'key': KEYS[provider + '_key'],
                    'messages': [{'role': 'user', 'content': 'Hi'}]})
                self.assertEqual(response.json()['text'], 'Hello')
                if provider == 'openai':
                    self.assertFalse(call.call_args.kwargs['json']['store'])

    def test_sarvam_transcription_and_speech(self):
        with patch('server.provider_api.provider_call', new=AsyncMock(return_value={'transcript': 'Hello'})):
            response = self.client.post('/api/transcribe', data={'key': KEYS['sarvam_key']},
                                        files={'audio': ('recording.webm', b'audio', 'audio/webm')})
        self.assertEqual(response.json()['text'], 'Hello')
        with patch('server.provider_api.provider_call', new=AsyncMock(return_value={'audios': ['wav']})):
            response = self.client.post('/api/speech', json={'key': KEYS['sarvam_key'], 'text': 'Hello'})
        self.assertEqual(response.json()['audios'], ['wav'])

    def test_request_limits_and_cache_headers(self):
        response = self.client.post('/api/chat', content=b'x' * 4_000_001)
        self.assertEqual(response.status_code, 413)
        response = self.client.get('/api/healthz')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json()['version'], 'byok-v2')


if __name__ == '__main__':
    unittest.main()
