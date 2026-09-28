# Shared image for both control towers. The app is stdlib-only ThreadingHTTPServer,
# so there is no WSGI layer: the container runs server.py directly.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first so the layer survives code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ is a mounted volume at runtime; this keeps the path valid if nothing is mounted.
RUN mkdir -p /app/data && \
    adduser --system --group --no-create-home rhg && \
    chown -R rhg:rhg /app
USER rhg

# --no-browser is mandatory: main() calls webbrowser.open() otherwise.
CMD ["python", "server.py", "--no-browser"]
