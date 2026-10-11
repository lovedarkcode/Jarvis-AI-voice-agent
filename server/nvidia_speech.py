"""NVIDIA hosted Riva speech, with per-request credentials and bounded RPCs."""
import io
import os
import re
import time
import wave

import httpx

from fastapi import HTTPException

ASR_FUNCTION = os.getenv('JARVIS_NVIDIA_ASR_FUNCTION_ID', '1598d209-5e27-4d3c-8079-4751568b1081')
TTS_FUNCTION = os.getenv('JARVIS_NVIDIA_TTS_FUNCTION_ID', '877104f7-e885-42b9-8de8-f6e4c6303969')
TTS_VOICE = os.getenv('JARVIS_NVIDIA_TTS_VOICE', 'Magpie-Multilingual.EN-US.Aria')
TTS_CHUNK_CHARS = 600  # Headroom for NVIDIA's 2,000 normalized-character limit.
TTS_SAMPLE_RATE = 16000  # Reduce long spoken answers' HTTP payload size.
TTS_BUDGET = 80  # Total deadline across chunks and retries, below the browser's 100s.


def _text_chunks(text, limit=TTS_CHUNK_CHARS):
    text = ' '.join(text.split())
    while len(text) > limit:
        boundaries = list(re.finditer(r'(?<=[.!?])\s+', text[:limit + 1]))
        cut = boundaries[-1].start() if boundaries else text.rfind(' ', 0, limit + 1)
        if cut <= 0:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip()
    if text:
        yield text


def _input_too_long(exc):
    # Inspect only for classification; never return provider details to the user.
    detail = (exc.details() or '').lower()
    return 'maximum input length' in detail or ('input text length' in detail and 'exceeds' in detail)


def _language(language):
    if language != 'en-IN':
        raise HTTPException(400, {'error': 'NVIDIA voice currently supports English. Select English in Audio settings.',
                                  'provider': 'nvidia'})
    return 'en-US'


def _rpc(key, function_id, operation, build):
    try:
        import grpc
        import riva.client
    except ImportError:
        raise HTTPException(503, {'error': 'NVIDIA speech requires the server dependencies. Install server/requirements.txt and restart.',
                                  'provider': 'nvidia', 'operation': operation}) from None
    auth = riva.client.Auth(uri='grpc.nvcf.nvidia.com:443', use_ssl=True,
                            metadata_args=[['function-id', function_id], ['authorization', 'Bearer ' + key]])
    try:
        return build(riva.client, auth)
    except grpc.RpcError as exc:
        code = exc.code()
        status = 403 if code in (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED) else \
                 429 if code == grpc.StatusCode.RESOURCE_EXHAUSTED else 502
        retryable = code in (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE)
        if _input_too_long(exc):
            status = 400
        reason = ('could not normalize this text for speech. Try a shorter spoken reply.' if status == 400 else
                  'denied access to this speech endpoint. Check NVIDIA speech permissions.' if status == 403 else
                  'quota or rate limit reached.' if status == 429 else
                  'endpoint was not found. Check the configured NVIDIA speech function ID.' if code == grpc.StatusCode.NOT_FOUND else
                  'rejected the speech parameters.' if code == grpc.StatusCode.INVALID_ARGUMENT else
                  'could not complete. Please retry.')
        raise HTTPException(status, {'error': 'NVIDIA ' + operation + ' ' + reason,
                                     'provider': 'nvidia', 'operation': operation, 'retryable': retryable,
                                     'upstream_code': code.name}) from None
    finally:
        auth.channel.close()


def recognize(key, raw, language):
    language = _language(language)
    try:
        with wave.open(io.BytesIO(raw), 'rb') as recording:
            if recording.getnchannels() != 1 or recording.getsampwidth() != 2 or recording.getcomptype() != 'NONE':
                raise ValueError('Unsupported WAV')
            rate = recording.getframerate()
            pcm = recording.readframes(recording.getnframes())
            if not pcm or rate not in (16000, 48000):
                raise ValueError('Unsupported sample rate')
    except (wave.Error, EOFError, ValueError):
        raise HTTPException(400, 'NVIDIA transcription requires mono 16-bit PCM WAV at 16 or 48 kHz.') from None

    # NVIDIA's hosted HTTP endpoint accepts the complete WAV container. The
    # hosted gRPC Recognize endpoint can return an empty result for valid PCM.
    try:
        with httpx.Client(timeout=httpx.Timeout(25, connect=5), follow_redirects=False) as client:
            response = client.post(
                f'https://{ASR_FUNCTION}.invocation.api.nvcf.nvidia.com/v1/audio/transcriptions',
                headers={'Authorization': 'Bearer ' + key}, data={'language': language},
                files={'file': ('recording.wav', raw, 'audio/wav')})
    except httpx.RequestError:
        raise HTTPException(502, {'error': 'NVIDIA speech recognition could not be reached. Please retry.',
                                  'provider': 'nvidia', 'operation': 'speech recognition', 'retryable': True}) from None
    if not response.is_success:
        reason = ('denied access. Check NVIDIA speech permissions.' if response.status_code in (401, 403) else
                  'quota or rate limit reached.' if response.status_code == 429 else
                  'could not complete. Check the speech endpoint and retry.')
        status = response.status_code if response.status_code in (401, 403, 429) else 502
        raise HTTPException(status, {'error': 'NVIDIA speech recognition ' + reason, 'provider': 'nvidia',
                                     'operation': 'speech recognition', 'retryable': response.status_code >= 500})
    try:
        text = response.json()['text']
        if not isinstance(text, str):
            raise ValueError('Invalid transcript')
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, {'error': 'NVIDIA returned an unreadable transcript.', 'provider': 'nvidia'}) from None
    return text.strip()


def synthesize(key, text, language):
    language = _language(language)

    def call(client, auth):
        import grpc
        from riva.client.proto.riva_tts_pb2 import SynthesizeSpeechRequest
        stub = client.SpeechSynthesisService(auth).stub
        deadline = time.monotonic() + TTS_BUDGET

        def render(chunk):
            request = SynthesizeSpeechRequest(text=chunk, language_code=language,
                encoding=client.AudioEncoding.LINEAR_PCM, sample_rate_hz=TTS_SAMPLE_RATE, voice_name=TTS_VOICE)
            for attempt in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HTTPException(502, {'error': 'NVIDIA speech playback timed out. Please retry.',
                        'provider': 'nvidia', 'operation': 'speech playback', 'retryable': True})
                try:
                    response = stub.Synthesize(request, metadata=auth.get_auth_metadata(), timeout=min(45, remaining))
                    break
                except grpc.RpcError as exc:
                    # Normalization can greatly expand numeric PDF content.
                    # Split again instead of repeating the rejected input.
                    if _input_too_long(exc) and len(chunk) > 1:
                        return b''.join(render(part) for part in _text_chunks(chunk, max(1, len(chunk) // 2)))
                    if attempt == 0 and exc.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                        continue
                    raise
            if not response.audio:
                raise HTTPException(502, {'error': 'NVIDIA returned no speech audio.', 'provider': 'nvidia',
                                          'operation': 'speech playback', 'retryable': True})
            return response.audio

        pcm = b''.join(render(chunk) for chunk in _text_chunks(text))
        if not pcm:
            raise HTTPException(400, 'Enter text to speak.')
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(TTS_SAMPLE_RATE)
            wav.writeframes(pcm)
        return output.getvalue()
    return _rpc(key, TTS_FUNCTION, 'speech playback', call)
