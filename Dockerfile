FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PULSE_PLAYWRIGHT_HEADLESS=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv pip sync uv.lock \
    && playwright install --with-deps chromium

COPY app ./app

CMD ["python", "-m", "app.pulse_client"]
