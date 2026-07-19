<img alt="Grok2API" src="https://github.com/user-attachments/assets/037a0a6e-7986-41cc-b4af-04df612ee886" />

[![Python](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Version](https://img.shields.io/badge/version-2.0.4.rc4-111827)](./pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-16a34a)](./LICENSE)
[![Docker](https://img.shields.io/badge/docker-local%20build-2496ED?logo=docker&logoColor=white)](./Dockerfile)
[![English](https://img.shields.io/badge/English-2563EB?logo=bookstack&logoColor=white)](./docs/README.en.md)

> [!NOTE]
> 本项目仅供学习与研究使用。使用前请遵守 Grok 服务条款及当地法律法规，禁止用于任何违法用途。Fork 与 PR 请保留原作者与前端署名。

<br>

**Grok2API** 是基于 **FastAPI** 的 Grok 网关，将 Grok Web 能力以 OpenAI / Anthropic 兼容 API 对外提供。主要特性：

- OpenAI 兼容接口：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/images/generations`、`/v1/images/edits`、`/v1/videos`、`/v1/videos/{video_id}`、`/v1/videos/{video_id}/content`
- Anthropic 兼容接口：`/v1/messages`
- 流式 / 非流式对话、显式思考过程输出、Function Tools 透传、统一 token / usage 统计
- 多账号池、分层选号、失败反馈、配额同步与自动维护
- 本地图片 / 视频缓存与反代 URL
- 文生图、图编辑、文生视频、图生视频
- 内置管理后台、Web Chat、瀑布流图库、ChatKit 语音页
- 支持 `console.x.ai` 免费账号，以及 `*-console` / CLI（如 `grok-4.5`）模型族

<br>

## 目录

- [镜像信息](#镜像信息)
- [快速开始](#快速开始)
- [升级与回滚](#升级与回滚)
- [反向代理（Nginx 示例）](#反向代理nginx-示例)
- [WebUI](#webui)
- [账号管理](#账号管理)
- [运行时配置](#运行时配置)
- [环境变量](#环境变量)
- [模型列表](#模型列表)
- [API 一览](#api-一览)
- [调用示例](#调用示例)
- [防封部署（WARP + FlareSolverr）](#防封部署warp--flaresolverr)
- [实用脚本](#实用脚本)
- [常见问题](#常见问题)
- [项目结构](#项目结构)
- [致谢](#致谢)
- [许可证](#许可证)

<br>

## 镜像信息

本仓库基于 [chenyme/grok2api](https://github.com/chenyme/grok2api)。**当前无公开远程镜像，部署默认本地构建。**

| 字段 | 值 |
| :-- | :-- |
| 本地镜像标签 | `grok2api:local`（compose 构建产物） |
| 架构 | 随本机构建（CI 配置为 `linux/amd64`、`linux/arm64`） |
| 基础镜像 | `python:3.13-alpine` |
| 默认端口 | `8000` |
| 数据目录 | `/app/data` |
| 日志目录 | `/app/logs` |

> `docker-compose.yml` 已设置 `build: .` + `pull_policy: build`，不会去 GHCR 拉取。

<br>

## 快速开始

### 方式一：Docker Compose（推荐，本地 build）

```bash
git clone https://github.com/huslx/grok2api
cd grok2api
cp .env.example .env

# 首次与升级代码后均从本地 Dockerfile 构建
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f grok2api
```

防封版（WARP + FlareSolverr，见 [防封部署](#防封部署warp--flaresolverr)）：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

### 方式二：纯 Docker（本地 build）

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

Windows PowerShell：

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

### 方式三：源码运行

依赖：Python 3.13+ 与 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
git clone https://github.com/huslx/grok2api
cd grok2api
cp .env.example .env
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

### 首次配置

服务启动后访问 `http://localhost:8000/admin/login`，默认密码为 `grok2api`。建议依次：

1. 修改 `app.app_key`（管理后台密码）
2. 设置 `app.api_key`（API 鉴权密钥；留空则关闭鉴权）
3. 设置 `app.app_url`（对外可访问的根地址；否则图片 / 视频链接可能 403）
4. 在 **账号管理** 中导入账号（见 [账号管理](#账号管理)）

> 运行时配置会持久化到 `${DATA_DIR}/config.toml`，保存后立即生效，无需重启容器。  
> 默认值来源见仓库根目录 `config.defaults.toml`。

<br>

## 升级与回滚

```bash
# 拉取代码后重新本地构建并重启（数据卷 ./data ./logs 保留）
git pull
docker compose up -d --build

# 防封版：仅重建应用服务
docker compose -f docker-compose.warp.yml up -d --build --no-deps grok2api
```

回滚：检出历史 commit 后再 `docker compose up -d --build`。

<br>

## 反向代理（Nginx 示例）

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

        # 流式输出必需
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

配置反向代理后，请在管理后台将 `app.app_url` 设为 `https://your.domain.com`。

<br>

## WebUI

| 页面 | 路径 |
| :-- | :-- |
| 管理登录 | `/admin/login` |
| 账号管理 | `/admin/account` |
| 配置管理 | `/admin/config` |
| 缓存管理 | `/admin/cache` |
| WebUI 登录 | `/webui/login` |
| Web 对话 | `/webui/chat` |
| 瀑布流图库 | `/webui/masonry` |
| ChatKit 语音 | `/webui/chatkit` |

### 鉴权规则

| 范围 | 配置项 | 规则 |
| :-- | :-- | :-- |
| `/v1/*` | `app.api_key` | 为空时不鉴权 |
| `/admin/*` | `app.app_key` | 默认 `grok2api` |
| `/webui/*` | `app.webui_enabled`、`app.webui_key` | 默认关闭；`webui_key` 为空时不再额外校验 |

<br>

## 账号管理

### 账号类型

| 类型 | 凭证 | 典型模型 | 说明 |
| :-- | :-- | :-- | :-- |
| **付费账号** | grok.com / x.ai 的 `sso` Cookie | `grok-4.20-*`、`grok-4.3-beta`、Imagine 等 | 走官方 Web / 付费额度 |
| **免费 Console** | `sso` + 可选 CF Clearance | `*-console`、`grok-4.3-{low,medium,high}` | 经 `console.x.ai` 路由 |
| **CLI（grok-4.5）** | 同一 `sso`，运行时换 OIDC | `grok-4.5`、`grok-4.5-console` | 需 SSO→OIDC（可自动） |

后台路径：**管理后台 → 账号**。批量粘贴 token 时支持 `sso=` 前缀，导入后会自动清洗。

### 付费账号接入

1. 登录已开通对应模型的 x.ai / grok.com 账号
2. 打开开发者工具（F12）→ Network，复制任意请求 Cookie 中的 **`sso`** 值
3. 在 **账号管理 → 添加账号** 中粘贴 SSO Token
4. 若出口 IP 常被 Cloudflare 拦截，再按 [代理与 Clearance](#代理与-clearance) 配置 `proxy.clearance`

> 付费模型与账号档位（basic / super / heavy）相关；无额度或档位不足时请求会失败或被换号。

### 免费 Console 账号接入

使用 Console 模型需同时准备 **SSO Token** 与 **CF Clearance**（视网络环境而定）：

1. 打开浏览器开发者工具（F12）
2. 访问 `https://console.x.ai/`
3. 在 Network 中查看任意请求的 Cookie，复制：
   - `sso` 的值
   - `cf_clearance` 的值（若存在）
4. 在管理后台 → 账号 → 添加账号，填入 SSO；CF 相关项在 **配置 → 代理 / Clearance** 中统一管理更常见（`manual` / `flaresolverr`）

### CLI / grok-4.5（OIDC）

`grok-4.5` 走 CLI 通道，需要 OIDC `access_token`（由 SSO 经 Device Flow 换取）。

默认行为（`config.defaults.toml`）：

| 配置项 | 默认 | 含义 |
| :-- | :-- | :-- |
| `features.auto_oidc_on_import` | `true` | 账号导入 / 注册后自动排队 SSO→OIDC |
| `features.auto_oidc_workers` | `8` | 批量转换并发 |
| `chat.cli_reasoning_effort` | `medium` | CLI 默认思考档位（可被请求覆盖） |
| `chat.cli_account_retries` | `8` | OIDC 未就绪时换号次数下限 |

也可手动转换：

- **管理后台**：账号页触发 OIDC 转换（API：`POST /admin/api/tokens/oidc-convert`）
- **命令行**（见 [实用脚本](#实用脚本)）：

```bash
# 从本地 SQLite 账号库批量转换（示例：前 10 个，2 并发）
uv run python scripts/sso_to_oidc.py --from-db --limit 10 --workers 2
```

运行时 OIDC 缓存默认写在 `${DATA_DIR}/oidc_auth.json`。

> SSO、CF Clearance、OIDC token 均为敏感凭证，切勿提交到代码仓库。

<br>

## 运行时配置

运行时配置文件：`${DATA_DIR}/config.toml`（默认 `./data/config.toml`）。  
首次启动会基于 `config.defaults.toml` 生成；也可在 **管理后台 → 配置** 修改，保存后立即生效。

还可通过环境变量覆盖，格式为 `GROK_<SECTION>_<KEY>`（大写），例如：

- `GROK_APP_API_KEY` → `app.api_key`
- `GROK_FEATURES_STREAM` → `features.stream`
- `GROK_PROXY_CLEARANCE` 不适用嵌套过深的键；**复杂嵌套项请直接改 config.toml 或后台**

### 访问控制 `[app]`

| 键 | 默认 | 说明 |
| :-- | :-- | :-- |
| `app_key` | `grok2api` | 管理后台密码 |
| `api_key` | `""` | API Bearer；空则 `/v1/*` 不鉴权 |
| `app_url` | `""` | 对外根 URL（图片 / 视频本地反代必需） |
| `webui_enabled` | `false` | 是否启用 WebUI |
| `webui_key` | `""` | WebUI 密码；空则不额外校验 |

### 功能开关 `[features]`

| 键 | 默认 | 说明 |
| :-- | :-- | :-- |
| `stream` | `true` | 默认是否流式（请求可覆盖） |
| `thinking` | `true` | 是否输出思考过程 |
| `thinking_summary` | `false` | `true` 时输出精简摘要而非完整推理 |
| `memory` | `false` | 会话记忆 |
| `temporary` | `true` | 临时对话 |
| `auto_chat_mode_fallback` | `true` | AUTO 额度耗尽时降级到 fast/expert |
| `image_format` | `grok_url` | 图片返回：`grok_url` / `local_url` / `grok_md` / `local_md` / `base64` |
| `imagine_public_image_proxy` | `false` | 将 WebSocket 返回的 imagine-public 图片下载并本地代理 |
| `video_format` | `grok_url` | 视频返回：`grok_url` / `local_url` / `grok_html` / `local_html` |
| `imagine_public_video_proxy` | `false` | 将上游返回的 imagine-public 视频下载并本地代理 |
| `enable_nsfw` | `true` | 是否允许 NSFW 图片相关能力 |
| `auto_oidc_on_import` | `true` | 导入账号后自动 SSO→OIDC |
| `show_search_sources` | `false` | 是否在正文末追加 `## Sources` 文本 |

### 代理与 Clearance

`[proxy.egress]`：

| 键 | 默认 | 说明 |
| :-- | :-- | :-- |
| `mode` | `direct` | `direct` / `single_proxy` / `proxy_pool` |
| `proxy_url` | `""` | 单代理 URL（API 流量） |
| `proxy_pool` | `[]` | 代理池 |
| `resource_proxy_url` | `""` | 图片 / 视频下载代理；空则回落 `proxy_url` |
| `skip_ssl_verify` | `false` | 跳过代理 SSL 校验 |

`[proxy.clearance]`：

| 键 | 默认 | 说明 |
| :-- | :-- | :-- |
| `mode` | `none` | `none` / `manual` / `flaresolverr` |
| `cf_cookies` | `""` | manual：完整 Cookie 字符串 |
| `user_agent` | Chrome 136 UA | 需与 Cookie 匹配 |
| `flaresolverr_url` | `""` | FlareSolverr 地址 |
| `refresh_interval` | `3600` | Clearance 刷新间隔（秒） |

### 重试与账号调度

| 键 | 默认 | 说明 |
| :-- | :-- | :-- |
| `retry.max_retries` | `1` | 换账号最大次数（0 = 不重试） |
| `retry.on_codes` | `429,401,503` | 触发换号的 HTTP 状态码 |
| `account.refresh.enabled` | `true` | `true`=配额刷新评分选号；`false`=随机选号 |
| `account.selection.max_inflight` | `8` | 单号并发上限 |

完整默认值见 [`config.defaults.toml`](./config.defaults.toml)。

<br>

## 环境变量

启动期变量（`.env` / Compose / `docker run -e`）。完整模板见 [`.env.example`](./.env.example)。

| 名称 | 说明 | 默认值 |
| :-- | :-- | :-- |
| `TZ` | 时区 | `Asia/Shanghai` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `LOG_FILE_ENABLED` | 是否写文件日志 | `true` |
| `ACCOUNT_SYNC_INTERVAL` | 账号目录同步间隔（秒） | `30` |
| `ACCOUNT_SYNC_ACTIVE_INTERVAL` | 变更后的活跃同步间隔（秒） | `3` |
| `SERVER_HOST` | 监听地址 | `0.0.0.0` |
| `SERVER_PORT` | 监听端口 | `8000` |
| `SERVER_WORKERS` | Granian worker 数 | `1` |
| `HOST_PORT` | Compose 宿主机映射端口 | `8000` |
| `DATA_DIR` | 数据根目录 | `./data` |
| `LOG_DIR` | 日志目录 | `./logs` |
| `ACCOUNT_STORAGE` | 存储后端：`local` / `redis` / `mysql` / `postgresql` | `local` |
| `ACCOUNT_LOCAL_PATH` | `local` 模式 SQLite 路径 | `${DATA_DIR}/accounts.db` |
| `ACCOUNT_REDIS_URL` | `redis` 模式 DSN | `""` |
| `ACCOUNT_MYSQL_URL` | `mysql` 模式 DSN | `""` |
| `ACCOUNT_POSTGRESQL_URL` | `postgresql` 模式 DSN | `""` |
| `ACCOUNT_SQL_POOL_SIZE` | SQL 连接池核心大小 | `5` |
| `ACCOUNT_SQL_MAX_OVERFLOW` | SQL 连接池最大溢出 | `10` |
| `ACCOUNT_SQL_POOL_TIMEOUT` | 取连接超时（秒） | `30` |
| `ACCOUNT_SQL_POOL_RECYCLE` | 连接回收时间（秒） | `1800` |
| `CONFIG_LOCAL_PATH` | 运行时配置文件路径 | `${DATA_DIR}/config.toml` |

<br>

## 模型列表

> 以 `GET /v1/models` 的实时列表为准。

### 对话（付费）

| 模型 | mode | tier |
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
| `grok-4.20-fast` | `fast` | `basic`，优先高档位账号 |
| `grok-4.3-fast` | `fast` | `basic`，优先高档位账号 |
| `grok-4.20-auto` | `auto` | `super`，优先高档位账号 |
| `grok-4.20-expert` | `expert` | `super`，优先高档位账号 |
| `grok-4.20-heavy` | `heavy` | `heavy` |
| `grok-4.3-beta` | `grok-420-computer-use-sa` | `super` |

### 对话（console.x.ai / CLI 免费向）

| 模型 | 说明 |
| :-- | :-- |
| `grok-4.3-console` | Console 默认 |
| `grok-4.3-low` | 低思考 |
| `grok-4.3-medium` | 中思考 |
| `grok-4.3-high` | 高思考 |
| `grok-4.5` / `grok-4.5-console` | CLI（OIDC） |
| `grok-4.20-0309-console` | Console |
| `grok-4.20-0309-non-reasoning-console` | Console 非推理 |
| `grok-4.20-0309-reasoning-console` | Console 固定推理 |
| `grok-4.20-multi-agent-console` | Console 多智能体 |
| `grok-4.20-multi-agent-low` / `medium` / `high` / `xhigh` | Console 多智能体思考档位 |
| `grok-build-console` | Grok Build 0.1 |

### 图片 / 图编辑 / 视频

| 模型 | mode | tier |
| :-- | :-- | :-- |
| `grok-imagine-image-lite` | `fast` | `basic` |
| `grok-imagine-image` | `auto` | `super` |
| `grok-imagine-image-pro` | `auto` | `super` |
| `grok-imagine-image-edit` | `auto` | `super` |
| `grok-imagine-video` | `auto` | `super` |

<br>

## API 一览

| 接口 | 鉴权 | 说明 |
| :-- | :-- | :-- |
| `GET /v1/models` | 是 | 列出可用模型 |
| `GET /v1/models/{model_id}` | 是 | 查询单个模型 |
| `POST /v1/chat/completions` | 是 | 统一对话 / 图片 / 视频入口 |
| `POST /v1/responses` | 是 | OpenAI Responses API 子集 |
| `POST /v1/messages` | 是 | Anthropic Messages API |
| `POST /v1/images/generations` | 是 | 独立文生图 |
| `POST /v1/images/edits` | 是 | 独立图编辑 |
| `POST /v1/videos` | 是 | 异步创建视频任务 |
| `GET /v1/videos/{video_id}` | 是 | 查询视频任务 |
| `GET /v1/videos/{video_id}/content` | 是 | 下载最终视频 |
| `GET /v1/files/video?id=...` | 否 | 本地缓存视频 |
| `GET /v1/files/image?id=...` | 否 | 本地缓存图片 |
| `GET /health` | 否 | 健康检查 |

`POST /v1/chat/completions` 常用字段：

| 字段 | 说明 |
| :-- | :-- |
| `model` | 模型名 |
| `messages` | OpenAI 风格消息列表 |
| `stream` | 是否流式；省略时用 `features.stream` |
| `reasoning_effort` | 思考档位（如 `low` / `medium` / `high`；CLI 有效） |
| `tools` / `tool_choice` | Function tools 透传 |
| `image_config` | 当 model 为文生图 / 图编辑时的 `n` / `size` / `response_format` |
| `video_config` | 当 model 为视频时的 `seconds` / `size` / `resolution_name` / `preset` |

<br>

## 调用示例

以下示例中 `$GROK2API_API_KEY` 为你配置的 `app.api_key`（若为空可去掉 `Authorization` 头）。

### 付费账号对话

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "stream": true,
    "reasoning_effort": "high",
    "messages": [
      {"role":"user","content":"你好"}
    ]
  }'
```

### 免费 Console 对话

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.3-high",
    "stream": true,
    "messages": [
      {"role":"user","content":"你好"}
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
      {"role":"user","content":"用三句话解释什么是 API 网关"}
    ]
  }'
```

### Function Tools

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-4.20-auto",
    "messages": [
      {"role":"user","content":"查询北京天气"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "获取城市天气",
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
      {"role":"user","content":"你好"}
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
    "input": "用一句话介绍你自己",
    "stream": false
  }'
```

### 文生图

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "一只漂浮在太空中的猫",
    "n": 1,
    "size": "1792x1024",
    "response_format": "url"
  }'
```

### 图编辑

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -d '{
    "model": "grok-imagine-image-edit",
    "prompt": "把背景换成赛博朋克城市夜景",
    "image": "https://example.com/cat.png",
    "n": 1,
    "size": "1024x1024",
    "response_format": "url"
  }'
```

`image` 也支持 data URL 或本地上传字段（`multipart`）。

### 文生视频

```bash
curl http://localhost:8000/v1/videos \
  -H "Authorization: Bearer $GROK2API_API_KEY" \
  -F "model=grok-imagine-video" \
  -F "prompt=霓虹雨夜街道，电影感慢动作跟拍" \
  -F "seconds=10" \
  -F "size=1792x1024" \
  -F "resolution_name=720p" \
  -F "preset=normal"
```

创建后轮询：

```bash
curl http://localhost:8000/v1/videos/$VIDEO_ID \
  -H "Authorization: Bearer $GROK2API_API_KEY"
```

更多上游字段说明可参考 [chenyme/grok2api](https://github.com/chenyme/grok2api)；本仓库以本地实现与 `GET /v1/models` 为准。

<br>

## 防封部署（WARP + FlareSolverr）

当出口 IP 不干净、Cloudflare 频繁拦截时，使用 `docker-compose.warp.yml`：

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

| 服务 | 作用 |
| :-- | :-- |
| `init-config` | 启动前写入 `data/config.toml`：出口代理指向 Privoxy，Clearance 模式设为 FlareSolverr |
| `warp-proxy` | Cloudflare WARP 出口 |
| `privoxy` | 将 HTTP 代理转到 WARP（宿主机 `127.0.0.1:40080`） |
| `flaresolverr` | 自动解 CF 挑战，供 `proxy.clearance.mode=flaresolverr` 使用 |
| `grok2api` | 主服务 |

初始化后的关键配置等价于：

```toml
[proxy.egress]
mode = "single_proxy"
proxy_url = "http://privoxy:8118"
resource_proxy_url = "http://privoxy:8118"

[proxy.clearance]
mode = "flaresolverr"
flaresolverr_url = "http://flaresolverr:8191"
```

若已有 `data/config.toml` 且不含 `privoxy`，`init-config` 会改写 proxy 段；已含 `privoxy` 则跳过。

主 `docker-compose.yml` 里也预留了可选的 WARP / FlareSolverr 注释块，适合只开其中一个组件时手动启用。

<br>

## 实用脚本

均在仓库根目录执行（需已 `uv sync`）：

| 脚本 | 用途 |
| :-- | :-- |
| `scripts/sso_to_oidc.py` | SSO → OIDC（Device Flow），写入 `data/oidc_auth.json` |
| `scripts/oidc_to_auth_array.py` | `oidc_auth.json` → grok CLI 风格 auth 数组 |
| `scripts/oidc_to_sub2api.py` | `oidc_auth.json` → Sub2API 导入 JSON |
| `scripts/init_proxy_config.py` | Compose 防封栈写入代理配置（容器内调用） |

示例：

```bash
# 从本地账号库转换
uv run python scripts/sso_to_oidc.py --from-db --limit 10 --workers 2

# 从 SSO 列表文件（每行一个 JWT，或 email----sso）
uv run python scripts/sso_to_oidc.py --sso-file ./sso.txt --workers 4

# 导出 CLI auth 数组
uv run python scripts/oidc_to_auth_array.py
```

<br>

## 常见问题

**Q：容器启动后打不开 `/admin/login`。**  
用 `docker compose ps` 检查端口映射是否为 `0.0.0.0:8000->8000/tcp`，并确认宿主机防火墙放行。

**Q：图片 / 视频链接返回 403。**  
`app.app_url` 未设置或设置错误。需填写客户端可访问的完整根地址（例如 `https://api.example.com`）。若使用本地反代格式，确认 `features.image_format` / `video_format` 与缓存目录正常。

**Q：Cloudflare 持续拦截。**  
在管理后台 → 配置 → 代理中，将 `proxy.clearance.mode` 改为 `manual` 并填入匹配的 `cf_cookies` + `user_agent`；或部署 FlareSolverr 后改为 `flaresolverr` 模式。也可用 [防封部署](#防封部署warp--flaresolverr) 一键拉起。

**Q：`grok-4.5` 报 OIDC / 鉴权相关错误。**  
确认账号已完成 SSO→OIDC（导入自动转换、后台批量转换，或 `scripts/sso_to_oidc.py`）。查看 `data/oidc_auth.json` 是否有对应条目；限流时可调低 `features.auto_oidc_workers` 并增大 `auto_oidc_batch_delay_sec`。

**Q：多 worker 部署。**  
当 `SERVER_WORKERS > 1` 时，账号刷新调度通过文件锁选举唯一 leader，其余 worker 只做轻量同步。Windows 建议单 worker。

**Q：账号存储后端怎么选？**  
单机默认 `local`（SQLite）即可；多实例共享账号池时使用 `redis` / `mysql` / `postgresql`，并配置对应 DSN。

**Q：如何确认服务健康？**  

```bash
curl http://localhost:8000/health
```

<br>

## 项目结构

```text
app/
  control/      # 账号 / 模型 / 代理控制面
  dataplane/    # 选号、反代、传输与协议
  platform/     # 配置、鉴权、日志、存储等基础设施
  products/     # OpenAI / Anthropic / Web 产品入口
  statics/      # 管理后台与 WebUI 静态资源
config.defaults.toml   # 运行时配置默认值
docker-compose.yml     # 标准部署
docker-compose.warp.yml# 防封栈
scripts/               # OIDC / 代理初始化等工具
tests/                 # 单元与回归测试
```

开发时可用：

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```

<br>

## 致谢

- 上游项目：[chenyme/grok2api](https://github.com/chenyme/grok2api)
- DeepWiki：[chenyme/grok2api](https://deepwiki.com/chenyme/grok2api)
- 项目博客：[blog.cheny.me](https://blog.cheny.me/blog/posts/grok2api)

<br>

## 许可证

[MIT](./LICENSE)
