"""NVIDIA adapter tests use fake RPCs and keys; no network calls."""
import base64
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from fastapi import HTTPException
from fastapi.testclient import TestClient
from server import nvidia_speech
from server.provider_api import app, pcm_wav

KEY = 'nvapi-' + 'x' * 40


class NvidiaTests(unittest.TestCase):
    def test_verification_exercises_selected_model_with_bearer_key(self):
        import httpx
        with patch('httpx.AsyncClient.request', new=AsyncMock(return_value=httpx.Response(200,
                json={'choices': [{'message': {'content': 'Hi'}}]}))) as upstream:
            response = TestClient(app).post('/api/keys/verify', json={'nvidia_key': KEY})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['providers'], ['nvidia'])
        self.assertEqual(upstream.call_args.args[:2], ('POST', 'https://integrate.api.nvidia.com/v1/chat/completions'))
        self.assertEqual(upstream.call_args.kwargs['headers']['Authorization'], 'Bearer ' + KEY)
        self.assertFalse(upstream.call_args.kwargs['json']['chat_template_kwargs']['enable_thinking'])
        self.assertNotIn(KEY, response.text)

    def test_riva_wav_transcription_and_deadline(self):
        import httpx
        audio = pcm_wav(b'\x00\x01' * 800, 16000)
        with patch('server.nvidia_speech.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value = httpx.Response(200, json={'text': 'Hello'})
            result = nvidia_speech.recognize(KEY, audio, 'en-IN')
        self.assertEqual(result, 'Hello')
        request = client.return_value.__enter__.return_value.post.call_args
        self.assertEqual(request.kwargs['files']['file'][1], audio)
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer ' + KEY)
        self.assertEqual(request.kwargs['data']['language'], 'en-US')
        self.assertEqual(client.call_args.kwargs['timeout'].read, 25)

    def test_http_transcription_failures_do_not_expose_upstream_body(self):
        import httpx
        audio = pcm_wav(b'\x00\x01' * 800, 16000)
        for status in (401, 403, 429, 500):
            with patch('server.nvidia_speech.httpx.Client.post', return_value=httpx.Response(status, text=KEY)), \
                 self.assertRaises(HTTPException) as caught:
                nvidia_speech.recognize(KEY, audio, 'en-IN')
            self.assertNotIn(KEY, str(caught.exception.detail))
            self.assertEqual(caught.exception.detail['retryable'], status >= 500)

    def test_tts_returns_playable_wav_with_deadline(self):
        auth = Mock()
        synthesize = Mock(return_value=SimpleNamespace(audio=b'\x00\x01' * 800))
        with patch('riva.client.Auth', return_value=auth), \
             patch('riva.client.SpeechSynthesisService', return_value=SimpleNamespace(stub=SimpleNamespace(Synthesize=synthesize))):
            result = nvidia_speech.synthesize(KEY, 'Hello', 'en-IN')
        with wave.open(io.BytesIO(result)) as wav:
            self.assertEqual(wav.getframerate(), nvidia_speech.TTS_SAMPLE_RATE)
            self.assertEqual(wav.getnframes(), 800)
        self.assertEqual(synthesize.call_args.kwargs['timeout'], 45)
        auth.channel.close.assert_called_once()

    def test_long_pdf_reply_is_split_without_losing_text_and_audio_is_joined(self):
        text = ('The document describes project requirements and the delivery schedule. ' * 36)[:2500]
        auth = Mock()
        sent, audio = [], []
        def speak(request, **kwargs):
            sent.append(request.text)
            self.assertLessEqual(len(request.text), nvidia_speech.TTS_CHUNK_CHARS)
            pcm = bytes([len(sent), 0]) * 20
            audio.append(pcm)
            return SimpleNamespace(audio=pcm)
        with patch('riva.client.Auth', return_value=auth), \
             patch('riva.client.SpeechSynthesisService', return_value=SimpleNamespace(stub=SimpleNamespace(Synthesize=speak))):
            result = nvidia_speech.synthesize(KEY, text, 'en-IN')
        self.assertGreater(len(sent), 1)
        self.assertEqual(' '.join(sent), ' '.join(text.split()))
        with wave.open(io.BytesIO(result)) as wav:
            self.assertEqual(wav.readframes(wav.getnframes()), b''.join(audio))
        auth.channel.close.assert_called_once()

    def test_normalization_limit_splits_again_instead_of_retrying_same_text(self):
        import grpc
        class TooLong(grpc.RpcError):
            def code(self): return grpc.StatusCode.UNKNOWN
            def details(self): return 'Input text is larger than the maximum input length: 2300 > 2000.'
        sent = []
        def speak(request, **kwargs):
            sent.append(request.text)
            if len(request.text) > 100:
                raise TooLong()
            return SimpleNamespace(audio=b'\x01\x00' * 20)
        with patch('riva.client.Auth'), \
             patch('riva.client.SpeechSynthesisService', return_value=SimpleNamespace(stub=SimpleNamespace(Synthesize=speak))):
            result = nvidia_speech.synthesize(KEY, '1234567890 ' * 40, 'en-IN')
        self.assertTrue(result.startswith(b'RIFF'))
        self.assertGreater(len(sent), 1)
        self.assertLessEqual(len(sent[-1]), 100)

    def test_transient_speech_failure_retries_but_permission_failure_does_not(self):
        import grpc
        class Failure(grpc.RpcError):
            def __init__(self, code): self.status = code
            def code(self): return self.status
            def details(self): return 'redacted service failure'
        for code, expected in ((grpc.StatusCode.UNAVAILABLE, 2), (grpc.StatusCode.PERMISSION_DENIED, 1)):
            speak = Mock(side_effect=[Failure(code), SimpleNamespace(audio=b'\x00\x01')])
            with patch('riva.client.Auth'), \
                 patch('riva.client.SpeechSynthesisService', return_value=SimpleNamespace(stub=SimpleNamespace(Synthesize=speak))):
                if expected == 1:
                    with self.assertRaises(HTTPException): nvidia_speech.synthesize(KEY, 'Hello', 'en-IN')
                else:
                    self.assertTrue(nvidia_speech.synthesize(KEY, 'Hello', 'en-IN').startswith(b'RIFF'))
            self.assertEqual(speak.call_count, expected)

    def test_total_synthesis_deadline_applies_across_chunks(self):
        with patch('riva.client.Auth'), patch('riva.client.SpeechSynthesisService') as service, \
             patch('server.nvidia_speech.time.monotonic', side_effect=[0, nvidia_speech.TTS_BUDGET + 1]), \
             self.assertRaises(HTTPException) as caught:
            nvidia_speech.synthesize(KEY, 'Long reply. ' * 200, 'en-IN')
        self.assertTrue(caught.exception.detail['retryable'])
        service.return_value.stub.Synthesize.assert_not_called()

    def test_unbroken_text_is_bounded_and_preserved(self):
        text = 'x' * 2500
        chunks = list(nvidia_speech._text_chunks(text))
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(chunk) <= nvidia_speech.TTS_CHUNK_CHARS for chunk in chunks))

    def test_rpc_errors_are_sanitized_and_connection_closed(self):
        import grpc
        class Failure(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.PERMISSION_DENIED
            def details(self):
                return KEY
        auth = Mock()
        with patch('riva.client.Auth', return_value=auth), self.assertRaises(HTTPException) as caught:
            nvidia_speech._rpc(KEY, 'function', 'speech recognition', Mock(side_effect=Failure()))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertNotIn(KEY, str(caught.exception.detail))
        auth.channel.close.assert_called_once()

    def test_bad_audio_and_unsupported_language_rejected_before_rpc(self):
        with patch.object(nvidia_speech, '_rpc') as rpc:
            for audio, language in ((b'not audio', 'en-IN'), (b'not audio', 'hi-IN')):
                with self.assertRaises(HTTPException) as caught:
                    nvidia_speech.recognize(KEY, audio, language)
                self.assertEqual(caught.exception.status_code, 400)
            rpc.assert_not_called()

    def test_http_routes_keep_nvidia_key_and_provider(self):
        client = TestClient(app)
        with patch.object(nvidia_speech, 'recognize', return_value='Read my PDF') as stt:
            result = client.post('/api/transcribe', data={'provider': 'nvidia', 'key': KEY},
                                 files={'audio': ('recording.wav', pcm_wav(b'\x00\x01', 16000), 'audio/wav')})
            self.assertEqual(result.json(), {'text': 'Read my PDF', 'provider': 'nvidia'})
            self.assertEqual(stt.call_args.args[0], KEY)
        audio = pcm_wav(b'\x00\x01', 48000)
        with patch.object(nvidia_speech, 'synthesize', return_value=audio) as tts:
            result = client.post('/api/speech', json={'provider': 'nvidia', 'key': KEY, 'text': 'Answer'})
            self.assertEqual(base64.b64decode(result.json()['audios'][0]), audio)
            self.assertEqual(tts.call_args.args[0], KEY)


if __name__ == '__main__':
    unittest.main()
