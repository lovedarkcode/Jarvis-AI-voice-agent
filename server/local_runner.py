"""Local launcher for the exact ASGI app deployed to Vercel."""
import argparse
import asyncio
import webbrowser


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the production Jarvis web app locally.')
    parser.add_argument('--host', default='127.0.0.1', help='Bind address (default: 127.0.0.1).')
    parser.add_argument('--port', default=8765, type=int, help='HTTP port (default: 8765).')
    parser.add_argument('--no-browser', action='store_true', help='Start the server without opening a browser.')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')

    try:
        import uvicorn
    except ImportError:
        print('Install web dependencies: python -m pip install -r server/requirements.txt')
        return 1

    browser_host = '127.0.0.1' if args.host == '0.0.0.0' else 'localhost' if args.host == '::' else args.host
    if ':' in browser_host:
        browser_host = f'[{browser_host}]'
    url = f'http://{browser_host}:{args.port}/'
    print(f'Jarvis: {url}\nEnter your API keys in the browser on every start. Press Ctrl+C to stop.')

    class LocalServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and not args.no_browser:
                try:
                    await asyncio.to_thread(webbrowser.open, url)
                except OSError:
                    print(f'Open {url} in your browser.')

    LocalServer(uvicorn.Config('api.index:app', host=args.host, port=args.port)).run()
    return 0
