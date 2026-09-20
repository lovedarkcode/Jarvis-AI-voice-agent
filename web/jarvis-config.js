/* Resolves which backend this dashboard talks to.

   In the bundled dashboard the page and the API share an origin, so the code
   could say location.host. Served from Vercel they do not, so every request has
   to be addressed explicitly. The address is per-browser rather than baked in at
   build time: one deployment then works for any machine, and the URL of a
   private tunnel never has to be committed to a public repository. */
(function () {
  var LS_KEY = 'jarvis_backend_url';

  function normalize(url) {
    url = String(url || '').trim().replace(/\/+$/, '');
    if (!url) return '';
    if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
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
      if (/^https?:\/\//i.test(path)) return path;
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
