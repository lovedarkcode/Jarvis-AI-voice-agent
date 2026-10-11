"""BYOK endpoints: mock provider responses; never use real credentials."""
import unittest
import base64
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from fastapi import HTTPException
from server.provider_api import app, MODELS, RECOVERY

KEYS = {
    'openai_key': 'sk-' + 'a' * 32,
    'claude_key': 'sk-ant-' + 'b' * 32,
    'sarvam_key': 'c' * 32,
    'gemini_key': 'AIza' + 'd' * 32,
}
GEMINI_MODEL = {'name': 'models/' + MODELS['gemini'], 'supportedGenerationMethods': ['generateContent']}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_empty_keys_cannot_start(self):
        for keys in ({}, {'openai_key': '', 'claude_key': '', 'sarvam_key': '', 'gemini_key': ''}):
            response = self.client.post('/api/keys/verify', json=keys)
            self.assertEqual(response.status_code, 400)

    def test_local_routes_serve_the_same_setup_page_as_production(self):
        deployed_page = self.client.get('/demo.html')
        for route in ('/', '/login', '/login.html'):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, deployed_page.content)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertIn('charset=utf-8', response.headers['content-type'])
        self.assertIn('id="key-gate"', deployed_page.text)

    def test_each_provider_can_start_alone(self):
        for provider in ('openai', 'claude', 'sarvam', 'gemini'):
            with self.subTest(provider=provider), patch('server.provider_api.provider_call',
                    new=AsyncMock(return_value=GEMINI_MODEL if provider == 'gemini' else
                        {'data': [{'id': MODELS[provider]}]})) as call:
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
            if provider == 'gemini':
                return GEMINI_MODEL
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
                self.assertEqual(response.json()['operation'], 'model verification')
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
        self.assertEqual(call.call_args.args[2], f'/models/{MODELS["stt"]}:generateContent')
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

    def test_sarvam_transcription_and_speech_use_only_sarvam(self):
        with patch('httpx.AsyncClient.request', new=AsyncMock(return_value=httpx.Response(200,
                json={'transcript': ' नमस्ते '}))) as call:
            response = self.client.post('/api/transcribe',
                data={'provider': 'sarvam', 'key': KEYS['sarvam_key'], 'language': 'hi-IN'},
                files={'audio': ('recording.wav', b'audio', 'audio/wav')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'text': 'नमस्ते', 'provider': 'sarvam'})
        self.assertEqual(call.await_args.args[1], 'https://api.sarvam.ai/speech-to-text')
        self.assertEqual(call.await_args.kwargs['headers'], {'api-subscription-key': KEYS['sarvam_key']})
        self.assertEqual(call.await_args.kwargs['data'],
            {'model': MODELS['sarvam_stt'], 'language_code': 'hi-IN', 'mode': 'transcribe'})
        self.assertEqual(call.await_args.kwargs['files']['file'], ('recording.wav', b'audio', 'audio/wav'))

        encoded = base64.b64encode(b'RIFFaudio').decode()
        with patch('httpx.AsyncClient.request', new=AsyncMock(return_value=httpx.Response(200,
                json={'audios': [encoded]}))) as call:
            response = self.client.post('/api/speech', json={
                'provider': 'sarvam', 'key': KEYS['sarvam_key'], 'text': 'नमस्ते', 'language': 'hi-IN'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'audios': [encoded], 'provider': 'sarvam'})
        self.assertEqual(call.await_args.args[1], 'https://api.sarvam.ai/text-to-speech')
        self.assertEqual(call.await_args.kwargs['headers'], {'api-subscription-key': KEYS['sarvam_key']})
        self.assertEqual(call.await_args.kwargs['json'], {
            'text': 'नमस्ते', 'language_code': 'hi-IN', 'model': MODELS['sarvam_tts'],
            'speech_sample_rate': 24000, 'output_audio_codec': 'wav'})
        self.assertNotIn(KEYS['sarvam_key'], response.text)

    def test_sarvam_voice_errors_remain_redacted_and_identify_operation(self):
        for upstream_status in (401, 403, 422, 429):
            with self.subTest(status=upstream_status), patch('httpx.AsyncClient.request',
                    new=AsyncMock(return_value=httpx.Response(upstream_status,
                        json={'error': KEYS['sarvam_key']}))):
                speech = self.client.post('/api/speech', json={
                    'provider': 'sarvam', 'key': KEYS['sarvam_key'], 'text': 'Hi'})
                transcription = self.client.post('/api/transcribe',
                    data={'provider': 'sarvam', 'key': KEYS['sarvam_key']},
                    files={'audio': ('recording.wav', b'audio', 'audio/wav')})
            for response, operation in ((speech, 'speech playback'), (transcription, 'speech recognition')):
                self.assertEqual(response.status_code, 401 if upstream_status in (401, 403) else upstream_status)
                self.assertEqual(response.json()['provider'], 'sarvam')
                self.assertEqual(response.json()['operation'], operation)
                self.assertNotIn(KEYS['sarvam_key'], response.text)

    def test_voice_rejects_unsupported_provider_language_and_uploads(self):
        with patch('server.provider_api.provider_call', new=AsyncMock()) as call:
            for fields, raw, mime, expected in (
                    ({'provider': 'claude'}, b'audio', 'audio/wav', 400),
                    ({'provider': 'sarvam', 'language': 'invalid'}, b'audio', 'audio/wav', 400),
                    ({'provider': 'sarvam'}, b'audio', 'text/plain', 400),
                    ({'provider': 'sarvam'}, b'', 'audio/wav', 413),
                    ({'provider': 'sarvam'}, b'a' * 3_500_001, 'audio/wav', 413)):
                response = self.client.post('/api/transcribe', data={'key': KEYS['sarvam_key'], **fields},
                    files={'audio': ('recording.wav', raw, mime)})
                self.assertEqual(response.status_code, expected)
            response = self.client.post('/api/speech', json={
                'provider': 'claude', 'key': KEYS['claude_key'], 'text': 'Hi'})
            self.assertEqual(response.status_code, 422)
            call.assert_not_awaited()

    def test_sarvam_empty_and_malformed_voice_responses(self):
        with patch('server.provider_api.provider_call', new=AsyncMock(return_value={'transcript': ''})):
            response = self.client.post('/api/transcribe', data={'provider': 'sarvam', 'key': KEYS['sarvam_key']},
                files={'audio': ('recording.wav', b'audio', 'audio/wav')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['text'], '')
        for output in ({}, {'audios': []}, {'audios': [None]}):
            with patch('server.provider_api.provider_call', new=AsyncMock(return_value=output)):
                response = self.client.post('/api/speech', json={
                    'provider': 'sarvam', 'key': KEYS['sarvam_key'], 'text': 'Hi'})
            self.assertEqual(response.status_code, 502)

    def test_request_limits_and_cache_headers(self):
        response = self.client.post('/api/chat', content=b'x' * 4_000_001)
        self.assertEqual(response.status_code, 413)
        response = self.client.get('/api/healthz')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json()['version'], 'byok-v4')
        self.assertIn('gemini', response.json()['providers'])

    def test_new_gemini_key_format_is_accepted(self):
        aq_key = 'AQ.' + 'e' * 40
        with patch('server.provider_api.provider_call', new=AsyncMock(return_value=GEMINI_MODEL)):
            response = self.client.post('/api/keys/verify', json={'gemini_key': aq_key})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['providers'], ['gemini'])
        self.assertNotIn(aq_key, response.text)

    def test_gemini_verification_checks_model_access_without_generating_text(self):
        with patch('httpx.AsyncClient.request', new=AsyncMock(
                return_value=httpx.Response(200, json=GEMINI_MODEL))) as call:
            response = self.client.post('/api/keys/verify', json={'gemini_key': KEYS['gemini_key']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['providers'], ['gemini'])
        self.assertEqual(call.await_args.args, ('GET',
            f'https://generativelanguage.googleapis.com/v1beta/models/{MODELS["gemini"]}'))
        self.assertNotIn('json', call.await_args.kwargs)

    def test_gemini_verification_recovers_from_transient_network_and_service_failures(self):
        request = httpx.Request('GET', 'https://generativelanguage.googleapis.com')
        cases = [httpx.ConnectError('offline', request=request),
                 httpx.ReadTimeout('timeout', request=request), httpx.Response(503), httpx.Response(502)]
        for failure in cases:
            with self.subTest(failure=type(failure).__name__), patch('httpx.AsyncClient.request',
                    new=AsyncMock(side_effect=[failure, httpx.Response(200, json=GEMINI_MODEL)])) as call, \
                    patch('server.provider_api.asyncio.sleep', new=AsyncMock()) as sleep:
                response = self.client.post('/api/keys/verify', json={'gemini_key': KEYS['gemini_key']})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(call.await_count, 2)
            self.assertEqual(sleep.await_count, 1)

    def test_gemini_verification_bounds_retries_and_never_accepts_an_unreachable_provider(self):
        for failure in (httpx.ConnectError(KEYS['gemini_key']), httpx.Response(503)):
            with self.subTest(failure=type(failure).__name__), patch('httpx.AsyncClient.request',
                    new=AsyncMock(side_effect=[failure] * 3)) as call, \
                    patch('server.provider_api.asyncio.sleep', new=AsyncMock()):
                response = self.client.post('/api/keys/verify', json={'gemini_key': KEYS['gemini_key']})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(call.await_count, 3)
            self.assertNotIn(KEYS['gemini_key'], response.text)

    def test_gemini_verification_does_not_retry_auth_quota_or_configuration_errors(self):
        for status in (400, 401, 403, 404, 429):
            with self.subTest(status=status), patch('httpx.AsyncClient.request', new=AsyncMock(
                    return_value=httpx.Response(status))) as call:
                response = self.client.post('/api/keys/verify', json={'gemini_key': KEYS['gemini_key']})
            self.assertEqual(response.status_code, 401 if status in (401, 403) else status)
            self.assertEqual(call.await_count, 1)

    def test_gemini_verification_rejects_models_without_chat_support(self):
        with patch('httpx.AsyncClient.request', new=AsyncMock(return_value=httpx.Response(200,
                json={'supportedGenerationMethods': ['embedContent']}))):
            response = self.client.post('/api/keys/verify', json={'gemini_key': KEYS['gemini_key']})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['provider'], 'gemini')

    def test_live_token_is_short_lived_and_hides_the_key(self):
        aq_key = 'AQ.' + 'e' * 40
        upstream = httpx.Response(200, json={'name': 'auth_tokens/short-lived'})
        with patch('httpx.AsyncClient.request', new=AsyncMock(return_value=upstream)) as call:
            response = self.client.post('/api/live/token', json={'key': aq_key, 'language': 'en-IN'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['token'], 'auth_tokens/short-lived')
        self.assertEqual(response.json()['model'], 'gemini-3.8-live')
        self.assertNotIn(aq_key, response.text)
        self.assertIn('/v1beta/auth_tokens', call.await_args.args[1])
        self.assertEqual(call.await_args.kwargs['headers']['x-goog-api-key'], aq_key)
        payload = call.await_args.kwargs['json']
        self.assertEqual(payload['uses'], 1)
        self.assertNotIn('bidiGenerateContentSetup', payload)
        setup = response.json()['setup']
        self.assertEqual(setup['model'], 'models/' + MODELS['live'])
        self.assertEqual(setup['generationConfig']['responseModalities'], ['AUDIO'])
        self.assertIn('Prefer en-IN', setup['systemInstruction']['parts'][0]['text'])

    def test_gemini_key_formats_cover_chat_transcription_and_speech(self):
        for prefix in ('AIza', 'AQ.'):
            key = prefix + 'e' * 40
            pcm = b'\x00\x01' * 4
            output = {'candidates': [{'content': {'parts': [
                {'text': 'Hello'}, {'inlineData': {'mimeType': 'audio/L16;rate=24000',
                    'data': base64.b64encode(pcm).decode()}}]}}]}
            with self.subTest(prefix=prefix), patch('httpx.AsyncClient.request',
                    new=AsyncMock(return_value=httpx.Response(200, json=output))) as call:
                chat = self.client.post('/api/chat', json={'provider': 'gemini', 'key': key,
                    'messages': [{'role': 'user', 'content': 'Hi'}]})
                transcription = self.client.post('/api/transcribe', data={'provider': 'gemini', 'key': key},
                    files={'audio': ('recording.wav', b'audio', 'audio/wav')})
                speech = self.client.post('/api/speech', json={'provider': 'gemini', 'key': key, 'text': 'Hello'})
            self.assertEqual(chat.status_code, 200)
            self.assertEqual(transcription.status_code, 200)
            self.assertEqual(speech.status_code, 200)
            for request in call.await_args_list:
                self.assertTrue(request.args[1].startswith('https://generativelanguage.googleapis.com/v1beta/'))
                self.assertEqual(request.kwargs['headers'], {'x-goog-api-key': key})
                self.assertNotIn(key, request.args[1])
            voice = call.await_args.kwargs['json']['generationConfig']['speechConfig']['voiceConfig']
            self.assertEqual(voice, {'voice': 'Kore'})
            for response in (chat, transcription, speech):
                self.assertNotIn(key, response.text)

    def test_gemini_legacy_tts_override_preserves_voice_schema(self):
        with patch.dict(MODELS, {'tts': 'gemini-2.5-flash-preview-tts'}), patch(
                'server.provider_api.provider_call', new=AsyncMock(return_value={})) as call:
            self.client.post('/api/speech', json={'key': KEYS['gemini_key'], 'text': 'Hello'})
        voice = call.await_args.kwargs['json']['generationConfig']['speechConfig']['voiceConfig']
        self.assertEqual(voice, {'prebuiltVoiceConfig': {'voiceName': 'Kore'}})

    def test_both_voice_providers_bound_transcription_retries_and_keep_errors_redacted(self):
        for provider in ('gemini', 'sarvam'):
            key = KEYS[provider + '_key']
            with self.subTest(provider=provider), patch('httpx.AsyncClient.request',
                    new=AsyncMock(side_effect=httpx.ReadTimeout(key))) as call, \
                    patch('server.provider_api.asyncio.sleep', new=AsyncMock()):
                response = self.client.post('/api/transcribe', data={'provider': provider, 'key': key},
                    files={'audio': ('question.wav', b'original recording', 'audio/wav')})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(call.await_count, 3)
            self.assertTrue(response.json()['retryable'])
            self.assertEqual(response.json()['failure'], 'timeout')
            self.assertNotIn(key, response.text)

    def test_voice_recovery_does_not_retry_rejected_keys_billing_quota_or_validation(self):
        for provider in ('gemini', 'sarvam'):
            for status in (400, 401, 402, 403, 422, 429):
                with self.subTest(provider=provider, status=status), patch('httpx.AsyncClient.request',
                        new=AsyncMock(return_value=httpx.Response(status))) as call:
                    response = self.client.post('/api/transcribe',
                        data={'provider': provider, 'key': KEYS[provider + '_key']},
                        files={'audio': ('question.wav', b'original recording', 'audio/wav')})
                self.assertEqual(call.await_count, 1)
                self.assertFalse(response.json()['retryable'])

    def test_total_attempt_deadline_cancels_a_stalled_upstream_request(self):
        import asyncio

        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()

        with patch.dict(RECOVERY, {'speech recognition': (3, 0.01)}), \
                patch('httpx.AsyncClient.request', side_effect=stalled) as call, \
                patch('server.provider_api.asyncio.sleep', new=AsyncMock()):
            response = self.client.post('/api/transcribe', data={'provider': 'gemini', 'key': KEYS['gemini_key']},
                files={'audio': ('question.wav', b'audio', 'audio/wav')})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(call.await_count, 3)
        self.assertEqual(response.json()['failure'], 'timeout')


if __name__ == '__main__':
    unittest.main()
