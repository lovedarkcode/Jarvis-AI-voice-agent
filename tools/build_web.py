"""
Generate web/ — the Vercel-deployable dashboard — from dashboard/static/.

Why a generator rather than a second copy of the HTML: the dashboard is still
served locally over LAN exactly as before, and a hand-forked copy would drift
from it the first time either side is edited. This script rewrites only the URL
construction — every place the page assumes the backend is the same origin it
was served from — and leaves the markup, styling and logic untouched. Re-run it
after changing dashboard/static/.

The split it creates:

    Vercel (static)          your PC (behind a tunnel)
    ─────────────────        ────────────────────────
    index.html               FastAPI  /login  /api/*  /ws
    login.html               the live JARVIS process
    crypto-js.min.js         .env, credentials, primitives, MCP

Nothing secret is emitted here. The generated files are pure static assets; the
backend address is supplied by the user at pairing time and kept in their
browser's localStorage, so the same deployment serves anyone without rebuilding.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "dashboard" / "static"
OUT = BASE / "web"

CONFIG_JS = """\
/* Resolves which backend this dashboard talks to.

   In the bundled dashboard the page and the API share an origin, so the code
   could say location.host. Served from Vercel they do not, so every request has
   to be addressed explicitly. The address is per-browser rather than baked in at
   build time: one deployment then works for any machine, and the URL of a
   private tunnel never has to be committed to a public repository. */
