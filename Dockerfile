FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY smol_llm_proxy/ smol_llm_proxy/

RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

RUN mkdir -p /app/data && chmod 777 /app/data \
    && chmod +x entrypoint.sh

EXPOSE 8000

ENV PROXY_HOST=0.0.0.0
ENV PROXY_PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${PROXY_PORT:-8000}/health', timeout=4).getcode()==200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "smol_llm_proxy.main:app", "--host", "0.0.0.0", "--port", "8000"]
