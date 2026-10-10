# Hosted web application

The Vercel app serves `web/demo.html` and uses `api/index.py` →
`server/provider_api.py`. It is independent of the Qt desktop UI in `ui.py`.
The previous Gemini WebSocket demo is no longer the hosted entry point.

`python main.py` now runs the same `api.index:app` locally at
`http://127.0.0.1:8765/` and opens it in your browser. The root and login routes
serve `web/demo.html` locally, matching the Vercel rewrites. Both environments
ask for fresh API keys on every page load; the web app never reads keys from
`.env`. Use `python main.py --no-browser` to open the URL yourself, or
`python main.py --port 8800` to use another port.

The native assistant is available with `python main.py --desktop`. It has its
own desktop features and `.env` configuration; it is not the production web UI.

## Configuration and privacy

- At least one visitor key is required; the other fields can be blank: `claude_key`, `openai_key`, `sarvam_key`, `gemini_key`.
- The setup gate validates formats, then checks credentials against providers.
  OpenAI/Claude model listings must include the configured response model.
  Gemini and Sarvam verification each create a short response and can use credits.
  Failed optional providers are excluded without blocking a verified provider.
- Claude, OpenAI, Sarvam, and Gemini produce text responses. The provider selector also selects voice: Sarvam uses its own key for transcription, chat (including document context), and spoken replies; Gemini uses its own key for chat and voice, retaining Gemini Live. Selecting Claude or OpenAI disables voice instead of using another provider's key. Switching providers closes the old microphone/session and cancels pending requests.
- Credentials travel through same-origin HTTPS POST requests to fixed provider
  URLs. They are not written to server storage, logs, URLs, or response bodies.
- Keys are held in page memory only. Startup clears encrypted credential records
  created by older versions and never restores saved keys. Reloading or reopening
  the page requires fresh entry in both local and production environments.
  Theme, language and device preferences remain separate from credentials.
- A provider 401/403 clears active credentials and reopens the mandatory setup.
  Quota errors leave credentials intact. Settings can update or forget keys.

Model defaults: `gpt-4.1-mini`, `claude-sonnet-4-6`, `sarvam-105b`, `saaras:v3` (Sarvam speech recognition), `bulbul:v3` (Sarvam spoken replies), `gemini-flash-latest` (Gemini chat and speech recognition), `gemini-3.8-flash-lite-tts` (Gemini spoken replies), and `gemini-3.8-live` (Gemini Live).
Deployers can set `JARVIS_OPENAI_MODEL`, `JARVIS_CLAUDE_MODEL`, `JARVIS_SARVAM_MODEL`, `JARVIS_SARVAM_STT_MODEL`, `JARVIS_SARVAM_TTS_MODEL`, `JARVIS_GEMINI_MODEL`, `JARVIS_GEMINI_STT_MODEL`, `JARVIS_GEMINI_TTS_MODEL`, and `JARVIS_GEMINI_LIVE_MODEL` to models that
their users can access. No operator API key is required or injected into the UI.

## Web and desktop differences

The web UI defaults to a charcoal dark theme with violet accents. Users can
switch to a light theme; an explicit theme choice persists across visits.
The web UI includes saved light/dark appearance, key settings, audio-device
selection (where supported by the browser), speech language, activity, and full
screen. Voice starts automatically when the selected provider is Sarvam or Gemini.
Sarvam continuously listens, detects pauses, and sends WAV utterances (at most
about 25 seconds each) to its REST transcription endpoint, then uses the same
chat/document history and Sarvam TTS for the reply. It resumes listening after
the reply; microphone input is ignored during transcription and reply playback.
Gemini streams through Gemini Live when no document is attached. With an attachment,
Gemini uses transcription, the same document retrieval/chat endpoint, and TTS,
so spoken and typed questions see the same reference passages. Removing the
document restores Gemini Live. Voice mutes or resumes listening. Stop
cancels the current reply and mutes listening without ending the session. Listening
requires browser microphone permission. It does not change desktop audio or
desktop interruption handling.

Attachments support selectable-text PDF, Word `.docx`, Excel `.xlsx`/`.xlsm`,
and text/CSV/JSON files. Uploads are limited to 3.5 MB and 400,000 extracted
characters (up to 1,500 sections). Older `.doc`/`.xls` files must be converted
first; scanned PDFs need OCR. Wait for the filename to appear, then ask by voice
or text. Page, section and sheet labels are preserved for citations. Large
documents use the existing lexical retrieval index; summaries use passages
sampled throughout the document. General reading/access questions and queries
with no lexical match also receive representative document excerpts, with an
explicit attachment status; a missing passage never means the PDF is missing.
The current attachment overrides earlier replies claiming no file was uploaded.
No embedding API key is required.

Extraction runs in a temporary directory, deleted after the request. Extracted
segments live only in browser memory and are sent with each question, so
production does not depend on a persistent server session. Remove detaches
the document; New chat clears it and the conversation. Failed uploads keep
the previous document. Reloading clears the reference and requires fresh keys.

Computer automation, local plugins, wake-word detection, OS auto-start, and
desktop memory are desktop-only. This hosted provider chat does not currently
expose the old Gemini tool runner or Browser Link; it does not claim to execute
computer/browser actions or provide live web research.

## Verification and deployment

```sh
python -m unittest discover -s tests -p test_provider_api.py -v
python -m unittest discover -s tests -p test_local_runner.py -v
python -m unittest discover -s tests -p test_document_api.py -v
node --test tests/web-vault.test.cjs tests/web-gate.test.cjs
python main.py
```

Deploy the repository with its existing Vercel configuration. The root rewrite
and `/demo.html` both open the setup gateway. Do not run the legacy
`tools/build_web.py` over this UI; that script builds the separate desktop remote
dashboard. `.env` and desktop configuration are excluded from deployments.

Provider calls are mocked in automated tests. Real successful authentication,
account/model permissions, microphone hardware and billing require a live test
with the visitor's own keys. Application appearance persists independently of
credentials, including while the gateway is locked. Navigation icons are inline
SVG symbols, so they do not depend on emoji fonts or platform glyph support.

Voice API requests accept `provider` (`sarvam` or `gemini`) alongside `key` and
`language`. Requests omitting `provider` retain Gemini behavior for compatibility.
Sarvam uses the `api-subscription-key` header; REST TTS uses `language_code`, as
specified by the current [Sarvam TTS reference](https://docs.sarvam.ai/api-reference/text-to-speech/convert).
Transcription uses multipart `file`, `model`, `mode`, and `language_code` per the
[Sarvam STT reference](https://docs.sarvam.ai/api-reference/speech-to-text/transcribe).
