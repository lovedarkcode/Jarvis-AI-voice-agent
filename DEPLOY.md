# Deploying the JARVIS dashboard

The dashboard front end runs on Vercel. JARVIS itself keeps running on your PC.

That split is not a limitation to work around — it is the only arrangement that
works. JARVIS needs a microphone to hear you, a screen to look at, and your
actual computer to control. A copy running in a datacenter has none of those.
So the browser UI goes to Vercel, and everything that makes it JARVIS stays
where your hardware is.

```
  phone / laptop / anywhere
            │  https
   ┌────────▼─────────┐
   │      VERCEL      │   index.html, login.html  (static, no secrets)
   └────────┬─────────┘
            │  https + wss   (Cloudflare Tunnel)
   ┌────────▼─────────┐
   │      YOUR PC     │   FastAPI · the live JARVIS process
   │                  │   .env · GEMINI_API_KEY · primitives · MCP · mic
   └──────────────────┘
```

Your credentials never leave your machine. The deployed bundle contains no keys
— it is three static files and a copy of CryptoJS.

---

## 1. Expose your PC with a tunnel

Vercel cannot reach `192.168.x.x`. A tunnel gives your PC a public hostname
without opening a port on your router.

Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/):

```powershell
winget install --id Cloudflare.cloudflared
```

**Quick version** (no account, throwaway hostname, changes on every restart):

```powershell
cloudflared tunnel --url http://localhost:8000
```

It prints something like `https://tidy-otter-grow.trycloudflare.com`. That is
your backend address.

**Stable version** (recommended — a hostname that survives restarts):

```powershell
cloudflared tunnel login
cloudflared tunnel create jarvis
cloudflared tunnel route dns jarvis jarvis.yourdomain.com
cloudflared tunnel run --url http://localhost:8000 jarvis
```

The dashboard listens on port **8000** (`dashboard/server.py`, `PORT`).

---

## 2. Deploy the front end

```powershell
python tools/build_web.py     # regenerates web/ from dashboard/static/
npx vercel --prod
```

`vercel.json` already sets `web` as the output directory, so there is nothing to
configure in the Vercel dashboard. Vercel gives you a URL such as
`https://jarvis-dashboard.vercel.app`.

Re-run `tools/build_web.py` whenever you edit `dashboard/static/` — it rewrites
the parts of the page that assume the API shares an origin with it, and it fails
loudly rather than silently emitting a broken bundle if the markup has moved.

---

## 3. Tell the backend where the front end lives

In `config/api_keys.json`:

```json
{
  "dashboard_frontend_url": "https://jarvis-dashboard.vercel.app",
  "dashboard_allowed_origins": []
}
```

Two things depend on this:

- **CORS.** A browser refuses to call your backend from another origin unless
  the backend says that origin is allowed. Only the origins listed here are
  accepted — deliberately not `*`, because these endpoints queue commands into
  an assistant that can run arbitrary code.
- **The QR code.** With this set, the QR flow bounces the browser to your Vercel
  URL carrying its credentials in the URL *fragment*. Fragments are never sent
  to a server, so the token does not appear in Vercel's logs or any proxy in
  between.

Leave `dashboard_frontend_url` empty to go back to the original behaviour, where
your PC serves the UI itself over the LAN.

Restart JARVIS. The console should print:

```
[Dashboard] CORS enabled for: https://jarvis-dashboard.vercel.app
```

---

## 4. Pair a device

Open your Vercel URL. On a new browser it asks once for your backend address —
paste the tunnel hostname. It is checked before being saved, and it is kept in
that browser's `localStorage` only, so the same deployment serves any number of
people and your private hostname is never committed to the repo.

Then press **Remote Control** in the JARVIS desktop app for a session key, and
enter it. Or scan the QR code, which does both steps at once.

To re-pair against a different backend, double-click the address shown at the
top of the dashboard.

---

## Security

Worth being clear about, because this project's `run_python` and `shell`
primitives run with **no sandbox** by design:

- **The tunnel hostname is a credential.** Anyone who has it can reach your
  login endpoint. Session keys are one-time and expire in 10 minutes, and
  commands are AES-256-CBC encrypted with a key derived from the session key,
  but the hostname is still the outer wall. Do not publish it.
- **Use the named-tunnel form for anything long-lived.** A quick tunnel's
  hostname is random but public, and it changes on every restart, which tempts
  people into pasting it somewhere convenient.
- **Cloudflare Access** in front of the tunnel adds real authentication (Google
  login, a one-time PIN) ahead of JARVIS's own. On a free plan this is the single
  biggest improvement you can make to this setup.
- **Revoke paired devices** with `POST /api/revoke-devices` if you lose a phone.

The front end is static and holds no secrets, so the Vercel deployment itself is
not sensitive. Everything worth protecting is behind the tunnel.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Could not reach that address" when pairing | Tunnel is down, or JARVIS is not running. Check `cloudflared` is up and the console shows the dashboard URL. |
| Dashboard loads, then "Connection lost" | The WebSocket was blocked. Confirm the tunnel forwards `wss://`, and that your origin is in `dashboard_allowed_origins`. |
| Browser console: "blocked by CORS policy" | `dashboard_frontend_url` does not exactly match your Vercel origin — no trailing slash, and `https://` included. |
| Console never prints "CORS enabled for" | No frontend URL configured; the backend is still in single-origin mode. |
| Page shows `__IP__:__PORT__` | The bundle was copied by hand instead of built. Run `python tools/build_web.py`. |
