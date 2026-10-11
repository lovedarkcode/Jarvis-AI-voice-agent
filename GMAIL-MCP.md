# Gmail by voice (local only)

Gmail uses [Gmail AutoAuth MCP](https://github.com/GongRzhe/Gmail-MCP-Server).
Install Node.js/npm (including `npx`) and the Python web dependencies:

```powershell
python -m pip install -r server/requirements.txt
```

## One-time Google setup

1. Create a Google Cloud project and enable **Gmail API**.
2. Configure the OAuth consent screen / Google Auth Platform audience as
   **External** and add your own Google account as a **test user**.
3. Create an OAuth client ID of type **Desktop app** and download its JSON.
4. Save it as `~/.gmail-mcp/gcp-oauth.keys.json` (`~` is your home directory).
5. Authorize once, choosing the account Jarvis should read:

```powershell
New-Item -ItemType Directory -Force "$HOME/.gmail-mcp" | Out-Null
# If the downloaded file is currently in the repository:
Copy-Item -LiteralPath ./gcp-oauth.keys.json -Destination "$HOME/.gmail-mcp/gcp-oauth.keys.json"
npx @gongrzhe/server-gmail-autoauth-mcp auth
```

The token is saved at `~/.gmail-mcp/credentials.json`. Keep both files private.
For an existing **Web application** OAuth client, first register
`http://localhost:3000/oauth2callback` as an Authorized redirect URI in Google Cloud.
Port 3000 must be free during authorization.

If Google shows **403 access_denied** and says the app is being tested, open
**Google Auth Platform → Audience → Test users → Add users**, add the exact Gmail
account used for consent, save, and retry consent with that account. See
[Google's audience guide](https://support.google.com/cloud/answer/15549945?hl=en).

While an External consent screen is in **Testing**, Google expires Gmail refresh
tokens after **7 days**. Re-run `auth` weekly, or move the app to Production
(Google may require verification depending on the app and scopes).
See [Google's refresh-token rules](https://developers.google.com/identity/protocols/oauth2#expiration).

## Connect Jarvis

If `mcp_servers.json` does not exist, copy `mcp_servers.example.json`. Otherwise,
merge its `gmail` entry into your existing `mcpServers` object; keep other servers.
Set Gmail's `enabled` to `true`. No credentials or absolute paths belong in this config.
The checked-in example stays disabled. Start Jarvis after config changes:

```powershell
python main.py
```

Enter your provider key in the browser. NVIDIA (English voice), Sarvam and Gemini support spoken
Gmail queries. Local Gemini voice uses transcription → chat → speech so it can
access Gmail. The existing native assistant (`python main.py --desktop`) also
uses the gateway. The local web bridge exposes Gmail; existing desktop MCP
servers such as Playwright and GitHub retain their existing tools.

Servers start independently in the background. Inspect readiness and the catalog:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/mcp/status | Format-List
```

Expect `gmail: ready, 2 tools` and `search_emails, read_email`. Startup can take
longer on the first `npx` download. Restart after installing Node or authenticating.
Missing, disabled, unauthenticated and starting servers do not prevent Jarvis starting.
Expired/revoked tokens can still allow tool discovery; test an actual search too.

Try:

- “Do I have unread emails?”
- “Summarize my unread emails from today.”
- “Read the latest email from sender@example.com.”

The default search is `is:unread newer_than:1d` (the last 24 hours); explicit
sender/date criteria override it. At most five emails are read. Jarvis lists
matching messages without claiming that a capped result is an exact inbox total.
Each summary is one spoken sentence giving the sender and what they want.

## Safety and deployment

The current gateway has no spoken confirmation protocol. Gmail therefore uses
an enforced search/read allowlist: send, delete, drafts, marking read/unread,
label/filter changes and attachment downloads are hidden **and rejected on dispatch**.
The requested server description mentions marking messages; the policy overrides it.
The built-in Connect Gmail flow requests only gmail.readonly. The optional upstream
CLI auth command requests broader scopes; the gateway still blocks all writes.

Email content is untrusted data. The local bridge plans a search before fetching
mail; fetched content is only passed to the final summarizer, with no executable
tools. Email content goes to the selected AI provider to produce your summary.
The desktop assistant also receives explicit injection and read-only rules.

Gmail is only wired into loopback local runs. Binding `--host 0.0.0.0` or another
network address uses the hosted app without MCP. Local requests require a localhost
Host and same-origin browser requests. Vercel imports `api.index`, never the local
bridge; `VERCEL` also prevents Gmail startup. Configs, OAuth files and the local
bridge are excluded from the deployment and credentials are gitignored.

## Regression tests

```powershell
python -m unittest discover -s tests -p "test_mcp_gateway.py"
python -m unittest discover -s tests -p "test_local_mcp.py"
python -m unittest discover -s tests -p "test_local_runner.py"
python -m unittest discover -s tests -p "test_provider_api.py"
node --test tests/web-gate.test.cjs
```

These tests use fake subprocesses/provider responses and do not read real mail.
For a manual failure check, disable Gmail in your private config and restart;
ask about unread mail and then ask a normal question. Both should respond, with
a brief unavailable message for Gmail. Re-enable it and restart to reconnect.

## Sign in inside Jarvis (recommended)

After the Google project/client setup above, you no longer need the CLI auth command
or a Jarvis restart for consent. Run `python main.py`, enter your AI provider key,
then ask "Summarize my Gmail" or choose **Settings > Connect / switch Gmail**.
Jarvis opens Google sign-in in your default browser. Choose the correct account
and approve read-only Gmail access. Return to Jarvis: it reconnects MCP and resumes
the pending summary automatically. The account address appears in Settings.
Email content goes to the selected AI provider for summarization.

Sign-in waits up to five minutes. Port 3000 must be free; close any old CLI auth
command first. If Google blocks a Testing app, add the selected account under
Google Auth Platform > Audience > Test users. The Web client redirect must be
`http://localhost:3000/oauth2callback`. These Google settings cannot be bypassed
by opening the Gmail website. Reconnect from Settings after token expiry.

This integrated sign-in flow is available in the local browser app, not Vercel
or the legacy `--desktop` interface. The latter can use the saved MCP credentials.
