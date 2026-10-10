
# Jarvis

Run the same web app locally and on Vercel:

```sh
python -m pip install -r server/requirements.txt
python main.py
```

The browser opens at `http://127.0.0.1:8765/`. Enter an API key on every start or
page reload. Select **Sarvam** to use its key for chat, voice input and spoken
replies, or **Gemini** to use Gemini chat and voice. Allow microphone access to
test voice. The web app holds keys only in page memory and does not load `.env`.

Use `python main.py --no-browser` to open the URL yourself. The separate native
assistant is available with `python main.py --desktop` and uses `.env`.

See [WEB-PROVIDERS.md](WEB-PROVIDERS.md) for configuration, validation and deployment.
