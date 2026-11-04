# The report sender: a headless Chromium that opens screens of pulse as the people who receive them and emails
# a picture and a PDF of each. It holds no data and no accounts; identity tells it what to send.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt .
# Chromium and the system libraries and fonts it needs (a layer that survives code edits)
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

COPY . .

RUN adduser --system --group --no-create-home rhg && chown -R rhg:rhg /app /ms-playwright
USER rhg

CMD ["python", "reporter.py"]
