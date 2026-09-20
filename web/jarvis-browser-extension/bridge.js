// Runs only in the hosted JARVIS tab.  It is the narrow, visible hand-off
// between the page and the extension; untrusted websites never receive it.
(() => {
  const CHANNEL = "jarvis-browser-link";
  const announce = () => window.postMessage({channel: CHANNEL, type: "ready"}, location.origin);
  announce();

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.origin !== location.origin) return;
    const data = event.data || {};
    if (data.channel !== CHANNEL) return;
    if (data.type === "status_probe") return announce();
    if (data.type !== "execute") return;

    chrome.runtime.sendMessage({type: "execute", action: data.action, args: data.args || {}}, (response) => {
      const error = chrome.runtime.lastError?.message;
      window.postMessage({
        channel: CHANNEL,
        type: "result",
        request_id: data.request_id,
        ok: !error && Boolean(response?.ok),
        result: response?.result,
        error: error || response?.error || "Browser action failed."
      }, location.origin);
    });
  });
})();
