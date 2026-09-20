# The hosted JARVIS demo. Not the desktop assistant — see server/app.py.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so edits to the source do not invalidate the layer.
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# Only what the demo actually needs. The desktop app, its actions, the PyQt6
# UI and every credential file stay out of the image — an image that cannot
# leak a secret is better than one that is merely configured not to.
COPY server/            /app/server/
COPY core/env_config.py /app/core/env_config.py
COPY web/demo.html      /app/web/demo.html
RUN touch /app/core/__init__.py

# Runs unprivileged: the demo evaluates visitor-supplied Python, and although
# server/safe_tools.py restricts it heavily, root would make any gap worse than
# it needs to be.
RUN useradd --create-home --shell /usr/sbin/nologin jarvis && chown -R jarvis /app
USER jarvis

EXPOSE 8080
ENV PORT=8080
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
