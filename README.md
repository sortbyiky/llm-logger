# 🔍 LLM Logger

<div align="center">

[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](https://www.docker.com/)
[![GHCR](https://img.shields.io/badge/GHCR-ghcr.io%2Fsortbyiky%2Fllm--logger-blue)](https://ghcr.io/sortbyiky/llm-logger)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**企业级 LLM 请求实时监控系统**

实时追踪 · 成本分析 · 告警推送 · 流式聚合

[快速开始](#-快速开始) • [LiteLLM 接入](#-litellm-接入配置) • [功能特性](#-功能特性) • [配置说明](#-配置说明)

</div>

---

## ✨ 功能特性

### 📊 实时监控
- **WebSocket 实时推送** — 请求日志零延迟推送到浏览器
- **请求链路追踪** — 完整展示 `raw_request → pre_call → success/failure` 全链路
- **流式响应聚合** — 自动合并 chunk 片段，展示完整流式内容及片段数统计
- **甘特图时间轴** — 可视化每个请求各阶段耗时

### 💰 成本分析
- **自动计算 USD 成本** — 内置主流 Claude 模型价格表，支持按模型单独统计
- **Token 用量分布** — 按模型分组展示输入/输出 token 消耗
- **成本过滤** — 支持按成本区间（便宜/适中/昂贵）筛选请求

### 🔔 告警系统
- **错误率告警** — 5分钟内错误率超阈值自动触发
- **延迟告警** — 平均响应时间超阈值触发
- **大请求告警** — 单次输入 token 超阈值触发
- **日 Token 配额预警** — 当日总 token 用量超设定量触发
- **Telegram 推送** — 支持 Telegram Bot 实时通知
- **Webhook** — 支持自定义 Webhook 推送到任意系统
- **告警节流** — 同类型告警 60 分钟内只推送一次，避免轰炸

### 🔍 日志查询
- **多维过滤** — 模型、状态、请求ID、关键词、时间范围、成本区间
- **正则搜索** — 支持正则表达式搜索日志内容
- **自动分页** — 支持历史日志翻页查询
- **批量操作** — 多选导出（JSON/CSV）
- **请求标注** — 支持对请求打备注 📌
- **对比视图** — 两条请求并排对比

### 🛡️ 安全
- **密码登录** — 默认需要密码，支持修改
- **Token 认证** — 登录后颁发 7 天有效 Token，本地访问免认证
- **密码环境变量配置** — 不硬编码密码

---

## 🚀 快速开始

### 方式一：Docker Compose（推荐）

```yaml
# docker-compose.yml
services:
  llm-logger:
    image: ghcr.io/sortbyiky/llm-logger:latest
    container_name: llm-logger
    ports:
      - "8081:8080"
    volumes:
      - ./logs:/app/logs
    environment:
      - LLM_LOGGER_PASSWORD=your_password_here   # 修改为你的密码
    restart: unless-stopped
```

```bash
docker compose up -d
```

访问 `http://localhost:8081`，使用你设置的密码登录。

---

### 方式二：如果和 LiteLLM 在同一 Docker 网络

```yaml
# docker-compose.yml
services:
  llm-logger:
    image: ghcr.io/sortbyiky/llm-logger:latest
    container_name: llm-logger
    ports:
      - "8081:8080"
    volumes:
      - ./logs:/app/logs
    environment:
      - LLM_LOGGER_PASSWORD=your_password_here
    restart: unless-stopped
    networks:
      - litellm_default   # 加入 litellm 所在的网络

networks:
  litellm_default:
    external: true
```

---

### 方式三：从源码构建

```bash
git clone https://github.com/sortbyiky/llm-logger.git
cd llm-logger
docker compose up -d --build
```

---

## 🔗 LiteLLM 接入配置

LLM Logger 通过 **LiteLLM 的 Callback 机制** 接收数据，需要在 LiteLLM 配置中添加自定义 callback。

### 第一步：创建 LiteLLM Custom Callback 文件

在你的 LiteLLM 配置目录中创建 `llm_logger_callback.py`：

```python
# llm_logger_callback.py
import httpx
import json
from litellm.integrations.custom_logger import CustomLogger

LLM_LOGGER_URL = "http://llm-logger:8080"  # 同 Docker 网络时用容器名
# 如果不在同一网络，改为实际地址，例如：http://localhost:8081

class LLMLoggerCallback(CustomLogger):
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=5.0)

    async def async_log_pre_api_call(self, model, messages, kwargs):
        try:
            await self.client.post(f"{LLM_LOGGER_URL}/log", json={
                "event_type": "pre_call",
                "request_id": kwargs.get("litellm_call_id"),
                "model": model,
                "request_body": {"messages": messages, **{k: v for k, v in kwargs.items() if k in ("stream", "temperature", "max_tokens")}},
            })
        except Exception:
            pass

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        duration_ms = (end_time - start_time).total_seconds() * 1000
        usage = getattr(response_obj, "usage", None)
        try:
            await self.client.post(f"{LLM_LOGGER_URL}/log", json={
                "event_type": "success",
                "request_id": kwargs.get("litellm_call_id"),
                "model": kwargs.get("model"),
                "upstream_model": getattr(response_obj, "model", None),
                "duration_ms": duration_ms,
                "input_tokens": usage.prompt_tokens if usage else None,
                "output_tokens": usage.completion_tokens if usage else None,
                "request_body": {"messages": kwargs.get("messages"), "stream": kwargs.get("stream")},
                "response_body": response_obj.model_dump() if hasattr(response_obj, "model_dump") else None,
            })
        except Exception:
            pass

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        duration_ms = (end_time - start_time).total_seconds() * 1000
        try:
            await self.client.post(f"{LLM_LOGGER_URL}/log", json={
                "event_type": "failure",
                "request_id": kwargs.get("litellm_call_id"),
                "model": kwargs.get("model"),
                "duration_ms": duration_ms,
                "error": str(response_obj),
                "request_body": {"messages": kwargs.get("messages")},
            })
        except Exception:
            pass

proxy_handler_instance = LLMLoggerCallback()
```

### 第二步：在 LiteLLM config.yaml 中注册

```yaml
# litellm config.yaml
model_list:
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY

litellm_settings:
  callbacks:
    - /app/llm_logger_callback.py   # callback 文件路径（容器内路径）
```

### 第三步：挂载 callback 文件到 LiteLLM 容器

```yaml
# litellm docker-compose.yml 中添加 volumes
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./llm_logger_callback.py:/app/llm_logger_callback.py  # ← 挂载 callback
    environment:
      - ANTHROPIC_API_KEY=sk-ant-xxx
```

### 完整网络拓扑示意

```
Cursor / Coding Tool / 任意 OpenAI 兼容客户端
         |
         ▼
   LiteLLM Proxy (端口 4000/3001)
         |  ← async_log_* callbacks
         ▼
   LLM Logger (端口 8080/8081)    ←  浏览器 WebSocket 实时看板
         |
    SQLite 持久化
```

---

## ⚙️ 配置说明

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_LOGGER_PASSWORD` | `changeme` | 登录密码，**强烈建议修改** |

### 数据持久化

日志数据存储在容器内 `/app/logs/` 目录，建议挂载到宿主机：

```yaml
volumes:
  - ./logs:/app/logs
```

目录内容：
- `llm_logger.db` — SQLite 数据库（日志数据）
- `config.json` — 密码 Hash 和告警配置

### 告警配置

登录后点击右上角 **👤 → 告警配置**，可配置：
- Telegram Bot Token + Chat ID（如何获取 Chat ID：向 `@userinfobot` 发任意消息）
- Webhook URL
- 错误率/延迟/大请求/日 Token 配额阈值

---

## 📡 API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/log` | 接收日志（供 LiteLLM callback 调用，无需认证） |
| `GET` | `/logs` | 查询日志列表（分页、多维过滤） |
| `GET` | `/logs/{request_id}` | 查询单条请求完整链路 |
| `GET` | `/stats` | 统计数据（成功率、延迟、Token、成本） |
| `GET` | `/stats/models` | 按模型分组统计 |
| `GET` | `/stats/timeline` | 请求时间轴数据（用于图表） |
| `GET` | `/health` | 健康检查 |
| `WS` | `/ws` | WebSocket 实时推送 |

---

## 🗂️ 项目结构

```
llm-logger/
├── logger_server/
│   ├── main.py          # FastAPI 后端（全量逻辑）
│   ├── requirements.txt
│   ├── Dockerfile
│   └── static/
│       └── index.html   # 前端单页面（纯原生 JS）
├── .github/
│   └── workflows/
│       └── docker-publish.yml  # GitHub Actions 自动构建推送 GHCR
├── docker-compose.yml
└── README.md
```

---

## 🛠️ 本地开发

```bash
cd logger_server
pip install -r requirements.txt
LLM_LOGGER_PASSWORD=dev123 uvicorn main:app --reload --port 8080
```

---

## 📦 Docker 镜像

镜像自动发布到 GitHub Container Registry：

```
ghcr.io/sortbyiky/llm-logger:latest
ghcr.io/sortbyiky/llm-logger:main
```

每次推送到 `main` 分支或打 `v*.*.*` tag 时自动构建，支持 `linux/amd64` 和 `linux/arm64` 双架构。

---

## 📄 License

MIT License © 2026 sortbyiky
