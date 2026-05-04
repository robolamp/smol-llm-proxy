# smol-llm-proxy

Lightweight API key proxy for llama.cpp servers with per-user token usage tracking.

Routes requests to multiple llama-servers based on model name, tracks token consumption per user, and manages access keys without restarting the backend.

## Features

- Per-user API keys (create / delete / toggle active)
- Multi-server routing by model name
- Model aliases (`alias` → `model-name.gguf`)
- Token usage logging: prompt/completion tokens, timings, TPS
- Streaming and non-streaming proxy support
- SQLite backend (zero external DB dependencies)
- In-memory cache for keys, aliases, and routing (~5ms overhead)

## Dependencies

```
fastapi  — ASGI web framework
uvicorn  — ASGI server
httpx    — HTTP client for forwarding
pydantic — request/response validation
sqlite3  — stdlib, built-in database
```

## Quick Start

```bash
# Install dependencies and the package itself
pip install fastapi uvicorn httpx pydantic pyyaml
pip install .

# Edit config.yaml with your llama-server URLs, then run
ADMIN_KEY=secret python -m smol_llm_proxy
```

The proxy listens on `0.0.0.0:8000` by default. Override with `PROXY_PORT` and `PROXY_HOST` env vars.

## Configuration

Edit `config.yaml` to define servers, models, and aliases. These are loaded into SQLite at startup and persist across restarts.

```yaml
servers:
  - name: my-server
    url: http://host:port
    api_key: ""       # optional
    models:
      - model-name.gguf

aliases:
  alias: model-name.gguf
```

## Admin API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/servers` | `POST` | Register a llama-server |
| `/admin/servers/{id}` | `DELETE` / `PATCH` | Remove or update server |
| `/admin/servers/{id}/models` | `POST` / `DELETE` | Assign/unassign model name |
| `/admin/keys` | `POST` | Create user key |
| `/admin/keys/{key}` | `DELETE` | Revoke key |
| `/admin/keys/{key}/toggle` | `PATCH` | Activate/deactivate key |
| `/admin/usage` | `GET` | View token usage logs |

All admin endpoints require `Authorization: Bearer <ADMIN_KEY>` header.

## Usage Logs

Each request logs: user, server, model name, prompt/completion tokens, timings (ms), and total tokens. No conversation content is stored.

```bash
curl "http://localhost:8000/admin/usage?key_id=1" \
  -H "Authorization: Bearer $ADMIN_KEY"
```

## Docker

### docker-compose (recommended)

```bash
cp .env.example .env          # set ADMIN_KEY
cp config.example.yaml config.yaml  # fill in your servers
docker compose up -d --build
```

Volumes:
- `db-data` — SQLite DB persists across container restarts (`/data/proxy.db`)
- `./config.yaml:/config/config.yaml:ro` — config mounted read-only

Env vars: `ADMIN_KEY`, `PROXY_PORT`, `DB_PATH`, `CONFIG_PATH`

### Dockerfile only

```bash
docker build -t smol-llm-proxy .
docker run -p 8000:8000 \
  -e ADMIN_KEY=secret \
  -v db-data:/data \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  smol-llm-proxy
```

## Benchmarking

Proxy overhead measured with Locust against real llama-server backends.

### Low load (5 concurrent users)

| Metric | Direct | Through proxy | Overhead |
|--------|--------|---------------|----------|
| P50 latency | 280ms | 290ms | +10ms |
| P95 latency | 680ms | 700ms | +20ms |
| Mean latency | 326ms | 333ms | +7ms |
| RPS | 15.3 | 14.9 | -0.3 |

### Medium load (20 concurrent users)

| Metric | Direct | Through proxy | Overhead |
|--------|--------|---------------|----------|
| P50 latency | 1200ms | 1200ms | ~0 |
| P95 latency | 1600ms | 1600ms | ~0 |
| Mean latency | 1234ms | 1277ms | +44ms |
| RPS | 15.9 | 15.4 | -0.5 |

At low load proxy adds **~10ms** overhead per request. At higher load this becomes negligible compared to backend queueing delay.

Run your own benchmarks: `bash tests/benchmark/run.sh [low|medium|high]`

## Architecture

```
[users] ──HTTPS──> [proxy :port] ──HTTP──> [llama-server 1 :port]
                       │                   [llama-server 2 :port]
                       │                   [llama-server N :port]
                       │
                       ├── in-memory cache (keys, aliases, routes)
                       ├── validate API key (SQLite)
                       ├── resolve alias —> real model name
                       ├── route by model name —> matching server
                       ├── forward request (+ replace auth header)
                       └── async log tokens + timings from response
```

