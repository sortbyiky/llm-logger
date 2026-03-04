# LLM Logger

企业级 LLM 请求监控系统，专为 **litellm2api + Antigravity-Manager** 链路设计。

实时捕获 Cursor 发出的每一条请求，记录完整链路（原始请求 → 预处理 → 响应），提供可视化 WebUI 面板。

![screenshot](https://raw.githubusercontent.com/sortbyiky/llm-logger/main/docs/screenshot.png)

---

## 功能特性

- **完整链路记录**：raw_request（Cursor原始消息）→ pre_call → success/failure
- **实时 WebSocket 推送**：新请求到达立即显示，无需刷新
- **SQLite 持久化**：WAL 模式 + 4个索引，重启数据不丢失，自动清理7天前数据
- **全中文 WebUI**：深色主题，企业级设计
- **登录认证**：SHA256 密码，支持修改，token 有效期7天
- **请求链路弹窗**：点击任意条目，查看该请求所有阶段的甘特图时间轴
- **Markdown 渲染**：响应内容自动渲染，`<think>` 思维链折叠展示
- **流式/同步标注**：自动识别 stream 类型
- **统计图表**：时间线、模型分布、响应耗时、今日 vs 昨日对比
- **日志标注**：右键标注重要请求，支持备注和过滤
- **多种导出**：JSON / CSV 导出当前过滤结果
- **快捷键**：`/` 聚焦搜索，`Space` 暂停/恢复，`Esc` 关闭弹窗

---

## 架构说明

```
Cursor IDE
    │  HTTP 请求
    ▼
litellm2api (Port 3001)          ← 自定义镜像，内置 custom_logger 回调
    │  回调上报
    ▼
LLM Logger (Port 8081)           ← 本项目，接收并存储日志
    │  转发
    ▼
Antigravity-Manager (Port 8045)  ← 反重力管理器，实际调用 Claude API
    │
    ▼
Claude API
```

---

## 快速部署

### 前置条件

- Docker + Docker Compose
- 已部署 [litellm2api](https://github.com/sortbyiky/litellm2api)
- 已部署 [Antigravity-Manager](https://github.com/sortbyiky/Antigravity-Manager)

### 1. 克隆项目

```bash
git clone https://github.com/sortbyiky/llm-logger.git
cd llm-logger
```

### 2. 启动服务

```bash
# 创建 logs 目录（持久化数据）
mkdir -p logs

# 构建并启动
docker compose up -d --build
```

### 3. 访问 WebUI

```
http://your-server-ip:8081
默认密码：183193
```

---

## 完整部署（含 litellm2api + Antigravity-Manager）

### 一、部署 Antigravity-Manager

```bash
git clone https://github.com/sortbyiky/Antigravity-Manager.git
cd Antigravity-Manager/docker
docker compose up -d
```

**配置说明（`docker/docker-compose.yml` environment）：**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `API_KEY` | Antigravity 访问密钥 | 随机生成（启动日志可查） |
| `ABV_BIND_LOCAL_ONLY` | 仅绑定本地 127.0.0.1 | `true` |
| `THINKING_BUDGET_MODE` | 思考预算模式 | `disabled` |
| `LOG_LEVEL` | 日志级别 | `info` |

**数据目录：** `~/.antigravity_tools/`（自动创建）

**端口：** 使用 `network_mode: host`，监听 `127.0.0.1:8045`

**gui_config.json 示例（`~/.antigravity_tools/gui_config.json`）：**

```json
{
  "custom_mapping": {
    "claude-opus-4-6": "claude-opus-4-6"
  },
  "thinking_budget": {
    "mode": "auto",
    "custom_value": 24576,
    "effort": "medium"
  }
}
```

> ⚠️ `custom_mapping` 中不要添加 Kiro 相关条目（`Kiro-Sonnet-4-6`、`Kiro-Opus-4-6`），会导致路由错误。

---

### 二、部署 litellm2api

```bash
git clone https://github.com/sortbyiky/litellm2api.git
cd litellm2api
# 使用本项目提供的配置文件（见下方）
docker compose up -d
```

**`docker-compose.yml` 关键配置：**

```yaml
services:
  litellm:
    image: ghcr.io/sortbyiky/litellm2api:latest
    ports:
      - "3001:4000"
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./logs:/app/logs
      - ./custom_logger.py:/app/custom_logger.py
      - ./litellm_logging.py:/app/litellm/litellm_core_utils/litellm_logging.py
    environment:
      LITELLM_MASTER_KEY: 'your-master-key'
      UI_USERNAME: admin
      UI_PASSWORD: 'your-password'
      LITELLM_DROP_PARAMS: 'true'
      LITELLM_MODIFY_PARAMS: 'true'
      DEFAULT_ANTHROPIC_CHAT_MAX_TOKENS: '65535'
```

**`config.yaml` 关键配置：**

```yaml
general_settings:
  master_key: 'your-master-key'

litellm_settings:
  drop_params: true
  modify_params: true
  callbacks: ["custom_logger.customHandler"]
  input_callback: ["custom_logger.customHandler"]

model_list:
  - model_name: claude-sonnet-4-6
    litellm_params:
      api_base: http://172.17.0.1:8045   # Antigravity-Manager 地址
      api_key: sk-antigravity-2024
      model: anthropic/claude-sonnet-4-6
      merge_reasoning_content_in_choices: true  # 必须保留，用于 think 标签解析
```

> ⚠️ **`merge_reasoning_content_in_choices: true` 必须保留**，否则思维链内容无法正确解析。

**`custom_logger.py` 配置（挂载进容器）：**

在文件顶部配置 LLM Logger 地址：

```python
LLM_LOGGER_URL = "http://172.17.0.1:8081"   # 宿主机 IP + LLM Logger 端口
```

> 💡 容器内访问宿主机服务需使用 `172.17.0.1`（Docker 默认网桥 IP），不能使用 `localhost`。

---

### 三、部署 LLM Logger（本项目）

```bash
git clone https://github.com/sortbyiky/llm-logger.git
cd llm-logger
mkdir -p logs
docker compose up -d --build
```

**docker-compose.yml 说明：**

```yaml
services:
  llm-logger:
    build: ./logger_server
    container_name: llm-logger
    ports:
      - "8081:8080"
    volumes:
      - ./logs:/app/logs                       # 数据持久化目录
      - ./logger_server/static:/app/static     # 前端文件（修改后 restart 即生效）
      - ./logger_server/main.py:/app/main.py   # 后端文件（修改后 restart 即生效）
    networks:
      - litellm_default   # 与 litellm2api 同网络

networks:
  litellm_default:
    external: true    # litellm2api 创建的网络，需先启动 litellm2api
```

> ⚠️ `litellm_default` 网络由 litellm2api 的 docker compose 创建，需先启动 litellm2api，再启动 llm-logger。
>
> 若独立部署，可将网络改为自建：
> ```yaml
> networks:
>   litellm_default:
>     driver: bridge
> ```

---

## 环境变量

LLM Logger 本身不需要环境变量，所有配置通过 WebUI 完成。

| 配置项 | 位置 | 说明 |
|--------|------|------|
| 登录密码 | WebUI 右上角修改 | 默认 `183193`，SHA256 存储于 `logs/config.json` |
| 数据保留天数 | 自动清理 | 默认 7 天，修改 `main.py` 中 `CLEANUP_DAYS` |

---

## API 文档

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/log` | 写入日志事件（供 custom_logger 调用） |
| `GET` | `/logs` | 分页查询，支持多维过滤 |
| `GET` | `/logs/{request_id}` | 查询单个请求的完整链路 |
| `GET` | `/stats` | 统计数据（成功率、延迟、Token、RPM） |
| `GET` | `/stats/models` | 按模型统计 |
| `GET` | `/stats/timeline` | 时间线数据（图表用） |
| `GET` | `/stats/daily` | 今日 vs 昨日对比 |
| `GET` | `/models` | 所有出现过的模型列表 |
| `GET` | `/health` | 健康检查 |
| `DELETE` | `/logs` | 清理旧日志（支持 `before_date` 参数） |
| `WS` | `/ws` | WebSocket 实时推送 |
| `POST` | `/auth/login` | 登录，返回 token |
| `POST` | `/auth/change-password` | 修改密码 |
| `GET` | `/auth/verify` | 验证 token |
| `POST/GET/DELETE` | `/notes` | 日志标注 CRUD |

所有 API（`/health` 和 `/auth/*` 除外）需在 header 中携带：
```
X-Auth-Token: your-token
```

---

## WebUI 使用说明

### 主界面

- **顶部统计卡片**：总请求数、成功率、失败数、平均延迟、总 Token、每分钟请求数（1小时平均）
- **左侧过滤栏**：按事件类型、模型、状态、请求ID、关键词、时间范围过滤
- **主日志列表**：实时滚动，点击任意行打开详情弹窗

### 详情弹窗（点击任意行打开）

| Tab | 内容 |
|-----|------|
| 链路 | 甘特图时间轴 + Token 用量 + 各阶段详情 |
| 对话视图 | 聊天气泡风格展示消息历史 |
| 请求详情 | 按角色（user/assistant/system）格式化展示，Markdown 渲染 |
| 原始请求 | 完整原始 JSON，语法高亮 |
| 响应详情 | 模型回复内容，Markdown 渲染，`<think>` 思维链折叠 |
| 完整JSON | 该事件的原始数据 |

### 键盘快捷键

| 快捷键 | 功能 |
|--------|------|
| `/` | 聚焦搜索框 |
| `Space` | 暂停/恢复实时推送 |
| `Esc` | 关闭弹窗 |

### 右键菜单

右键点击任意行，可以：复制请求ID、复制消息内容、过滤同模型、过滤同请求ID、标注日志、对比两条请求。

---

## 事件类型说明

| 事件 | 说明 | 来源 |
|------|------|------|
| `raw_request` | Cursor 发出的原始请求（含完整 messages） | custom_logger pre_call |
| `pre_call` | LiteLLM 预处理后的请求 | custom_logger pre_call |
| `success` | 请求成功，含响应体和 Token 用量 | custom_logger success |
| `failure` | 请求失败，含错误信息 | custom_logger failure |
| `chunk` | 流式响应片段（仅流式请求） | custom_logger chunk |

---

## 数据存储

- **数据库**：`logs/llm_logger.db`（SQLite，WAL 模式）
- **密码配置**：`logs/config.json`
- **自动清理**：每6小时清理一次7天前的数据

> ⚠️ `logs/` 目录通过 volume 挂载，容器重启数据不丢失。请定期备份此目录。

---

## 常见问题

**Q：WebUI 修改后不生效？**

A：由于 `static/` 目录已通过 volume 挂载，修改文件后只需：
```bash
docker compose restart llm-logger
```
不需要 rebuild。

**Q：页面显示为空，没有日志？**

A：检查 `custom_logger.py` 中的 `LLM_LOGGER_URL` 是否指向正确地址，以及 litellm2api 容器是否与 llm-logger 在同一 Docker 网络中。

**Q：容器内报 "Connection refused" 访问宿主机服务？**

A：容器内访问宿主机需使用 `172.17.0.1`（Docker 默认网桥），不能用 `localhost` 或 `127.0.0.1`。

**Q：时间显示不对？**

A：WebUI 时间已固定为上海时区（Asia/Shanghai）显示，与服务器时区无关。

---

## License

MIT
