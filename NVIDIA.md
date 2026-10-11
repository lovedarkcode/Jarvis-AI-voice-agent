# NVIDIA Nemotron in Jarvis

On the initial key screen, enter your `nvapi-...` key in **NVIDIA Nemotron**,
connect, and select NVIDIA. No Gemini or Sarvam key is required.
Keys remain in browser memory and are sent only to the corresponding provider.
The web interface does not read `.env` keys automatically.

Supported operations:

- Chat with `nvidia/nemotron-3.5-lightning-30b-a3b`.
- PDF, Word, Excel and text document questions through the existing extraction/RAG path.
- Local Gmail search and summaries after [Gmail authorization](GMAIL-MCP.md).
- English voice input through NVIDIA Parakeet and spoken replies through Magpie.

The voice models are separate from Nemotron and use the same NVIDIA key.
The key must have access to those hosted speech endpoints. NVIDIA-only voice
currently supports English; Audio settings restrict the language accordingly.
Sarvam and Gemini keep their existing language and voice support.
Gmail remains local-only; the other operations are available in the web backend.

## Run

```powershell
python -m pip install -r server/requirements.txt
python main.py
```

Reload the browser after restarting the server. Enter the NVIDIA key, allow the
microphone, attach a document, and ask a question. Turning off **Read responses
aloud** keeps voice input available without speech synthesis.

NVIDIA verification generates a short response from the configured model and
can consume quota. Successful chat verification does not guarantee speech access.
Speech failures report the failing stage without exposing upstream error bodies
or discarding a working chat key for speech permission errors.

The chat adapter uses NVIDIA's OpenAI-compatible HTTP API. Extended thinking is
disabled and output is bounded for interactive use. Only final answer content
is returned, never `reasoning_content`. Speech recognition uploads the complete
PCM WAV to NVIDIA's HTTP endpoint; synthesis uses its Riva gRPC endpoint with
a deadline. Long replies are split at sentence/word boundaries into segments of
at most 600 characters, then their PCM audio is combined into one 16 kHz WAV.
This leaves room under Magpie's 2,000-character normalized-input limit and keeps
audio responses smaller. If normalization still exceeds the limit, the affected
segment is split again. Transient unavailable/deadline errors get one retry;
the complete synthesis request has an 80-second budget. Server dependencies
include `nvidia-riva-client`.

Optional **process environment** overrides (no secrets):

```powershell
$env:JARVIS_NVIDIA_MODEL = 'nvidia/nemotron-3.5-lightning-30b-a3b'
$env:JARVIS_NVIDIA_ASR_FUNCTION_ID = '1598d209-5e27-4d3c-8079-4751568b1081'
$env:JARVIS_NVIDIA_TTS_FUNCTION_ID = '877104f7-e885-42b9-8de8-f6e4c6303969'
$env:JARVIS_NVIDIA_TTS_VOICE = 'Magpie-Multilingual.EN-US.Jason'
```

Deployment IDs can change. Check NVIDIA's current
[Nemotron API](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b),
[Parakeet API](https://build.nvidia.com/nvidia/parakeet-ctc-1_1b-asr/api), and
[Magpie API](https://build.nvidia.com/nvidia/magpie-tts-multilingual/api).
Hosted quotas still apply; open model weights do not imply unlimited hosted usage.

## Tests

```powershell
python -m unittest discover -s tests -p "test_*.py"
node --test tests/web-gate.test.cjs tests/web-vault.test.cjs
```

Automated tests use synthetic audio, fake keys and mocked providers. They cover
NVIDIA-only setup, provider isolation, documents, Gmail context, speech formats,
deadlines, permission/quota errors and existing Sarvam/Gemini behavior.
