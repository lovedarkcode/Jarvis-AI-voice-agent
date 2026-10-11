"""Visual smoke check with fake API responses, never real keys or mail."""
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'screenshots' / 'chaiui'
OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
    errors, missing = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def route(request):
        path = urlsplit(request.request.url).path
        if path == '/api/keys/verify':
            request.fulfill(json={'providers': ['openai'], 'models': {'openai': 'demo'}, 'local_mcp': True})
        elif path == '/api/chat':
            request.fulfill(json={'text': 'Your workspace is ready. What would you like to explore?', 'provider': 'openai'})
        else:
            target = (ROOT / 'web' / (path.lstrip('/') or 'demo.html')).resolve()
            if not target.is_relative_to(ROOT / 'web') or not target.is_file():
                missing.append(path)
                request.fulfill(status=404)
            else:
                request.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(str(target))[0] or 'application/octet-stream')

    page.route('http://jarvis.test/**', route)
    page.goto('http://jarvis.test/')
    page.evaluate('document.fonts.ready')
    page.screenshot(path=str(OUT / 'setup-dark.png'), full_page=True)
    page.set_viewport_size({'width': 375, 'height': 812})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('.theme.corner').click()
    page.screenshot(path=str(OUT / 'setup-mobile-light.png'), full_page=True)
    page.locator('.theme.corner').click()
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('#openai_key').fill('sk-' + 'demo' * 12)
    page.locator('#save-keys').click()
    page.locator('#app').wait_for(state='visible')
    page.screenshot(path=str(OUT / 'workspace-dark.png'))
    page.locator('#message').fill('Hello Jarvis')
    page.locator('#send').click()
    page.locator('.message.assistant').wait_for()
    page.locator('#header-settings').click()
    assert page.locator('#settings-dialog').evaluate('(el)=>el.open')
    page.locator('#settings-dialog .theme').click()
    page.locator('#settings-dialog [data-close]').click()
    page.screenshot(path=str(OUT / 'workspace-light.png'))
    page.locator('#header-settings').click()
    page.locator('#settings-audio').click()
    page.screenshot(path=str(OUT / 'audio-light.png'))
    page.locator('#audio-dialog [data-close]').click()
    page.set_viewport_size({'width': 375, 'height': 812})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate('[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth && getComputedStyle(e).display!=="none").map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right]).slice(0,12)')
    page.screenshot(path=str(OUT / 'mobile-light.png'))
    page.locator('#menu-toggle').click()
    page.locator('.sidebar .theme').click()
    page.locator('#sidebar-backdrop').click(position={'x': 350, 'y': 200})
    page.screenshot(path=str(OUT / 'mobile-dark.png'))
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate('[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth && getComputedStyle(e).display!=="none").map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right]).slice(0,12)')
    assert not errors, errors
    assert not missing, missing
    print(json.dumps({'page_errors': errors, 'missing_assets': missing, 'screenshots': str(OUT)}))
    browser.close()
