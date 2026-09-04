FROM python:3.11-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY gateway ./gateway
COPY config.yaml ./config.yaml
COPY scripts ./scripts

ENV PATH="/app/.venv/bin:$PATH"

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
