"""
MARK LIII — one-time setup.

Installs the Python dependencies for THIS operating system only: the OS-specific
packages in requirements.txt carry `sys_platform` markers, so a macOS or Linux
user never pulls Windows-only libraries (and vice-versa). Then it fetches the
Playwright browsers needed for web automation (current-OS builds only).

The optional local wake word ("Hey Jarvis") is NOT installed here — it's a
one-click, opt-in download from ⚙ → WAKE WORD inside the app.
"""
import platform
import subprocess
import sys
from pathlib import Path

OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True)


def main() -> None:
    print(f"⚙  MARK LIII setup — detected OS: {OS or 'unknown'}")

    # requirements.txt filters OS-specific extras by itself via pip markers.
    _run("Installing Python dependencies (OS-specific extras auto-filtered)…",
         [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

    # Chromium covers Chrome/Edge/Opera/Brave/Vivaldi; Firefox for Firefox.
    # (Safari automation additionally needs: python -m playwright install webkit)
    _run("Installing Playwright browsers (chromium + firefox)…",
         [sys.executable, "-m", "playwright", "install", "chromium", "firefox"])

    # ── OS-specific post-install notes ────────────────────────────────────────
    if OS == "Windows":
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            postinstall = Path(sys.executable).parent / "Scripts" / "pywin32_postinstall.py"
            print(
                "\n⚠️  pywin32 did not register correctly — desktop-shortcut "
                "creation will use a slower fallback. To fix it, run:\n"
                f'    "{sys.executable}" -m pip install --force-reinstall pywin32\n'
                f'    "{sys.executable}" "{postinstall}" -install'
            )
    elif OS == "Linux":
        print(
            "\nℹ️  Linux note — a few voice-controlled OS actions shell out to "
            "native tools. Install the ones you'll use via your package manager:\n"
            "    • volume      → pulseaudio-utils   (pactl)\n"
            "    • brightness  → brightnessctl\n"
            "    • reminders   → systemd (systemd-run) or 'at'\n"
            "    • open URLs   → xdg-utils          (xdg-open)"
        )
    elif OS == "Darwin":
        print(
            "\nℹ️  macOS note — volume, brightness and reminders use the built-in "
            "'osascript' / LaunchAgents, so no extra tools are required.\n"
            "    For Safari automation only: python -m playwright install webkit"
        )

    # ── The one manual step: the API key ─────────────────────────────────────
    # The .env is created here rather than only described, because a file that
    # already exists, in the right folder, with the variable name spelled
    # correctly, is much harder to get wrong than an instruction to write one.
    env     = Path(__file__).resolve().parent / ".env"
    example = env.parent / ".env.example"
    if not env.exists():
        try:
            env.write_text(
                example.read_text(encoding="utf-8") if example.exists()
                else "GEMINI_API_KEY=your_key_here\n",
                encoding="utf-8",
            )
            print(f"\n📝 Created {env}")
        except Exception as e:
            print(f"\n⚠️  Could not create .env ({e}) — please create it by hand.")

    print("\n✅ Setup complete!")
    print("   1) Put your free Gemini API key in .env:")
    print("         GEMINI_API_KEY=your_key_here")
    print("      Get one at https://aistudio.google.com/apikey")
    print("      It lives ONLY in .env — the app reads it and never writes it.")
    print("   2) Launch it:  python main.py")
    print("   3) (Optional) Enable 'Hey Jarvis' from ⚙ → WAKE WORD.")


if __name__ == "__main__":
    main()
