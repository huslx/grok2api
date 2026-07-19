<img alt="Grok2API" src="https://github.com/user-attachments/assets/037a0a6e-7986-41cc-b4af-04df612ee886" />

[![Python](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Version](https://img.shields.io/badge/version-2.0.4.rc4-111827)](../pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-16a34a)](../LICENSE)
[![Docker](https://img.shields.io/badge/docker-local%20build-2496ED?logo=docker&logoColor=white)](../Dockerfile)
[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-DC2626?logo=bookstack&logoColor=white)](../README.md)

> [!NOTE]
> This project is for learning and research only. You must comply with Grok's Terms of Service and your local laws. Do not use it for unlawful purposes. Forks and PRs should preserve original author and frontend attribution.

<br>

**Grok2API** is a **FastAPI**-based Grok gateway that exposes Grok web capabilities through OpenAI / Anthropic-compatible APIs. Highlights:

- OpenAI-compatible endpoints: `/v1/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/images/generations`, `/v1/images/edits`, `/v1/videos`, `/v1/videos/{video_id}`, `/v1/videos/{video_id}/content`
- Anthropic-compatible endpoint: `/v1/messages`
- Streaming and non-streaming chat, explicit reasoning output, function tools passthrough, unified token / usage accounting
- Multi-account pool, tiered selection, failure feedback, quota sync and auto maintenance
- Local image / video caching with reverse-proxied URLs
- Text-to-image, image edit, text-to-video, image-to-video
- Built-in Admin console, Web Chat, Masonry image gallery, ChatKit voice page
- `console.x.ai` free account support plus `*-console` / CLI models (e.g. `grok-4.5`)

<br>

## Table of Contents

- [Image Info](#image-info)
- [Quick Start](#quick-start)
- [Upgrade and Rollback](#upgrade-and-rollback)
- [Reverse Proxy (Nginx example)](#reverse-proxy-nginx-example)
- [WebUI](#webui)
- [Account Management](#account-management)
- [Runtime Configuration](#runtime-configuration)
- [Environment Variables](#environment-variables)
- [Models](#models)
- [API Reference](#api-reference)
- [Examples](#examples)
- [Anti-block Stack (WARP + FlareSolverr)](#anti-block-stack-warp--flaresolverr)
- [Utility Scripts](#utility-scripts)
- [FAQ](#faq)
- [Project Layout](#project-layout)
- [Credits](#credits)
- [License](#license)

<br>

## Image Info

This repository builds on top of [chenyme/grok2api](https://github.com/chenyme/grok2api). **No public remote image is available; deploy with a local build.**

| Field | Value |
| :-- | :-- |
| Local image tag | `grok2api:local` (compose build output) |
| Architecture | Host architecture (CI targets `linux/amd64`, `linux/arm64`) |
| Base image | `python:3.13-alpine` |
| Default port | `8000` |
| Data dir | `/app/data` |
| Logs dir | `/app/logs` |

> `docker-compose.yml` uses `build: .` and `pull_policy: build`, so Compose will not pull from GHCR.

<br>

## Quick Start

### Option 1: Docker Compose (recommended, local build)

```bash
git clone https://github.com/huslx/grok2api
cd grok2api
cp .env.example .env

# Always build from the local Dockerfile
docker compose up -d --build
```

Tail logs:

```bash
docker compose logs -f grok2api
```

Anti-block stack (WARP + FlareSolverr — see [Anti-block Stack](#anti-block-stack-warp--flaresolverr)):

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

### Option 2: Plain Docker (local build)

```bash
docker build -t grok2api:local .

docker run -d \
  --name grok2api \
  -p 8000:8000 \
  -e TZ=Asia/Shanghai \
  -e LOG_LEVEL=INFO \
  -e ACCOUNT_STORAGE=local \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  grok2api:local
```

Windows PowerShell:

```powershell
docker build -t grok2api:local .

docker run -d `
  --name grok2api `
  -p 8000:8000 `
  -e TZ=Asia/Shanghai `
  -e LOG_LEVEL=INFO `
  -e ACCOUNT_STORAGE=local `
  -v ${PWD}/data:/app/data `
  -v ${PWD}/logs:/app/logs `
  --restart unless-stopped `
  grok2api:local
```

### Option 3: From source

Prerequisites: Python 3.13+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/huslx/grok2api
cd grok2api
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

### First-time setup

After the service is up, open `http://localhost:8000/admin/login`. Default password is `grok2api`. Then:

1. Change `app.app_key` (Admin console password)
2. Set `app.api_key` (API auth key; leave empty to disable auth)
3. Set `app.app_url` (publicly reachable base URL; otherwise image / video links may return 403)
4. Import accounts under **Account management** (see [Account Management](#account-management))

> Runtime config is persisted to `${DATA_DIR}/config.toml` and applied immediately. No container restart is required.  
> Defaults ship in `config.defaults.toml` at the repo root.

<br>

## Upgrade and Rollback

```bash
# Pull code, rebuild locally, restart (./data and ./logs volumes are kept)
git pull
docker compose up -d --build

# Anti-block stack: rebuild only the app service
docker compose -f docker-compose.warp.yml up -d --build --no-deps grok2api
```

Rollback: check out an older commit, then run `docker compose up -d --build` again.

<br>

## Reverse Proxy (Nginx example)

```nginx
server {
    listen 443 ssl http2;
    server_name your.domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Required for streaming
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

After enabling the reverse proxy, set `app.app_url` to `https://your.domain.com` in the Admin console.

<br>

## WebUI

| Page | Path |
| :-- | :-- |
| Admin login | `/admin/login` |
| Account management | `/admin/account` |
| Config management | `/admin/config` |
| Cache management | `/admin/cache` |
| WebUI login | `/webui/login` |
| Web Chat | `/webui/chat` |
| Masonry | `/webui/masonry` |
| ChatKit | `/webui/chatkit` |

### Authentication

| Scope | Config | Rule |
| :-- | :-- | :-- |
| `/v1/*` | `app.api_key` | No auth when empty |
| `/admin/*` | `app.app_key` | Default `grok2api` |
| `/webui/*` | `app.webui_enabled`, `app.webui_key` | Disabled by default; no extra check when `webui_key` is empty |

<br>

## Account Management

### Account types

| Type | Credential | Typical models | Notes |
| :-- | :-- | :-- | :-- |
| **Paid** | `sso` cookie from grok.com / x.ai | `grok-4.20-*`, `grok-4.3-beta`, Imagine, etc. | Official web / paid quota |
| **Free Console** | `sso` + optional CF Clearance | `*-console`, `grok-4.3-{low,medium,high}` | Routed via `console.x.ai` |
| **CLI (grok-4.5)** | Same `sso`, OIDC at runtime | `grok-4.5`, `grok-4.5-console` | Requires SSO→OIDC (can be automatic) |

Admin path: **Admin → Account**. Batch paste accepts an optional `sso=` prefix.

### Paid account setup

1. Sign in to an x.ai / grok.com account with the required plan
2. Open DevTools (F12) → Network and copy the **`sso`** cookie value
3. Paste it under **Account → Add**
4. If your egress IP is frequently challenged by Cloudflare, configure [Proxy & Clearance](#proxy--clearance)

> Paid models depend on account tier (basic / super / heavy). Missing quota or tier causes failures or account rotation.

### Free Console account setup

1. Open browser DevTools (F12)
2. Visit `https://console.x.ai/`
3. In the Network tab, copy from cookies:
   - `sso`
   - `cf_clearance` (if present)
4. Add the SSO in Admin → Account; CF cookies are usually managed under **Config → Proxy / Clearance** (`manual` or `flaresolverr`)

### CLI / grok-4.5 (OIDC)

`grok-4.5` uses the CLI path and needs an OIDC `access_token` obtained from SSO via Device Flow.

Defaults from `config.defaults.toml`:

| Key | Default | Meaning |
| :-- | :-- | :-- |
| `features.auto_oidc_on_import` | `true` | Queue SSO→OIDC after import / registration |
| `features.auto_oidc_workers` | `8` | Batch convert concurrency |
| `chat.cli_reasoning_effort` | `medium` | Default CLI effort (overridable per request) |
| `chat.cli_account_retries` | `8` | Min account switches when OIDC is not warm |

Manual conversion options:

- **Admin**: OIDC convert action on the account page (`POST /admin/api/tokens/oidc-convert`)
- **CLI** (see [Utility Scripts](#utility-scripts)):

```bash
uv run python scripts/sso_to_oidc.py --from-db --limit 10 --workers 2
```

Runtime OIDC cache defaults to `${DATA_DIR}/oidc_auth.json`.

> SSO, CF Clearance, and OIDC tokens are secrets. Never commit them.

<br>

## Runtime Configuration

Runtime file: `${DATA_DIR}/config.toml` (default `./data/config.toml`).  
First boot seeds from `config.defaults.toml`. You can also edit via **Admin → Config** (applies immediately).

Env overrides use `GROK_<SECTION>_<KEY>` (uppercase), for example:

- `GROK_APP_API_KEY` → `app.api_key`
- `GROK_FEATURES_STREAM` → `features.stream`

Deeply nested keys are easier to edit in `config.toml` or the Admin UI.

### Access control `[app]`

| Key | Default | Description |
| :-- | :-- | :-- |
| `app_key` | `grok2api` | Admin password |
| `api_key` | `""` | API Bearer; empty disables `/v1/*` auth |
| `app_url` | `""` | Public base URL (required for local media proxy links) |
| `webui_enabled` | `false` | Enable WebUI |
| `webui_key` | `""` | WebUI password; empty skips extra check |

### Feature flags `[features]`

| Key | Default | Description |
| :-- | :-- | :-- |
| `stream` | `true` | Default streaming when request omits `stream` |
| `thinking` | `true` | Emit reasoning / thinking |
| `thinking_summary` | `false` | Summarized reasoning instead of raw |
| `memory` | `false` | Conversation memory |
| `temporary` | `true` | Temporary chats |
| `auto_chat_mode_fallback` | `true` | Fall back from AUTO when quota is exhausted |
| `image_format` | `grok_url` | `grok_url` / `local_url` / `grok_md` / `local_md` / `base64` |
| `imagine_public_image_proxy` | `false` | Download & locally proxy imagine-public images from WebSocket |
| `video_format` | `grok_url` | `grok_url` / `local_url` / `grok_html` / `local_html` |
| `imagine_public_video_proxy` | `false` | Download & locally proxy imagine-public videos from upstream |
| `enable_nsfw` | `true` | Allow NSFW image-related features |
| `auto_oidc_on_import` | `true` | Auto SSO→OIDC after import |
| `show_search_sources` | `false` | Append `## Sources` text to the body |

### Proxy & Clearance

`[proxy.egress]`:

| Key | Default | Description |
| :-- | :-- | :-- |
| `mode` | `direct` | `direct` / `single_proxy` / `proxy_pool` |
| `proxy_url` | `""` | Single proxy for API traffic |
| `proxy_pool` | `[]` | Proxy pool |
| `resource_proxy_url` | `""` | Media download proxy; falls back to `proxy_url` |
| `skip_ssl_verify` | `false` | Skip SSL verify for proxy TLS |

`[proxy.clearance]`:

| Key | Default | Description |
| :-- | :-- | :-- |
| `mode` | `none` | `none` / `manual` / `flaresolverr` |
| `cf_cookies` | `""` | manual: full cookie string |
| `user_agent` | Chrome 136 UA | Must match cookies |
| `flaresolverr_url` | `""` | FlareSolverr base URL |
| `refresh_interval` | `3600` | Clearance refresh interval (seconds) |

### Retry & account scheduling

| Key | Default | Description |
| :-- | :-- | :-- |
| `retry.max_retries` | `1` | Max account switches (0 = no retry) |
| `retry.on_codes` | `429,401,503` | Status codes that trigger rotation |
| `account.refresh.enabled` | `true` | `true` = quota-aware scoring; `false` = random pick |
| `account.selection.max_inflight` | `8` | Per-account concurrency cap |

Full defaults: [`config.defaults.toml`](../config.defaults.toml).

<br>

## Environment Variables

Bootstrap-time variables (`.env` / Compose / `docker run -e`). Template: [`.env.example`](../.env.example).

| Name | Description | Default |
| :-- | :-- | :-- |
| `TZ` | Timezone | `Asia/Shanghai` |
| `LOG_LEVEL` | Log level | `INFO` |
| `LOG_FILE_ENABLED` | Write file logs | `true` |
| `ACCOUNT_SYNC_INTERVAL` | Account directory sync interval (s) | `30` |
| `ACCOUNT_SYNC_ACTIVE_INTERVAL` | Active sync interval after a change (s) | `3` |
| `SERVER_HOST` | Listen host | `0.0.0.0` |
| `SERVER_PORT` | Listen port | `8000` |
| `SERVER_WORKERS` | Granian workers | `1` |
| `HOST_PORT` | Compose host port mapping | `8000` |
| `DATA_DIR` | Data root | `./data` |
| `LOG_DIR` | Logs dir | `./logs` |
| `ACCOUNT_STORAGE` | Backend: `local` / `redis` / `mysql` / `postgresql` | `local` |
| `ACCOUNT_LOCAL_PATH` | SQLite path for `local` mode | `${DATA_DIR}/accounts.db` |
| `ACCOUNT_REDIS_URL` | DSN for `redis` mode | `""` |
| `ACCOUNT_MYSQL_URL` | DSN for `mysql` mode | `""` |
| `ACCOUNT_POSTGRESQL_URL` | DSN for `postgresql` mode | `""` |
| `ACCOUNT_SQL_POOL_SIZE` | SQL pool core size | `5` |
| `ACCOUNT_SQL_MAX_OVERFLOW` | SQL pool max overflow | `10` |
| `ACCOUNT_SQL_POOL_TIMEOUT` | Pool checkout timeout (s) | `30` |
| `ACCOUNT_SQL_POOL_RECYCLE` | Connection recycle time (s) | `1800` |
| `CONFIG_LOCAL_PATH` | Runtime config file path | `${DATA_DIR}/config.toml` |

<br>

## Models

> Use `GET /v1/models` for the live list.

### Chat (paid)

| Model | mode | tier |
| :-- | :-- | :-- |
| `grok-4.20-0309-non-reasoning` | `fast` | `basic` |
| `grok-4.20-0309` | `auto` | `super` |
| `grok-4.20-0309-reasoning` | `expert` | `super` |
| `grok-4.20-0309-non-reasoning-super` | `fast` | `super` |
| `grok-4.20-0309-super` | `auto` | `super` |
| `grok-4.20-0309-reasoning-super` | `expert` | `super` |
| `grok-4.20-0309-non-reasoning-heavy` | `fast` | `heavy` |
| `grok-4.20-0309-heavy` | `auto` | `heavy` |
| `grok-4.20-0309-reasoning-heavy` | `expert` | `heavy` |
| `grok-4.20-multi-agent-0309` | `heavy` | `heavy` |
| `grok-4.20-fast` | `fast` | `basic`, prefers higher-tier accounts |
| `grok-4.3-fast` | `fast` | `basic`, prefers higher-tier accounts |
| `grok-4.20-auto` | `auto` | `super`, prefers higher-tier accounts |
| `grok-4.20-expert` | `expert` | `super`, prefers higher-tier accounts |
| `grok-4.20-heavy` | `heavy` | `heavy` |
| `grok-4.3-beta` | `grok-420-computer-use-sa` | `super` |

### Chat (console.x.ai / CLI)

| Model | Notes |
| :-- | :-- |
| `grok-4.3-console` | Console default |
| `grok-4.3-low` / `medium` / `high` | Thinking effort variants |
| `grok-4.5` / `grok-4.5-console` | CLI (OIDC) |
| `grok-4.20-0309-console` | Console |
| `grok-4.20-0309-non-reasoning-console` | Console non-reasoning |
| `grok-4.20-0309-reasoning-console` | Console fixed reasoning |
| `grok-4.20-multi-agent-console` | Console multi-agent |
| `grok-4.20-multi-agent-low` / `medium` / `high` / `xhigh` | Multi-agent effort |
| `grok-build-console` | Grok Build 0.1 |

### Image / Image Edit / Video

| Model | mode | tier |
| :-- | :-- | :-- |
| `grok-imagine-image-lite` | `fast` | `basic` |
| `grok-imagine-image` | `auto` | `super` |
| `grok-imagine-image-pro` | `auto` | `super` |
| `grok-imagine-image-edit` | `auto` | `super` |
| `grok-imagine-video` | `auto` | `super` |

<br>

## API Reference

| Endpoint | Auth | Description |
| :-- | :-- | :-- |
| `GET /v1/models` | yes | List enabled models |
| `GET /v1/models/{model_id}` | yes | Get a single model |
| `POST /v1/chat/completions` | yes | Unified chat / image / video entry |
| `POST /v1/responses` | yes | OpenAI Responses API subset |
| `POST /v1/messages` | yes | Anthropic Messages API |
| `POST /v1/images/generations` | yes | Standalone image generation |
| `POST /v1/images/edits` | yes | Standalone image editing |
| `POST /v1/videos` | yes | Async video job creation |
| `GET /v1/videos/{video_id}` | yes | Query a video job |
| `GET /v1/videos/{video_id}/content` | yes | Download the final video |
| `GET /v1/files/video?id=...` | no | Locally cached video |
| `GET /v1/files/image?id=...` | no | Locally cached image |
| `GET /health` | no | Health check |

Common `POST /v1/chat/completions` fields:

| Field | Description |
| :-- | :-- |
| `model` | Model name |
| `messages` | OpenAI-style messages |
| `stream` | Streaming; falls back to `features.stream` when omitted |
| `reasoning_effort` | Effort (e.g. `low` / `medium` / `high`; used by CLI) |
| `tools` / `tool_choice` | Function tools passthrough |
| `image_config` | `n` / `size` / `response_format` for image models |
| `video_config` | `seconds` / `size` / `resolution_name` / `preset` for video models |

<br>

## Examples

Set `$GROK2API_API_KEY` to your `app.api_key` (omit the header if empty).

### Paid account chat

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "stream": true,
    "reasoning_effort": "high",
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

### Free Console chat

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.3-high",
    "stream": true,
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

### CLI / grok-4.5

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.5",
    "stream": true,
    "reasoning_effort": "medium",
    "messages": [
      {"role":"user","content":"Explain what an API gateway is in three sentences"}
    ]
  }'
```

### Function tools

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "messages": [
      {"role":"user","content":"What is the weather in Beijing?"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get weather for a city",
          "parameters": {
            "type": "object",
            "properties": {
              "city": {"type": "string"}
            },
            "required": ["city"]
          }
        }
      }
    ]
  }'
```

### Anthropic Messages

```bash
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: $GROK2API_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "grok-4.3-high",
    "max_tokens": 1024,
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

### OpenAI Responses

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "input": "Introduce yourself in one sentence",
    "stream": false
  }'
```

### Image generation

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "A cat floating in outer space",
    "n": 1,
    "size": "1792x1024",
    "response_format": "url"
  }'
```

### Image edit

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-imagine-image-edit",
    "prompt": "Change the background to a cyberpunk city at night",
    "image": "https://example.com/cat.png",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'
```

`image` also accepts data URLs or multipart uploads.

### Video generation

```bash
curl http://localhost:8000/v1/videos \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -F "model=grok-imagine-video" \
  -F "prompt=Neon rainy night street, cinematic slow-motion tracking shot" \
  -F "seconds=10" \
  -F "size=1792x1024" \
  -F "resolution_name=720p" \
  -F "preset=normal"
```

Poll the job:

```bash
curl http://localhost:8000/v1/videos/$VIDEO_ID \
  -H "Authorization: Bearer $GROK2API_API_KEY"
```

For additional upstream field notes see [chenyme/grok2api](https://github.com/chenyme/grok2api); this fork’s source of truth is the local implementation and `GET /v1/models`.

<br>

## Anti-block Stack (WARP + FlareSolverr)

When egress IPs are unclean or Cloudflare challenges are frequent:

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

| Service | Role |
| :-- | :-- |
| `init-config` | Seeds `data/config.toml`: egress via Privoxy, clearance via FlareSolverr |
| `warp-proxy` | Cloudflare WARP egress |
| `privoxy` | HTTP proxy in front of WARP (`127.0.0.1:40080` on the host) |
| `flaresolverr` | Auto-solves CF challenges for `proxy.clearance.mode=flaresolverr` |
| `grok2api` | Main app |

Effective config written by init:

```toml
[proxy.egress]
mode = "single_proxy"
proxy_url = "http://privoxy:8118"
resource_proxy_url = "http://privoxy:8118"

[proxy.clearance]
mode = "flaresolverr"
flaresolverr_url = "http://flaresolverr:8191"
```

If `data/config.toml` already exists without `privoxy`, `init-config` rewrites the proxy sections; if `privoxy` is already present, it skips.

The main `docker-compose.yml` also contains optional commented WARP / FlareSolverr blocks for partial setups.

<br>

## Utility Scripts

Run from the repo root after `uv sync`:

| Script | Purpose |
| :-- | :-- |
| `scripts/sso_to_oidc.py` | SSO → OIDC (Device Flow) into `data/oidc_auth.json` |
| `scripts/oidc_to_auth_array.py` | `oidc_auth.json` → grok CLI-style auth array |
| `scripts/oidc_to_sub2api.py` | `oidc_auth.json` → Sub2API import JSON |
| `scripts/init_proxy_config.py` | Writes proxy config for the anti-block compose stack |

Examples:

```bash
uv run python scripts/sso_to_oidc.py --from-db --limit 10 --workers 2
uv run python scripts/sso_to_oidc.py --sso-file ./sso.txt --workers 4
uv run python scripts/oidc_to_auth_array.py
```

<br>

## FAQ

**Q: `/admin/login` is unreachable after the container starts.**  
Check the port mapping with `docker compose ps` (expect `0.0.0.0:8000->8000/tcp`) and verify your host firewall allows it.

**Q: Image / video URLs return 403.**  
`app.app_url` is missing or wrong. It must be a fully qualified URL that clients can reach (e.g. `https://api.example.com`). For local proxy formats, also check `features.image_format` / `video_format` and the cache directory.

**Q: Cloudflare keeps blocking requests.**  
In Admin → Config → Proxy, switch `proxy.clearance.mode` to `manual` with matching `cf_cookies` + `user_agent`, or deploy FlareSolverr and use `flaresolverr` mode. Or start the [anti-block stack](#anti-block-stack-warp--flaresolverr).

**Q: `grok-4.5` fails with OIDC / auth errors.**  
Ensure SSO→OIDC completed (auto on import, Admin batch convert, or `scripts/sso_to_oidc.py`). Check `data/oidc_auth.json`. On rate limits, lower `features.auto_oidc_workers` and raise `auto_oidc_batch_delay_sec`.

**Q: Multi-worker deployment.**  
When `SERVER_WORKERS > 1`, the account refresh scheduler elects a single leader via a file lock; other workers only run lightweight syncing. On Windows, single-worker mode is recommended.

**Q: Which account storage backend should I use?**  
Single-node: `local` (SQLite). Shared pools across instances: `redis` / `mysql` / `postgresql` with the matching DSN.

**Q: How do I health-check the service?**  

```bash
curl http://localhost:8000/health
```

<br>

## Project Layout

```text
app/
  control/      # Account / model / proxy control plane
  dataplane/    # Selection, reverse proxy, transport, protocols
  platform/     # Config, auth, logging, storage
  products/     # OpenAI / Anthropic / Web product entrypoints
  statics/      # Admin + WebUI static assets
config.defaults.toml    # Runtime config defaults
docker-compose.yml      # Standard deploy
docker-compose.warp.yml # Anti-block stack
scripts/                # OIDC / proxy helpers
tests/                  # Unit and regression tests
```

Development helpers:

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```

<br>

## Credits

- Upstream: [chenyme/grok2api](https://github.com/chenyme/grok2api)
- DeepWiki: [chenyme/grok2api](https://deepwiki.com/chenyme/grok2api)
- Project blog: [blog.cheny.me](https://blog.cheny.me/blog/posts/grok2api)

<br>

## License

[MIT](../LICENSE)
