# smol-llm-proxy

[![PyPI version](https://img.shields.io/pypi/v/smol-llm-proxy)](https://pypi.org/project/smol-llm-proxy/)
[![CI](https://github.com/robolamp/smol-llm-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/robolamp/smol-llm-proxy/actions)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A small API proxy for self-hosted llama.cpp setups. Routes across multiple llama-server instances, per-user API keys, token usage tracking. In-memory rate limiting (RPM/TPM) with async SQLite flush. <=1100 code lines (excluding blanks, docstrings, comments), ~53 MB RAM, ~2–5ms mean overhead on clean conditions.

Built for the case where you run multiple llama-server instances (different models, different GPUs) and want to share them across users with token tracking. Not a replacement for LiteLLM or llama-swap — see comparison below.

## Features

- Per-user API keys (create / delete / toggle active)
- **Rate limiting** — per-key RPM/TPM, sliding 60-second window, 429 + `Retry-After` on exceed
- **Multi-server routing** by model name with in-memory cache (TTL 30s)
- **Model aliases** (`alias` -> `model-name.gguf`)
- **Token usage logging** — prompt/completion tokens, timings, 90-day retention with daily purge
- **Streaming and non-streaming** proxy support for chat, completions, and embeddings
- **Connection-pooled httpx client** (keepalive connections to backends)
- **SQLite backend** (zero external DB dependencies)

## Dependencies

| Package | Version | Notes |
|---------|---------|-------|
| `fastapi` | >= 0.115.0 | Web framework |
| `uvicorn[standard]` | >= 0.30.0 | ASGI server |
| `httpx` | >= 0.27.0 | Async HTTP client |
| `pydantic` | >= 2.0.0 | Data validation |
| `pyyaml` | >= 6.0 | Config parsing |
| `orjson` | >= 3.9.0 | Fast JSON serialization |
| `uvloop` | >= 0.19.0 | Linux/macOS only (`sys_platform != 'win32'`) |

Optional dev dependencies: `pytest`, `pytest-rerunfailures`, `ruff` (see `pyproject.toml`).

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
  -v db-data:/app/data \
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
# Edit .env and set ADMIN_KEY, then fill in config.yaml with your servers
ADMIN_KEY=secret smol-llm-proxy
# or: ADMIN_KEY=secret python -m smol_llm_proxy
```

Or download from GitHub:

```bash
curl -sO https://raw.githubusercontent.com/robolamp/smol-llm-proxy/main/.env.example && cp .env.example .env
curl -sO https://raw.githubusercontent.com/robolamp/smol-llm-proxy/main/config.example.yaml && cp config.example.yaml config.yaml
```

### From source

```bash
git clone https://github.com/robolamp/smol-llm-proxy.git
cd smol-llm-proxy
pip install -e .
```

Copy example configs and run:

```bash
cp .env.example .env                # set ADMIN_KEY
cp config.example.yaml config.yaml  # fill in your servers
ADMIN_KEY=secret python -m smol_llm_proxy
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

### Config sync semantics

On startup, `config.yaml` is the **source of truth**:
- Servers/aliases **not present** in `config.yaml` are **deleted** from the database.
- Servers/aliases in the database **not present** in `config.yaml` are **deleted**.
- Models listed under a server in YAML are synced; extra models on that server are removed.
- API keys, usage logs, and rate limits are **not** affected by config sync.
- The in-memory route cache is cleared after sync.

This means if you remove a server or alias from `config.yaml` and restart, it will be gone from the database. If you want to keep something permanently, manage it via the Admin API (not YAML).

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_KEY` | **required** | Bearer token for `/admin/*` endpoints. Startup fails without it. |
| `PROXY_HOST` | `0.0.0.0` | Listen address |
| `PROXY_PORT` | `8000` | Listen port |
| `DB_PATH` | `data/proxy.db` | SQLite database location |
| `CONFIG_PATH` | `config.yaml` | Path to YAML config file |
| `HTTPX_TIMEOUT` | `120` | Upstream HTTP client timeout (seconds) |
| `PROXY_MAX_CONNECTIONS` | `50` | httpx connection pool max size |
| `PROXY_MAX_KEEPALIVE` | `20` | httpx keepalive pool max size |
| `PROXY_KEEPALIVE_EXPIRY` | `30.0` | httpx keepalive expiry (seconds) |
| `BENCH_COLD_CACHE` | *(unset)* | Set to `"1"` to disable in-memory caches (for benchmarking) |

For `pip install`, set them in shell or via a `.env`-like loader. For Docker Compose, put them in `.env`:

```bash
ADMIN_KEY=secret
PROXY_PORT=8000
```

### Docker volumes

The Compose setup mounts two volumes:

- `${DATA_DIR:-./data}:/app/data` — SQLite database (`DB_PATH=/app/data/proxy.db`), persists across container restarts. Override with `DATA_DIR` in `.env`.
- `./config.yaml:/config/config.yaml:ro` — config file, read-only. Set `CONFIG_PATH=/config/config.yaml` in Compose (hardcoded, not from `.env`).


## Admin API

All admin endpoints require `Authorization: Bearer <ADMIN_KEY>` header.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/keys` | `GET` / `POST` | List API keys / Create user key |
| `/admin/keys/{key_id}` | `DELETE` | Revoke key (integer id, not key string) |
| `/admin/keys/{key_id}/toggle` | `PATCH` | Activate/deactivate key |
| `/admin/keys/{key_id}/limits` | `PUT` | Set per-key RPM/TPM rate limits |
| `/admin/servers` | `GET` / `POST` | List servers / Register a llama-server |
| `/admin/servers/{server_id}` | `DELETE` / `PATCH` | Remove or update server |
| `/admin/servers/{server_id}/models` | `POST` | Assign model to server (use `?reassign=true` to move from another server) |
| `/admin/servers/{server_id}/models/{model_name}` | `DELETE` | Unassign model from server |
| `/admin/aliases` | `GET` / `POST` | List aliases / Create alias |
| `/admin/aliases/{alias_name}` | `PATCH` / `DELETE` | Update alias target / Delete alias |
| `/admin/usage` | `GET` | Token usage logs + summary (accepts `key_id`, `server_id`, `start_date`, `end_date`, `limit`, `offset`) |
| `/admin/usage/summary/real` | `GET` | Usage summary grouped by real model name + server |

**Note:** Key operations (`DELETE`, `PATCH /toggle`, `PUT /limits`) use integer `key_id` from the database, not the API key string itself.

### Rate limits

Default limits: **100 RPM**, **50,000 TPM**. Set custom limits per key:

```bash
curl -X PUT http://localhost:8000/admin/keys/1/limits \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"rpm_limit": 200, "tpm_limit": 100000}'
```

Exceeding limits returns `429 Too Many Requests` with a `Retry-After` header. The rate limiter reads from SQLite + in-memory counters on every check and commits in-memory with async batch flush — one read-only SELECT per request, no commit overhead. **Note:** rate counters and caches are process-local; under `--workers N` each worker maintains independent counters (limits multiply by N).

### Model assignment

Each model can be assigned to **exactly one server** at a time. Assigning a model that is already on another server returns `409 Conflict`:

```bash
curl -X POST http://localhost:8000/admin/servers/2/models \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"model_name": "qwen3-30b-a3b-instruct.gguf"}'
# 409: Model 'qwen3-30b-a3b-instruct.gguf' already assigned to server 'server-a' (id=1). Use ?reassign=true to move.
```

Move a model to a different server with `?reassign=true`:

```bash
curl -X POST "http://localhost:8000/admin/servers/2/models?reassign=true" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"model_name": "qwen3-30b-a3b-instruct.gguf"}'
```

Multiple aliases can point to the same model. Aliases can be switched to a different model via `PATCH`:

```bash
curl -X PATCH http://localhost:8000/admin/aliases/fast \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"real_model_name": "new-model.gguf"}'
```

### Usage

View raw usage logs:

```bash
curl "http://localhost:8000/admin/usage?key_id=1&start_date=2024-01-01T00:00:00&end_date=2024-01-31T23:59:59" \
  -H "Authorization: Bearer $ADMIN_KEY"
```

Usage summary grouped by real model name + server (aggregates across all aliases):

```bash
curl "http://localhost:8000/admin/usage/summary/real?key_id=1&start_date=2024-01-01T00:00:00&end_date=2024-01-31T23:59:59" \
  -H "Authorization: Bearer $ADMIN_KEY"
```

All endpoints accept: `key_id`, `server_id`, `start_date` (ISO 8601), `end_date` (ISO 8601), `limit`, `offset`. Usage logs are preserved even after a server or key is deleted.

## Proxy Endpoints

These forward to llama-server backends based on model name routing.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | `POST` | Chat completions (streaming + non-streaming) |
| `/v1/completions` | `POST` | Legacy completions |
| `/v1/embeddings` | `POST` | Embeddings |
| `/v1/models` | `GET` | List available models (requires user API key — returns 401 without Authorization, 403 for invalid/inactive key, fans out to all backends) |
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
| Low | 5+5 | 100ms | 100ms | +0ms | 100ms | 100ms | +0ms | 100ms | 110ms | +10ms | 101ms | 104ms | +2ms | 49.1 | 48.1 | -1.0 |
| Medium | 20+20 | 100ms | 100ms | +0ms | 100ms | 110ms | +10ms | 100ms | 110ms | +10ms | 102ms | 104ms | +2ms | 194.5 | 190.7 | -3.8 |
| High | 100+100 | 100ms | 110ms | +10ms | 110ms | 110ms | +0ms | 110ms | 120ms | +10ms | 103ms | 106ms | +4ms | 893.7 | 864.7 | -29.0 |

### Real llama-server backend

| Mode | Users | Direct P50 | Proxy P50 | Overhead P50 | Direct P95 | Proxy P95 | Overhead P95 | Direct P99 | Proxy P99 | Overhead P99 | Direct Mean | Through proxy | Overhead Mean | Direct RPS | Through proxy | RPS overhead |
|------|-------|-----------|-----------|-------------|-----------|-----------|-------------|-----------|----------|-------------|------------|--------------|--------------|-----------|--------------|-------------|
| Low | 5+5 | 430ms | 430ms | +0ms | 760ms | 750ms | -10ms | 850ms | 870ms | +20ms | 460ms | 461ms | +1ms | 10.8 | 10.8 | 0.0 |

Proxy overhead on clean conditions (mock): **~2–4ms** mean, **+0–10ms** P99 across all load levels with DB-backed rate limiter. Against real backend: **negligible at low load** (+1ms mean), higher load limited by SQLite I/O under concurrent bench runs.
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
[users] ──HTTP──> [proxy :port] ──HTTP──> [llama-server 1 :port]
                       │                  [llama-server 2 :port]
                       │                  [llama-server N :port]
                       │
                       ├── in-memory cache (keys, routes) — TTL 30s
                       ├── validate API key + resolve routing (SQLite on first call, then cache)
                       ├── rate limiter: read-only DB check + in-memory reservation + reconcile on response
                       ├── async batch flush to SQLite (1s interval)
                       ├── forward request via connection-pooled httpx client
                       ├── async usage logger (batch queue, 50 items / 1s timeout)
                       └── retention cleanup (90-day purge, daily)
```

> TLS termination is handled externally — Cloudflare, Caddy, nginx, or any reverse proxy in front of the proxy.
