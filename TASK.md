# LLM Logger V4 任务：chunk聚合 + 成本估算 + 告警系统

## 项目位置
/opt/llm-logger/

## 任务一：chunk 事件聚合

### 问题
流式请求会产生几十到几百个 chunk 事件，在日志列表里大量刷屏，严重影响可读性。

### 方案
**后端**：
- `POST /log` 接收 chunk 事件时，不单独存入（或存入但标记为 hidden=1）
- 改为累积到内存缓冲区，当收到同一 request_id 的 success 事件时，把所有 chunk 的内容合并，更新到一个 `stream_summary` 字段里
- 或者：chunk 单独存，但 GET /logs 默认不返回 chunk（加 `include_chunks=true` 参数才返回）
- 在 `success` 事件上新增字段：`chunk_count`（chunk 总数）、`stream_content`（所有 chunk 拼接的完整内容）

**前端**：
- 列表默认不显示 chunk 事件（过滤栏的 chunk 事件类型默认不勾选）
- success 事件上显示「流式 · N个片段」标注（如果 chunk_count > 0）
- 弹窗「响应详情」Tab 里显示完整流式内容（从 stream_content 字段）

## 任务二：成本估算

### 模型价格表（美元/百万token，2025年最新价格）
```python
MODEL_PRICES = {
    # Claude 系列（input_price, output_price）单位：美元/百万token
    "claude-opus-4-6":        (15.0, 75.0),
    "claude-opus-4":          (15.0, 75.0),
    "claude-opus-4-5-20251101": (15.0, 75.0),
    "claude-sonnet-4-6":      (3.0,  15.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-sonnet-4":        (3.0,  15.0),
    "claude-haiku-4":         (0.8,  4.0),
    "claude-haiku-4-5-20251001": (0.8, 4.0),
    "claude-haiku-4-6":       (0.8,  4.0),
    "claude-3-5-sonnet-20241022": (3.0, 15.0),
    "claude-3-5-sonnet-20240620": (3.0, 15.0),
    "claude-3-haiku-20240307": (0.25, 1.25),
    # 默认（未知模型）
    "default":                (3.0,  15.0),
}
```

### 后端改动
- `POST /log` 接收到 success 事件时，根据 model + input_tokens + output_tokens 计算成本，存入新字段 `cost_usd`（REAL类型）
- 数据库新增 `cost_usd` 列（ALTER TABLE logs ADD COLUMN cost_usd REAL）
- GET /stats 返回新字段：`total_cost_usd`（总成本）、`avg_cost_usd`（平均每次）
- GET /stats/models 每个模型返回 `cost_usd`

### 前端改动
- 统计卡片新增第7个：「💰 总成本」显示 $X.XX 格式
  - 0.01以下显示 $0.0001 格式（4位小数）
  - 0.01-1 显示 $0.01 格式（2位小数）
  - 1以上显示 $1.23 格式
- 列表每行右侧（token列后面）新增成本列，显示该次请求的成本（仅 success 事件显示）
- 弹窗「概览」Tab 增加成本信息：input成本 + output成本 + 合计
- 过滤栏新增「成本区间」过滤（便宜/适中/昂贵 三档）

## 任务三：告警系统

### 告警规则（后端）
在 main.py 新增后台任务，每60秒检查一次：

1. **错误率告警**：过去5分钟内，failure/(success+failure) > 20% 且请求总数 >= 3，触发告警
2. **延迟告警**：过去5分钟内，avg(duration_ms) > 30000（30秒），触发告警  
3. **大请求告警**：单次请求 input_tokens > 100000（10万token），触发告警
4. **Token 日配额预警**：今日 total_tokens > 配置的阈值，触发告警

### 告警去重
- 同类型告警60分钟内只触发一次（存 last_alert_time 到 config.json）

### 告警通道
**Telegram 推送**（priority 1）：
- 配置：`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`（存 config.json，WebUI 可配置）
- 消息格式：`🚨 LLM Logger 告警\n类型：错误率过高\n当前：35%（阈值20%）\n时间：2026-03-04 08:30`

**Webhook 推送**（priority 2，可选）：
- POST JSON 到配置的 URL

### 告警配置 WebUI
在右上角 👤 菜单新增「告警配置」选项，弹窗包含：
- Telegram Bot Token 输入框
- Telegram Chat ID 输入框（文字说明如何获取：给 @userinfobot 发消息）
- 各告警规则开关 + 阈值输入框
  - 错误率告警：开关 + 阈值%
  - 延迟告警：开关 + 阈值秒数
  - 大请求告警：开关 + 阈值Token数
  - 日Token配额：开关 + 阈值（百万Token）
- 「发送测试消息」按钮
- 保存按钮

### 后端告警 API
- `GET /alerts/config` — 获取告警配置
- `POST /alerts/config` — 保存告警配置
- `POST /alerts/test` — 发送测试消息
- `GET /alerts/history` — 最近10条告警历史
- 告警历史存 SQLite `alerts` 表

### WebUI 告警历史
导航栏新增「🔔 告警」图标，点击弹出最近告警列表（最近10条），未读告警数显示红色徽章。

## 技术要求
- 后端：Python 3.11+，FastAPI + aiosqlite，httpx（Telegram HTTP 请求）
- 数据库迁移：启动时检查字段是否存在，不存在则 ALTER TABLE 添加
- 所有新 API 需认证（/health /auth/* /alerts/test 除外）
- chunk 聚合逻辑要稳健，避免影响现有 success 事件的正常记录
- 成本计算精度：保留6位小数存储，显示时根据量级格式化

## 部署步骤（完成后执行）
1. cd /opt/llm-logger && docker compose up -d --build
2. curl http://localhost:8081/health
3. curl -s http://localhost:8081/stats | python3 -c "import sys,json; d=json.load(sys.stdin); print('total_cost_usd:', d.get('total_cost_usd'))"

完成后通知：openclaw system event --text "LLM Logger V4 完成：chunk聚合+成本估算+告警系统" --mode now
