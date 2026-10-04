# Hosted web application

The Vercel app serves `web/demo.html` and uses `api/index.py` →
`server/provider_api.py`. It is independent of the Qt desktop UI in `ui.py`.
The previous Gemini WebSocket demo is no longer the hosted entry point.

## Configuration and privacy

- All three visitor keys are required: `openai_key`, `claude_key`, `sarvam_key`.
- The setup gate validates formats, then checks credentials against providers.
  OpenAI/Claude model listings must include the configured response model.
  Sarvam verification creates a short "Ready" speech sample and can use credits.
- OpenAI and Claude produce text responses; Sarvam transcribes recordings and
  synthesizes spoken replies. The provider selector switches response models.
- Credentials travel through same-origin HTTPS POST requests to fixed provider
  URLs. They are not written to server storage, logs, URLs, or response bodies.
- Session storage uses AES-GCM with a random tab-session key. The encryption
  material is in sessionStorage too: this protects against accidental plaintext
  storage, **not** malicious scripts executing on the same origin.
- Persistent storage uses AES-256-GCM with a random IV/salt and PBKDF2-SHA256
  (310,000 iterations). The passphrase is never stored or sent to the server.
  A new session must unlock the vault. Unlocked keys remain accessible to the
  application, so protection against XSS remains essential.
- A provider 401/403 clears saved credentials and reopens the mandatory setup.
  Quota errors leave credentials intact. Settings can update or forget keys.

Model defaults: `gpt-4.1-mini`, `claude-sonnet-4-6`, `saaras:v3`, `bulbul:v3`.
Deployers can set `JARVIS_OPENAI_MODEL` and `JARVIS_CLAUDE_MODEL` to models that
their users can access. No operator API key is required or injected into the UI.

## Web and desktop differences

The web UI includes saved light/dark appearance, key settings, audio-device
selection (where supported by the browser), speech language, activity, and full
screen. Voice uses explicit short recording turns: click **Voice**, then
**Send recording**; **Stop** aborts recording, requests and playback. Recording
requires browser microphone permission. It does not change desktop audio or
desktop interruption handling. Text attachments are limited to 24,000 characters.

Computer automation, local plugins, wake-word detection, OS auto-start, and
desktop memory are desktop-only. This hosted provider chat does not currently
expose the old Gemini tool runner or Browser Link; it does not claim to execute
computer/browser actions or provide live web research.

## Verification and deployment

```sh
python -m unittest discover -s tests -p test_provider_api.py -v
node --test tests/web-vault.test.cjs tests/web-gate.test.cjs
python -m uvicorn server.provider_api:app --host 127.0.0.1 --port 8765
```

Deploy the repository with its existing Vercel configuration. The root rewrite
and `/demo.html` both open the setup gateway. Do not run the legacy
`tools/build_web.py` over this UI; that script builds the separate desktop remote
dashboard. `.env` and desktop configuration are excluded from deployments.

Provider calls are mocked in automated tests. Real successful authentication,
account/model permissions, microphone hardware and billing require a live test
with the visitor's own keys. Application appearance persists independently of
credentials, including while the gateway is locked.
