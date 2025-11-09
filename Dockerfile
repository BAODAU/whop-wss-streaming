#... existing setup ...

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

# 1. Add the future .venv to PATH immediately
ENV PATH="/app/.venv/bin:$PATH"

# 2. Use 'uv sync --frozen' to install directly from uv.lock
# 3. Run playwright install (now correctly located in .venv)
RUN uv sync --frozen \
    && playwright install --with-deps chromium

COPY app ./app

CMD ["python", "-m", "app.pulse_client"]