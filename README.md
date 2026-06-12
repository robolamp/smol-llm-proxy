# smol-llm-proxy

[![PyPI version](https://img.shields.io/pypi/v/smol-llm-proxy)](https://pypi.org/project/smol-llm-proxy/)
[![CI](https://github.com/robolamp/smol-llm-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/robolamp/smol-llm-proxy/actions)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A small API proxy for self-hosted llama.cpp setups. Routes across multiple llama-server instances, per-user API keys, token usage tracking. In-memory rate limiting (RPM/TPM) with async SQLite flush. <=1000 code lines (excluding blanks, docstrings, comments), ~53 MB RAM, ~2–5ms mean overhead on clean conditions.

Built for the case where you run multiple llama-server instances (different models, different GPUs) and want to share them across users with token tracking. Not a replacement for LiteLLM or llama-swap — see comparison below.

## Features

- Per-user API keys (create / delete / toggle active)
- RPM/TPM rate limiting with in-memory counters + async batch flush to SQLite
- Multi-server routing by model name with in-memory cache
- Model aliases (`alias` -> `model-name.gguf`)
- Token usage logging: prompt/completion tokens, timings
- Streaming and non-streaming proxy support
- Connection-pooled httpx client (keepalive connections to backends)
- SQLite backend (zero external DB dependencies)

## Quick Start

### Docker Compose (recommended)

Clone the repo, then:

```bash
cp .env.example .env                # set ADMIN_KEY
cp config.example.yaml config.yaml  # fill in your servers
docker compose up -d --build
```

The proxy listens on `0.0.0.0:8000` by default.

### Plain Docker

```bash
docker build -t smol-llm-proxy .
docker run -p 8000:8000 \
  -e ADMIN_KEY=secret \
  -v db-data:/data \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  smol-llm-proxy
```

### Pip install

```bash
pip install smol-llm-proxy
```

Example configs ship with the package. Copy them:

```bash
python -c "import smol_llm_proxy, shutil, os; d=os.path.dirname(smol_llm_proxy.__file__); shutil.copy2(f'{d}/config.example.yaml','config.yaml'); shutil.copy2(f'{d}/.env.example','.env')"
cp .env.example .env                # set ADMIN_KEY
cp config.example.yaml config.yaml  # fill in your servers
ADMIN_KEY=secret python -m smol_llm_proxy
```

Or download from GitHub:

```bash
curl -sO https://raw.githubusercontent.com/robolamp/smol-llm-proxy/main/.env.example && cp .env.example .env
curl -sO https://raw.githubusercontent.com/robolamp/smol-llm-proxy/main/config.example.yaml && cp config.example.yaml config.yaml
```

### Quick usage

1. Create a user key:

```bash
curl -X POST http://localhost:8000/admin/keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"name": "my-user"}'
```

The response contains a JSON object with a `key` field — that's the user's Bearer token. Save it; you'll need it for proxy requests.

2. Send a chat completion with the user key:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-<full-key-from-step-1>" \
  -d '{
    "model": "Qwen3.5-2B",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

## Configuration

The proxy reads two files: `config.yaml` for routing and `.env` for runtime settings.

### `config.yaml` — servers, models, aliases

Loaded into SQLite at startup, persisted across restarts:

```yaml
servers:
  - name: my-server
    url: http://host:port
    api_key: ""              # optional, if llama-server requires auth
    models:
      - model-name.gguf

aliases:
  alias: model-name.gguf     # short name -> real model name
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_KEY` | required | Bearer token for `/admin/*` endpoints |
| `PROXY_HOST` | `0.0.0.0` | Listen address |
| `PROXY_PORT` | `8000` | Listen port |
| `DB_PATH` | `data/proxy.db` | SQLite database location |
| `CONFIG_PATH` | `./config.yaml` | Path to config file |

For `pip install`, set them in shell or via a `.env`-like loader. For Docker Compose, put them in `.env`:

```bash
ADMIN_KEY=secret
PROXY_PORT=8000
```

### Docker volumes

The Compose setup mounts two volumes:

- `db-data:/data` — SQLite database, persists across container restarts
- `./config.yaml:/config/config.yaml:ro` — config file, read-only


## Admin API

All admin endpoints require `Authorization: Bearer <ADMIN_KEY>` header.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/servers` | `GET` | List all registered servers |
| `/admin/servers` | `POST` | Register a llama-server |
| `/admin/servers/{id}` | `DELETE` / `PATCH` | Remove or update server |
| `/admin/servers/{id}/models` | `POST` / `DELETE` | Assign/unassign model name |
| `/admin/keys` | `GET` | List all API keys |
| `/admin/keys` | `POST` | Create user key |
| `/admin/keys/{key_id}` | `DELETE` | Revoke key (by integer id) |
| `/admin/keys/{key_id}/toggle` | `PATCH` | Activate/deactivate key (by integer id) |
| `/admin/keys/{key_id}/limits` | `PUT` | Set RPM/TPM rate limits for a key |
| `/admin/aliases` | `GET` / `POST` | List or create model aliases |
| `/admin/aliases/{alias_name}` | `DELETE` | Delete alias |
| `/admin/usage` | `GET` | View token usage logs |

**Note:** Key operations (`DELETE`, `PATCH /toggle`, `PUT /limits`) use integer `key_id` from the database, not the API key string itself.

### Rate limits

Default limits: **100 RPM**, **50,000 TPM**. Set custom limits per key:

```bash
curl -X PUT http://localhost:8000/admin/keys/1/limits \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"rpm_limit": 200, "tpm_limit": 100000}'
```

Exceeding limits returns `429 Too Many Requests` with a `Retry-After` header. The rate limiter reads from SQLite on every check (accurate across multiple workers) and commits in-memory with async batch flush — one read-only SELECT per request, no commit overhead.

## Proxy Endpoints

These forward to llama-server backends based on model name routing.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | `POST` | Chat completions (streaming + non-streaming) |
| `/v1/completions` | `POST` | Legacy completions |
| `/v1/embeddings` | `POST` | Embeddings |
| `/v1/models` | `GET` | List available models (no auth required — fans out to all backends) |
| `/health` | `GET` | Health check (no auth required) |

## Usage Logs

Each request logs: user, server, model name, prompt/completion tokens, timings (ms), and total tokens. No conversation content is stored.

```bash
curl "http://localhost:8000/admin/usage?key_id=1" \
  -H "Authorization: Bearer $ADMIN_KEY"
```

## Benchmarking

Proxy overhead measured with Locust using **parallel concurrent execution** — both benchmarks (direct and proxy) hit the same backend simultaneously for a fair comparison.

All requests authenticated, routed, and logged via SQLite on every call (cold cache mode). In-memory cache disabled to measure worst-case per-request overhead.

**Hardware:** i9-14900K, RTX 4090 | **Model:** Qwen3.5-2B-GGUF | **Backend:** llama.cpp server

### Mock backend (fixed 100ms delay, clean conditions)

| Mode | Users | Direct P50 | Proxy P50 | Overhead P50 | Direct P95 | Proxy P95 | Overhead P95 | Direct P99 | Proxy P99 | Overhead P99 | Direct Mean | Through proxy | Overhead Mean | Direct RPS | Through proxy | RPS overhead |
|------|-------|-----------|-----------|-------------|-----------|-----------|-------------|-----------|----------|-------------|------------|--------------|--------------|-----------|--------------|-------------|
| Low | 5+5 | 100ms | 100ms | +0ms | 100ms | 100ms | +0ms | 100ms | 120ms | +20ms | 101ms | 103ms | +2ms | 49.3 | 48.3 | -1.0 |
| Medium | 20+20 | 100ms | 100ms | +0ms | 100ms | 110ms | +10ms | 100ms | 120ms | +20ms | 101ms | 104ms | +3ms | 194.7 | 190.7 | -4.0 |
| High | 100+100 | 100ms | 110ms | +10ms | 110ms | 120ms | +10ms | 110ms | 140ms | +30ms | 102ms | 109ms | +7ms | 895.7 | 840.4 | -55.4 |

### Real llama-server backend

| Mode | Users | Direct P50 | Proxy P50 | Overhead P50 | Direct P95 | Proxy P95 | Overhead P95 | Direct P99 | Proxy P99 | Overhead P99 | Direct Mean | Through proxy | Overhead Mean | Direct RPS | Through proxy | RPS overhead |
|------|-------|-----------|-----------|-------------|-----------|-----------|-------------|-----------|----------|-------------|------------|--------------|--------------|-----------|--------------|-------------|
| Low | 5+5 | 580ms | 590ms | +10ms | ~1000ms | ~950ms | ~0ms | ~1100ms | ~1100ms | ~0ms | 623ms | 617ms | -6ms | 7.9 | 8.1 | +0.1 |
| Medium | 20+20 | ~2300ms | ~2400ms | ~100ms | ~2700ms | ~2700ms | ~0ms | ~2900ms | ~2900ms | ~0ms | ~2302ms | ~2337ms | +35ms | 8.5 | 7.5 | -1.0 |
| High | 100+100 | 13000ms | 13000ms | ~0ms | 14000ms | 14000ms | ~0ms | 14000ms | 14000ms | ~0ms | 10624ms | 10723ms | +98ms | 7.8 | 7.6 | -0.2 |

Proxy overhead on clean conditions (mock): **~2–7ms** mean, **+0–30ms** P99 across all load levels with DB-backed rate limiter (accurate across multiple workers). Against real backend: **negligible at low/medium load** (~6–35ms mean), high load variance driven by upstream llama.cpp contention.
Run your own benchmarks: `python tests/benchmark/run.py [low|medium|high]` (add `--mock` for fixed-delay backend)

### Memory footprint

| Workers | Idle    | Under load | Growth |
|---------|---------|------------|--------|
| 1       | 53 MB   | 62 MB      | +9 MB  |
| 4       | 252 MB  | 273 MB     | +21 MB |

Per-worker baseline: **~53 MB**, load growth: **+4–6 MB** per worker.  
Identical footprint against mock and real backends — the proxy forwards without buffering responses. No memory growth observed across extended runs.

### Caveat

The aggregate overhead numbers (+2–5ms mean, +0–20ms P99 on mock) include asyncio event loop contention at high concurrency (100+ concurrent users per worker). Per-request proxy logic itself is **~0.13ms** with uvloop — the difference comes from how asyncio handles many simultaneous awaits on a single thread. With 4 uvicorn workers, each worker handles ~25 requests, keeping contention minimal.

Real backend P99 spikes (Medium mode, ~14s) are caused by llama.cpp single-thread inference bottleneck under 20 concurrent users — not proxy overhead. Proxy adds negligible latency regardless of backend saturation.

## How it compares

smol-llm-proxy is built for one specific case: **multiple llama-server instances, multiple users, per-user token accounting**. 

- **[LiteLLM](https://github.com/BerriAI/litellm)** — much broader scope: 100+ cloud providers, virtual keys, budgets, admin UI, fallbacks. Requires Postgres + Redis for full features. Use it if you need a production gateway across cloud LLMs.
- **[llama-swap](https://github.com/mostlygeek/llama-swap)** — solves a different problem: hot-swapping models on one llama.cpp instance. No users, no accounting. Use it if you run many models on one machine and want them loaded on demand.
- **[llama.cpp router mode](https://github.com/ggerganov/llama.cpp)** — built into llama-server itself. Same scope as llama-swap, no auth layer.

If you self-host several llama-server instances on one or more machines and want to share them with a small group while tracking usage, smol-llm-proxy is the smallest thing that does that. Otherwise, one of the above is probably a better fit.

## Architecture

```
[users] ──HTTPS──> [proxy :port] ──HTTP──> [llama-server 1 :port]
                        │                  [llama-server 2 :port]
                        │                  [llama-server N :port]
                        │
                        ├── in-memory cache (keys, aliases, routes) — TTL 30s
                        ├── validate API key + resolve routing (SQLite on first call, then cache)
                        ├── rate limiter: read-only DB check + in-memory commit with async batch flush
                        ├── forward request via connection-pooled httpx client
                        └── async log tokens + timings (background worker, no blocking)
```
