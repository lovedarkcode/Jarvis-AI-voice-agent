# The public JARVIS demo

A hosted, shareable JARVIS: anyone with the link talks to it in their browser
and hears it answer. Your PC does not need to be on.

This is a **different application** from the desktop assistant, living in
`server/`. It is not the desktop build with a web skin, because that is not a
thing that can exist — read the next section before deploying, so you can
describe it accurately to the people you send it to.

---

## What it is and is not

The desktop JARVIS is valuable because it controls the machine it runs on:
volume, windows, the mouse, arbitrary code with your authority. A visitor
arriving over the internet has no such machine here — only a shared container in
a datacenter. Those capabilities are not withheld out of caution; they have
nothing to point at.

| Works in the demo | Does not, and cannot |
|---|---|
| Real spoken conversation, it talks back | Controlling the visitor's computer |
| Web search | Opening apps, volume, brightness |
| Reading a public page | Seeing anyone's screen |
| Exact calculation, dates, statistics | Files, shell, unrestricted Python |
| **Upload a document and ask about it** | Reading anything not uploaded |
| Typing instead of speaking | Wake word, always-on mic |
| Isolated per visitor | Any access to *your* machine |

If someone asks it to open their Spotify, it explains what the demo is and what
the full JARVIS does. It does not pretend.

---

## Document RAG

Visitors can attach a PDF, Word, Excel, CSV, JSON or text file and ask questions
about it. Retrieval is the same engine the desktop build uses — parse, chunk at
~1,100 characters, BM25 index, budgeted context — so a 340 KB document comes back
as a few relevant passages rather than as 340 KB of prompt.

Two things differ here, and both matter:

**Every visitor gets their own store.** `core.document_store.store` is a
module-level singleton, which is right for one person on their own machine and
wrong the moment strangers share a process — visitor A's CV would be answerable
by visitor B. `server/session_docs.py` gives each WebSocket its own
`DocumentStore` and its own temp directory, and destroys both when the socket
closes. Nothing survives the session.

**Memory is capped, because it is all RAM.** Chunk text, the lexical index and
(when enabled) ~1.1 MB of float32 vectors per 340 KB document all live in the
process, on a 512 MB machine, fed by anonymous uploads:

| Setting | Default |
|---|---|
| `DEMO_MAX_UPLOAD_MB` | 5 |
| `DEMO_MAX_DOCS` | 3 per session |
| `DEMO_MAX_DOC_CHARS` | 400,000 per session |

The character cap is checked twice — once on the raw bytes, once after parsing,
because a 2 MB PDF holds far more text than a 2 MB spreadsheet and only the
parser knows which it was.

**Embeddings are off by default** (`DOCUMENT_EMBEDDINGS=false`). They cost an API
call per batch of chunks against your key and add the vector memory above. BM25
alone answers "what does this document say about X" well enough for a demo. Set
it to `true` for semantic matching if you are happy with the cost.

---

## 1. Deploy the backend

Not Vercel. A conversation is one long WebSocket held open for minutes;
serverless functions are killed long before that. Use Fly.io, Railway or Render
— all have a free tier.

**Fly.io:**

```powershell
# one-time
iwr https://fly.io/install.ps1 -useb | iex
fly auth signup

fly launch --no-deploy --copy-config
fly secrets set GEMINI_API_KEY=your_key_here
fly deploy
```

`fly.toml` is already written. Your backend lands at
`https://jarvis-demo.fly.dev`.

Check it:

```powershell
curl https://jarvis-demo.fly.dev/healthz
```

```json
{"ok":true,"key_configured":true,"active":0,"budget_minutes_left":120.0,...}
```

`key_configured: false` means the secret did not take.

**Railway / Render:** point either at this repo. Both read the `Dockerfile`.
Set `GEMINI_API_KEY` in their environment settings. Render's free tier sleeps
when idle, so the first visitor after a quiet spell waits ~30s.

## 2. Deploy the front end

`web/demo.html` is plain static — put it anywhere, including the Vercel project
you already have:

```powershell
npx vercel --prod
```

Then open it pointed at your backend:

```
https://your-site.vercel.app/demo.html?api=https://jarvis-demo.fly.dev
```

That `?api=` is the whole configuration. **This is the link you send people.**

Simpler alternative: the backend can serve the page itself, so
`https://jarvis-demo.fly.dev/` alone works and there is nothing to deploy to
Vercel at all. Use that if one URL is easier to hand out.

## 3. Lock the origin

Once the front end has a fixed address:

```powershell
fly secrets set DEMO_ALLOWED_ORIGINS=https://your-site.vercel.app
```

Otherwise any website can embed your backend and spend your quota.

---

## Money

**Every visitor spends your API key.** Gemini Live bills per minute of audio, so
a public link is an open tap unless it is bounded. The defaults:

| Setting | Default | What it prevents |
|---|---|---|
| `DEMO_MAX_SESSION_SECONDS` | 300 | One visitor talking all afternoon |
| `DEMO_IDLE_TIMEOUT_SECONDS` | 60 | A forgotten open tab |
| `DEMO_MAX_CONCURRENT` | 3 | A crowd, or one script opening many sockets |
| `DEMO_MAX_SESSIONS_PER_IP_HOUR` | 6 | One person reconnecting in a loop |
| `DEMO_DAILY_AUDIO_MINUTES` | 120 | Everything else — the hard ceiling |

The daily budget is the backstop: whatever leaks past the others, the day's
spend stops there and visitors are told to come back tomorrow. Watch
`/healthz` for a few days before raising anything.

**Also set a billing cap in Google AI Studio.** These limits live in one process
and reset if it restarts; the provider-side cap does not.

---

## Safety

The demo evaluates Python that strangers supply, through `calculate`. That is
not `run_python` from the desktop build:

- An AST pass runs **before** execution and refuses anything outside a small
  allowlist — no `os`, `subprocess`, `socket`, `open`, `eval`, `__import__`, and
  no `__dunder__` attribute access, which is the usual way out of a restricted
  namespace.
- Imports are limited to `math`, `statistics`, `json`, `re`, `datetime` and a
  few others, enforced twice: once in the AST pass, once at import time.
- `read_page` refuses localhost, private ranges and cloud metadata addresses, so
  it cannot be turned into a probe of the host's internal network.
- The container runs as a non-root user and holds no credential but the API key.

Treat that as the first of two walls. The second is that there is nothing of
value inside the container — which is why `.dockerignore` keeps `.env`,
`config/` and `mcp_servers.json` out of the image entirely.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "This demo is not configured yet." | `GEMINI_API_KEY` secret missing — check `/healthz` |
| "Could not reach the demo server." | Wrong `?api=` value, or the backend is down |
| Connects, no sound | The browser blocked autoplay. Audio starts on the **Start** click — do not auto-connect on page load. |
| Mic blocked | Browsers require HTTPS for microphone access. Use the deployed URL, not an IP. |
| "All 3 demo lines are busy" | Working as intended. Raise `DEMO_MAX_CONCURRENT` if your budget allows. |
| Everyone gets rejected | Daily budget spent. `/healthz` shows `budget_minutes_left`. |
| First visit after quiet is slow | Render free tier sleeping. Fly with `min_machines_running = 1` avoids it. |
| "That session is no longer open." | The socket dropped before the upload finished. Press Start again. |
| Upload rejected on type | Only PDF, Word, Excel, CSV, JSON and text are parsed. |
| It answers from memory, not the document | It should call `search_document`. Check the upload returned `ok: true`. |
