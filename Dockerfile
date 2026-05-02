FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .

RUN pip install --no-cache-dir --prefix=/install ".[dev]" 2>/dev/null || pip install --no-cache-dir --prefix=/install fastapi uvicorn httpx pydantic pyyaml bcrypt

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

RUN mkdir -p /app/data && chmod 777 /app/data \
    && chmod +x entrypoint.sh

EXPOSE 8000

ENV PROXY_HOST=0.0.0.0
ENV PROXY_PORT=8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