(function () {
  var LS_KEY = 'jarvis_backend_url';

  function normalize(url) {
    url = String(url || '').trim().replace(/\\/+$/, '');
    if (!url) return '';
    if (!/^https?:\\/\\//i.test(url)) url = 'https://' + url;
    return url;
  }

  var JB = {
    backend: function () {
      return localStorage.getItem(LS_KEY) || (window.JARVIS_DEFAULT_BACKEND || '');
    },
    setBackend: function (url) {
      var n = normalize(url);
      if (n) localStorage.setItem(LS_KEY, n);
      return n;
    },
    clearBackend: function () { localStorage.removeItem(LS_KEY); },
    configured: function () { return !!JB.backend(); },

    /* Absolute URLs pass through untouched so an already-resolved address is
       never prefixed twice. */
    api: function (path) {
      if (/^https?:\\/\\//i.test(path)) return path;
      return JB.backend() + path;
    },

    /* ws:// for a plain-http backend, wss:// for https. A tunnel is https, so
       this is wss in practice; the http case keeps LAN testing working. */
    wsBase: function () {
      return JB.backend().replace(/^http/i, 'ws');
    }
  };

  window.JB = JB;
})();
"""

# (pattern, replacement, how many hits are expected)
APP_RULES: list[tuple[str, str, int]] = [
    ('<script src="/static/crypto.js"></script>',
     '<script src="./jarvis-config.js"></script>\n  <script src="./crypto-js.min.js"></script>', 1),
    ("location.replace('/login');",
     "location.replace('./login.html');", 1),
    ("      return fetch(url, opts);",
     "      return fetch(JB.api(url), opts);", 1),
    ("`${_wsProto}://${location.host}/ws?token=${encodeURIComponent(_authToken)}`",
     "`${JB.wsBase()}/ws?token=${encodeURIComponent(_authToken)}`", 1),
    ("xhr.open('POST', '/api/upload');",
     "xhr.open('POST', JB.api('/api/upload'));", 1),
    ("      const origin = location.origin;",
     "      const origin = JB.backend();", 1),
    ("`${wsProto}://${location.host}/ws/phone-audio?token=${encodeURIComponent(_authToken)}`",
     "`${JB.wsBase()}/ws/phone-audio?token=${encodeURIComponent(_authToken)}`", 1),
    # __IP__/__PORT__ are substituted by the backend as it serves the page. A
    # static host does no substitution, so the placeholder would be shown to the
    # user verbatim. Fill it from the configured backend instead.
    ('<div class="url">__IP__:__PORT__</div>',
     '<div class="url" id="jb-host">—</div>', 1),
]

LOGIN_RULES: list[tuple[str, str, int]] = [
    ("fetch('/api/device-login', {",
     "fetch(JB.api('/api/device-login'), {", 1),
    ("location.replace('/');",
     "location.replace('./index.html');", 1),
    ("fetch('/login', {",
     "fetch(JB.api('/login'), {", 1),
    ("location.href = '/';",
     "location.href = './index.html';", 1),
]

# Injected into login.html: the backend prompt, and the QR hand-off. The backend
# redirects here with credentials in the fragment when a frontend URL is set.
LOGIN_BOOTSTRAP = """
<script src="./jarvis-config.js"></script>
<script>
(function () {
  /* QR hand-off. The backend puts token/key/device in the fragment because a
     fragment is never transmitted to a server — not to Vercel, not to any proxy
     between here and the tunnel. Consume it and strip it from the address bar
     so the credentials do not sit in history or get copied out of the URL. */
  if (location.hash && location.hash.length > 1) {
    var f = new URLSearchParams(location.hash.slice(1));
    var tok = f.get('token'), key = f.get('key'), dev = f.get('device');
    var be  = f.get('backend');
    if (be)  JB.setBackend(be);
    if (tok) sessionStorage.setItem('jarvis_token', tok);
    if (key) sessionStorage.setItem('jarvis_key', key);
    if (dev) localStorage.setItem('jarvis_device_token', dev);
    history.replaceState(null, '', location.pathname);
    if (tok && JB.configured()) { location.replace('./index.html'); return; }
  }

  /* First run on a new browser: we do not know where this person's JARVIS is.
     Ask once, keep it, and let them change it later. */
  if (!JB.configured()) {
    document.addEventListener('DOMContentLoaded', function () {
      var wrap = document.createElement('div');
      wrap.style.cssText = 'position:fixed;inset:0;background:#07090f;color:#dde3ed;'
        + 'font-family:system-ui,sans-serif;display:flex;align-items:center;'
        + 'justify-content:center;z-index:9999;padding:24px';
      wrap.innerHTML =
        '<div style="max-width:420px;width:100%">'
        + '<h2 style="margin:0 0 6px;font-size:19px">Connect to your JARVIS</h2>'
        + '<p style="color:#5e6a7e;font-size:13px;line-height:1.7;margin:0 0 16px">'
        + 'Paste the address your JARVIS is reachable at — the tunnel hostname '
        + 'printed in its console. Stored in this browser only.</p>'
        + '<input id="be" placeholder="jarvis.example.com" autocapitalize="off" '
        + 'autocorrect="off" spellcheck="false" style="width:100%;box-sizing:border-box;'
        + 'padding:13px 14px;border-radius:11px;border:1px solid #232a38;'
        + 'background:#0c1018;color:#dde3ed;font-size:15px;outline:none">'
        + '<button id="bego" style="width:100%;margin-top:10px;padding:13px;'
        + 'border-radius:11px;border:0;background:#6366f1;color:#fff;font-size:15px;'
        + 'font-weight:600;cursor:pointer">Continue</button>'
        + '<p id="beerr" style="color:#f87171;font-size:12px;min-height:16px;margin:10px 0 0"></p>'
        + '</div>';
      document.body.appendChild(wrap);

      var input = wrap.querySelector('#be');
      var err   = wrap.querySelector('#beerr');
      input.focus();

      function go() {
        var v = JB.setBackend(input.value);
        if (!v) { err.textContent = 'Enter an address.'; return; }
        err.style.color = '#5e6a7e';
        err.textContent = 'Checking…';
        /* Verify before committing: a typo here would otherwise surface later as
           an unexplained login failure. */
        fetch(v + '/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                              body: JSON.stringify({ pin: '' }) })
          .then(function (r) {
            if (r.status === 401 || r.ok) { wrap.remove(); }
            else { throw new Error('unexpected ' + r.status); }
          })
          .catch(function () {
            JB.clearBackend();
            err.style.color = '#f87171';
            err.textContent = 'Could not reach that address. Is JARVIS running and the tunnel up?';
          });
      }
      wrap.querySelector('#bego').onclick = go;
      input.addEventListener('keydown', function (e) { if (e.key === 'Enter') go(); });
    });
  }
})();
</script>
"""


def apply(text: str, rules: list[tuple[str, str, int]], label: str) -> str:
    """Apply each rewrite, failing loudly if a pattern is missing.

    A silent miss is the dangerous outcome: the page would deploy and then fail
    at runtime against the wrong origin, which looks like a tunnel problem rather
    than a build problem. Better to stop here.
    """
    for pattern, replacement, expected in rules:
        found = text.count(pattern)
        if found != expected:
            raise SystemExit(
                f"[build_web] {label}: expected {expected} occurrence(s) of:\n"
                f"    {pattern[:100]}\n"
                f"  but found {found}. dashboard/static/ has changed — update "
                f"the rules in tools/build_web.py."
            )
        text = text.replace(pattern, replacement)
    return text


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"[build_web] missing {SRC}")

    OUT.mkdir(exist_ok=True)

    app = apply((SRC / "app.html").read_text(encoding="utf-8"), APP_RULES, "app.html")
    # Without a backend there is nothing to show; send them to pair first.
    app = app.replace(
        "    const _authToken  = sessionStorage.getItem('jarvis_token');",
        "    if (!JB.configured()) location.replace('./login.html');\n"
        "    const _authToken  = sessionStorage.getItem('jarvis_token');",
        1,
    )
    app = app.replace(
        "</body>",
        r"""  <script>
    (function () {
      var el = document.getElementById('jb-host');
      if (!el) return;
      try { el.textContent = JB.backend().replace(/^https?:\/\//i, ''); } catch (e) {}
      /* Long-press / double-click the address to re-pair against a different
         backend — otherwise a wrong or changed tunnel hostname strands the user
         with no way back short of clearing site data. */
      el.style.cursor = 'pointer';
      el.title = 'Double-click to change backend';
      el.addEventListener('dblclick', function () {
        if (confirm('Forget this JARVIS address and pair again?')) {
          JB.clearBackend();
          sessionStorage.clear();
          location.replace('./login.html');
        }
      });
    })();
  </script>
</body>""",
        1,
    )
    (OUT / "index.html").write_text(app, encoding="utf-8")

    login = apply((SRC / "login.html").read_text(encoding="utf-8"), LOGIN_RULES, "login.html")
    if "</head>" in login:
        login = login.replace("</head>", LOGIN_BOOTSTRAP + "</head>", 1)
    else:
        login = LOGIN_BOOTSTRAP + login
    (OUT / "login.html").write_text(login, encoding="utf-8")

    (OUT / "jarvis-config.js").write_text(CONFIG_JS, encoding="utf-8")

    crypto = SRC / "crypto-js.min.js"
    if crypto.exists() and crypto.stat().st_size > 1000:
        shutil.copy2(crypto, OUT / "crypto-js.min.js")
    else:
        raise SystemExit(
            "[build_web] dashboard/static/crypto-js.min.js is missing or empty.\n"
            "  Start JARVIS once so it downloads, or fetch it manually from\n"
            "  https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js"
        )

    for f in sorted(OUT.iterdir()):
        if f.is_file():
            print(f"  {f.name:24} {f.stat().st_size:>8,} bytes")
    print(f"[build_web] wrote {OUT}")


if __name__ == "__main__":
    main()
