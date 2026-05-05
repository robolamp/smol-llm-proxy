# smol-llm-proxy

An API proxy for llama.cpp. Multi-server routing, per-user keys, token accounting. Minimal dependencies, ~5ms overhead.

## Features

- Per-user API keys (create / delete / toggle active)
- Multi-server routing by model name with in-memory cache (~5ms overhead)
- Model aliases (`alias` -> `model-name.gguf`)
- Token usage logging: prompt/completion tokens, timings
- Streaming and non-streaming proxy support
- Connection-pooled httpx client (keepalive connections to backends)
- SQLite backend (zero external DB dependencies)

## Quick Start

### Docker Compose (recommended)

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
pip install .
cp config.example.yaml config.yaml  # fill in your servers
ADMIN_KEY=secret python -m smol_llm_proxy
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
| `DB_PATH` | `./data/proxy.db` | SQLite database location |
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
| `/admin/aliases` | `GET` / `POST` | List or create model aliases |
| `/admin/aliases/{alias_name}` | `DELETE` | Delete alias |
| `/admin/usage` | `GET` | View token usage logs |

**Note:** Key operations (`DELETE`, `PATCH /toggle`) use integer `key_id` from the database, not the API key string itself.

## Proxy Endpoints

These forward to llama-server backends based on model name routing.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | `POST` | Chat completions (streaming + non-streaming) |
| `/v1/completions` | `POST` | Legacy completions |
| `/v1/embeddings` | `POST` | Embeddings |
| `/v1/models` | `GET` | List available models (no auth required) |
| `/health` | `GET` | Health check (no auth required) |

## Usage Logs

Each request logs: user, server, model name, prompt/completion tokens, timings (ms), and total tokens. No conversation content is stored.

```bash
curl "http://localhost:8000/admin/usage?key_id=1" \
  -H "Authorization: Bearer $ADMIN_KEY"
```

## Benchmarking

Proxy overhead measured with Locust against real llama-server backends using **parallel concurrent execution** — both benchmarks (direct and proxy) hit the same backend simultaneously for a fair comparison.

### Low load (5 users each, 30s)

| Metric | Direct | Through proxy | Overhead |
|--------|--------|---------------|----------|
| P50 latency | 560ms | 570ms | +10ms |
| Mean latency | 596ms | 601ms | +5ms |
| RPS | 8.3 | 8.3 | ~0 |

### Medium load (20 users each, 60s)

| Metric | Direct | Through proxy | Overhead |
|--------|--------|---------------|----------|
| P50 latency | ~2300ms | ~2300ms | ~0ms |
| Mean latency | ~2.3s | ~2.4s | +30ms |
| RPS | 8.4 | 8.4 | ~0 |

### High load (100 users each, 60s)

| Metric | Direct | Through proxy | Overhead |
|--------|--------|---------------|----------|
| P50 latency | ~12-13s | ~12-13s | ~0ms |
| Mean latency | ~10.1s | ~10.4s | +270ms |
| RPS | 8.1 | 7.9 | -0.2 |

At low load the proxy adds **~5ms mean** overhead — less than 1% of total request time. The proxy uses in-memory caching for keys, aliases, and routing; SQLite is touched only once per cold start, then all lookups are in-memory. Token logging is fully async via background worker — no blocking on hot path. At higher load the ~270ms mean overhead at 100 concurrent users is just **~2.7%** of total request time (~10s), negligible compared to backend queueing delay.

Run your own benchmarks: `python tests/benchmark/run.py [low|medium|high]`

## Architecture

```
[users] ──HTTPS──> [proxy :port] ──HTTP──> [llama-server 1 :port]
                        │                  [llama-server 2 :port]
                        │                  [llama-server N :port]
                        │
                        ├── in-memory cache (keys, aliases, routes)
                        ├── validate API key + resolve routing (SQLite on first call, then cache)
                        ├── forward request via connection-pooled httpx client
                        └── async log tokens + timings (background worker, no blocking)
```
