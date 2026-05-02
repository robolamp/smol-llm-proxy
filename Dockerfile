FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .

RUN pip install --no-cache-dir --prefix=/install ".[dev]" 2>/dev/null || pip install --no-cache-dir --prefix=/install fastapi uvicorn httpx pydantic pyyaml bcrypt

FROM python:3.12-slim

WORKDIR /app

RUN groupadd -r appuser && useradd -r -g appuser appuser

COPY --from=builder /install /usr/local

COPY . .

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENV PROXY_HOST=0.0.0.0
ENV PROXY_PORT=8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
