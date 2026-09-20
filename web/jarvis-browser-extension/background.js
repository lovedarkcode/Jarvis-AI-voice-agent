// The extension retains the tab it opened for a JARVIS session.  It never
// injects into a page until the person approved a matching request in JARVIS.
const controlledTabs = new Map();

const asUrl = (value) => {
  const url = new URL(String(value || ""));
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("Only http(s) addresses can be opened.");
  return url.toString();
};

const waitForLoad = (tabId) => new Promise((resolve) => {
  const timeout = setTimeout(() => { chrome.tabs.onUpdated.removeListener(listener); resolve(); }, 10000);
  const listener = (id, change) => {
    if (id === tabId && change.status === "complete") {
      clearTimeout(timeout); chrome.tabs.onUpdated.removeListener(listener); resolve();
    }
  };
  chrome.tabs.onUpdated.addListener(listener);
});

async function executeIn(tabId, func, args = []) {
  const [{result}] = await chrome.scripting.executeScript({target: {tabId}, func, args});
  return result;
}

async function browserAction(senderTabId, action, args) {
  let targetId = controlledTabs.get(senderTabId);
  if (action === "open") {
    const tab = await chrome.tabs.create({url: asUrl(args.url), active: true});
    controlledTabs.set(senderTabId, tab.id);
    return `Opened ${tab.url || args.url}.`;
  }
  if (action === "search" || action === "youtube_play") {
    const query = String(args.query || "").trim();
    if (!query) throw new Error("A search query is required.");
    const isYouTube = action === "youtube_play" || String(args.engine).toLowerCase() === "youtube";
    const url = isYouTube
      ? `https://www.youtube.com/results?search_query=${encodeURIComponent(query)}`
      : `https://www.google.com/search?q=${encodeURIComponent(query)}`;
    const tab = await chrome.tabs.create({url, active: true});
    targetId = tab.id; controlledTabs.set(senderTabId, targetId);
    if (action !== "youtube_play") return `Opened ${isYouTube ? "YouTube" : "Google"} results for “${query}”.`;
    await waitForLoad(targetId);
    const title = await executeIn(targetId, async () => {
      const until = Date.now() + 9000;
      while (Date.now() < until) {
        const link = document.querySelector("ytd-video-renderer a#video-title");
        if (link) { const title = link.textContent.trim(); link.click(); return title || "the first result"; }
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      return "YouTube search results";
    });
    return `Opened YouTube and selected ${title}.`;
  }
  if (!targetId) throw new Error("No JARVIS-controlled browser tab exists yet. Open or search first.");
  if (action === "snapshot") return await executeIn(targetId, () => ({
    url: location.href, title: document.title, text: document.body.innerText.slice(0, 6000)
  }));
  if (action === "click") {
    const selector = String(args.selector || "");
    const result = await executeIn(targetId, (s) => {
      const node = document.querySelector(s);
      if (!node) return {ok: false, error: `No match for ${s}`};
      node.click(); return {ok: true, text: node.textContent.trim().slice(0, 160)};
    }, [selector]);
    if (!result.ok) throw new Error(result.error);
    return `Clicked ${result.text || args.selector}.`;
  }
  if (action === "type") {
    const selector = String(args.selector || ""), text = String(args.text || "");
    const result = await executeIn(targetId, (s, value) => {
      const node = document.querySelector(s);
      if (!(node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement || node?.isContentEditable)) return {ok: false, error: "That selector is not an editable field."};
      if (node.isContentEditable) node.textContent = value; else node.value = value;
      node.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
      node.dispatchEvent(new Event("change", {bubbles: true}));
      return {ok: true};
    }, [selector, text]);
    if (!result.ok) throw new Error(result.error);
    return "Filled the requested field.";
  }
  throw new Error("That browser action is not supported.");
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "execute" || !sender.tab?.id) return;
  browserAction(sender.tab.id, message.action, message.args || {})
    .then(result => sendResponse({ok: true, result: typeof result === "string" ? result : JSON.stringify(result)}))
    .catch(error => sendResponse({ok: false, error: error.message || "Browser action failed."}));
  return true;
});
