# smol-llm-proxy

An API proxy for llama.cpp. Multi-server routing, per-user keys, token accounting in less than 1000 lines of code. Minimal dependencies, ~5ms overhead.

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

Proxy overhead measured with Locust using **parallel concurrent execution** — both benchmarks (direct and proxy) hit the same backend simultaneously for a fair comparison.

**Hardware:** i9-14900K, RTX 4090 | **Model:** Qwen3.5-2B-GGUF | **Backend:** llama.cpp server

### Mock backend (fixed 100ms delay, clean conditions)

| Mode | Users | Direct P50 | Proxy P50 | Overhead P50 | Direct Mean | Through proxy | Overhead Mean | Direct RPS | Through proxy | RPS overhead |
|------|-------|-----------|-----------|-------------|------------|--------------|--------------|-----------|--------------|-------------|
| Low | 5+5 | 100ms | 100ms | +0ms | 101ms | 106ms | +5ms | 49.2 | 47.0 | -2.3 |
| Medium | 20+20 | 100ms | 110ms | +10ms | 101ms | 113ms | +12ms | 195.0 | 174.5 | -20.5 |

### Real llama-server backend

| Mode | Users | Direct P50 | Proxy P50 | Overhead P50 | Direct Mean | Through proxy | Overhead Mean | Direct RPS | Through proxy | RPS overhead |
|------|-------|-----------|-----------|-------------|------------|--------------|--------------|-----------|--------------|-------------|
| Low | 5+5 | 560ms | 550ms | ~0ms | 567ms | 595ms | +27ms | 8.8 | 8.3 | -0.4 |
| Medium | 20+20 | ~2300ms | ~2300ms | ~0ms | ~2306ms | ~2344ms | +38ms | 8.4 | 8.3 | -0.1 |

At low load the proxy adds **~5ms overhead** on clean conditions (mock) and **~27ms** against real backend — negligible compared to backend latency (~560ms+).
Run your own benchmarks: `python tests/benchmark/run.py [low|medium|high]` (add `--mock` for fixed-delay backend)

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
