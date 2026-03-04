"""
LLM Logger - Enterprise Grade Backend V4
新增：chunk聚合、成本估算、告警系统
"""
import asyncio
import hashlib
import json
import secrets
import uuid
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

import aiosqlite
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager

DB_PATH = Path("/app/logs/llm_logger.db")
CONFIG_PATH = Path("/app/logs/config.json")
DB_PATH.parent.mkdir(exist_ok=True)

DEFAULT_PASSWORD = os.environ.get("LLM_LOGGER_PASSWORD", "changeme")
TOKEN_VALIDITY_DAYS = 7

# ──────────────────────────────────────────────
# 成本价格表（美元/百万token）
# ──────────────────────────────────────────────
MODEL_PRICES = {
    "claude-opus-4-6":           (15.0, 75.0),
    "claude-opus-4":             (15.0, 75.0),
    "claude-opus-4-5-20251101":  (15.0, 75.0),
    "claude-sonnet-4-6":         (3.0,  15.0),
    "claude-sonnet-4-5-20250929":(3.0,  15.0),
    "claude-sonnet-4":           (3.0,  15.0),
    "claude-haiku-4":            (0.8,  4.0),
    "claude-haiku-4-5-20251001": (0.8,  4.0),
    "claude-haiku-4-6":          (0.8,  4.0),
    "claude-3-5-sonnet-20241022":(3.0,  15.0),
    "claude-3-5-sonnet-20240620":(3.0,  15.0),
    "claude-3-haiku-20240307":   (0.25, 1.25),
    "default":                   (3.0,  15.0),
}

def calc_cost(model: Optional[str], input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    if not input_tokens and not output_tokens:
        return None
    inp = input_tokens or 0
    outp = output_tokens or 0
    prices = MODEL_PRICES.get(model or "", MODEL_PRICES["default"])
    cost = (inp / 1_000_000) * prices[0] + (outp / 1_000_000) * prices[1]
    return round(cost, 6)

# ──────────────────────────────────────────────
# Chunk 缓冲区（内存）
# ──────────────────────────────────────────────
# request_id -> list of chunk_data strings
_chunk_buffer: dict = {}

# ──────────────────────────────────────────────
# Config (password hash storage)
# ──────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def ensure_config():
    cfg = load_config()
    if "password_hash" not in cfg:
        cfg["password_hash"] = sha256(DEFAULT_PASSWORD)
        cfg["tokens"] = {}
        save_config(cfg)
    if "tokens" not in cfg:
        cfg["tokens"] = {}
        save_config(cfg)
    return cfg

# ──────────────────────────────────────────────
# Auth helpers
# ──────────────────────────────────────────────
def generate_token() -> str:
    return secrets.token_hex(32)

def is_token_valid(token: str) -> bool:
    cfg = load_config()
    tokens = cfg.get("tokens", {})
    if token not in tokens:
        return False
    exp = tokens[token]
    try:
        if datetime.fromisoformat(exp) > datetime.utcnow():
            return True
    except Exception:
        pass
    return False

def create_token() -> tuple[str, str]:
    token = generate_token()
    expires_at = (datetime.utcnow() + timedelta(days=TOKEN_VALIDITY_DAYS)).isoformat()
    cfg = load_config()
    tokens = cfg.get("tokens", {})
    now = datetime.utcnow()
    tokens = {t: e for t, e in tokens.items() if datetime.fromisoformat(e) > now}
    tokens[token] = expires_at
    cfg["tokens"] = tokens
    save_config(cfg)
    return token, expires_at

def revoke_all_tokens():
    cfg = load_config()
    cfg["tokens"] = {}
    save_config(cfg)

def is_local_request(request: Request) -> bool:
    client = request.client
    if client is None:
        return True
    host = client.host
    return host in ("127.0.0.1", "::1", "localhost")

async def require_auth(request: Request):
    """Dependency: allow if local IP or valid token"""
    if is_local_request(request):
        return True
    token = request.headers.get("X-Auth-Token", "")
    if token and is_token_valid(token):
        return True
    raise HTTPException(status_code=401, detail="Unauthorized")

# ──────────────────────────────────────────────
# WebSocket connection manager
# ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                dead.append(conn)
        for d in dead:
            self.disconnect(d)

manager = ConnectionManager()

# ──────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS logs (
    id          TEXT PRIMARY KEY,
    request_id  TEXT,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    model       TEXT,
    upstream_model TEXT,
    duration_ms REAL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    request_body  TEXT,
    response_body TEXT,
    chunk_data    TEXT,
    error         TEXT,
    metadata      TEXT,
    hidden        INTEGER DEFAULT 0,
    chunk_count   INTEGER DEFAULT 0,
    stream_content TEXT,
    cost_usd      REAL
);
CREATE INDEX IF NOT EXISTS idx_timestamp   ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_request_id  ON logs(request_id);
CREATE INDEX IF NOT EXISTS idx_event_type  ON logs(event_type);
CREATE INDEX IF NOT EXISTS idx_model       ON logs(model);

CREATE TABLE IF NOT EXISTS notes (
    id         TEXT PRIMARY KEY,
    log_id     TEXT NOT NULL,
    request_id TEXT,
    note       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_log_id     ON notes(log_id);
CREATE INDEX IF NOT EXISTS idx_notes_request_id ON notes(request_id);

CREATE TABLE IF NOT EXISTS alerts (
    id         TEXT PRIMARY KEY,
    alert_type TEXT NOT NULL,
    message    TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL,
    is_read    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);
"""

MIGRATION_SQL = [
    "ALTER TABLE logs ADD COLUMN hidden INTEGER DEFAULT 0",
    "ALTER TABLE logs ADD COLUMN chunk_count INTEGER DEFAULT 0",
    "ALTER TABLE logs ADD COLUMN stream_content TEXT",
    "ALTER TABLE logs ADD COLUMN cost_usd REAL",
]

_db_conn: Optional[aiosqlite.Connection] = None

async def get_db() -> aiosqlite.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = await aiosqlite.connect(str(DB_PATH))
        await _db_conn.execute("PRAGMA journal_mode=WAL")
        await _db_conn.execute("PRAGMA synchronous=NORMAL")
        await _db_conn.executescript(CREATE_TABLE_SQL)
        await _db_conn.commit()
        # Migrations: add columns if not exist
        for sql in MIGRATION_SQL:
            try:
                await _db_conn.execute(sql)
                await _db_conn.commit()
            except Exception:
                pass  # column already exists
        _db_conn.row_factory = aiosqlite.Row
    return _db_conn

def row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("request_body", "response_body", "metadata"):
        if d.get(field) and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    return d

async def cleanup_old_logs():
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    db = await get_db()
    await db.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))
    await db.commit()

async def auto_cleanup_task():
    while True:
        try:
            await cleanup_old_logs()
        except Exception as e:
            print(f"[llm-logger] cleanup error: {e}")
        await asyncio.sleep(6 * 3600)

# ──────────────────────────────────────────────
# 告警系统
# ──────────────────────────────────────────────
async def send_telegram(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

async def send_webhook(url: str, payload: dict):
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json=payload)

async def record_alert(alert_type: str, message: str, detail: str = ""):
    db = await get_db()
    alert_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    await db.execute(
        "INSERT INTO alerts (id, alert_type, message, detail, created_at) VALUES (?,?,?,?,?)",
        (alert_id, alert_type, message, detail, now)
    )
    await db.commit()
    # 保留最近50条告警
    await db.execute(
        "DELETE FROM alerts WHERE id NOT IN (SELECT id FROM alerts ORDER BY created_at DESC LIMIT 50)"
    )
    await db.commit()

def get_alert_config() -> dict:
    cfg = load_config()
    return cfg.get("alert_config", {})

def is_alert_throttled(alert_type: str) -> bool:
    """检查60分钟内是否已发送过该类型告警"""
    cfg = load_config()
    last_times = cfg.get("last_alert_times", {})
    last_str = last_times.get(alert_type)
    if not last_str:
        return False
    try:
        last_dt = datetime.fromisoformat(last_str)
        if (datetime.utcnow() - last_dt).total_seconds() < 3600:
            return True
    except Exception:
        pass
    return False

def update_alert_time(alert_type: str):
    cfg = load_config()
    if "last_alert_times" not in cfg:
        cfg["last_alert_times"] = {}
    cfg["last_alert_times"][alert_type] = datetime.utcnow().isoformat()
    save_config(cfg)

async def dispatch_alert(alert_type: str, message: str, detail: str = ""):
    """记录告警并发送通知"""
    await record_alert(alert_type, message, detail)
    update_alert_time(alert_type)
    ac = get_alert_config()
    tg_token = ac.get("telegram_bot_token", "")
    tg_chat = ac.get("telegram_chat_id", "")
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC"
    tg_text = f"🚨 <b>LLM Logger 告警</b>\n类型：{message}\n{detail}\n时间：{now_str}"
    if tg_token and tg_chat:
        try:
            await send_telegram(tg_token, tg_chat, tg_text)
        except Exception as e:
            print(f"[llm-logger] Telegram send error: {e}")
    webhook_url = ac.get("webhook_url", "")
    if webhook_url:
        try:
            await send_webhook(webhook_url, {
                "alert_type": alert_type,
                "message": message,
                "detail": detail,
                "time": now_str
            })
        except Exception as e:
            print(f"[llm-logger] Webhook send error: {e}")

async def alert_check_task():
    """每60秒检查一次告警条件"""
    await asyncio.sleep(30)  # 启动延迟
    while True:
        try:
            await run_alert_checks()
        except Exception as e:
            print(f"[llm-logger] alert check error: {e}")
        await asyncio.sleep(60)

async def run_alert_checks():
    ac = get_alert_config()
    db = await get_db()
    since_5m = (datetime.utcnow() - timedelta(minutes=5)).isoformat() + "Z"
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"

    # 1. 错误率告警
    if ac.get("error_rate_enabled", True):
        threshold = float(ac.get("error_rate_threshold", 20))
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM logs WHERE timestamp >= ? AND event_type='success'", (since_5m,)
        ) as cur:
            s_cnt = (await cur.fetchone())["cnt"]
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM logs WHERE timestamp >= ? AND event_type='failure'", (since_5m,)
        ) as cur:
            f_cnt = (await cur.fetchone())["cnt"]
        total = s_cnt + f_cnt
        if total >= 3:
            err_rate = f_cnt / total * 100
            if err_rate > threshold and not is_alert_throttled("error_rate"):
                await dispatch_alert(
                    "error_rate",
                    f"错误率过高",
                    f"当前：{err_rate:.1f}%（阈值{threshold}%），过去5分钟共{total}次请求"
                )

    # 2. 延迟告警
    if ac.get("latency_enabled", True):
        threshold_ms = float(ac.get("latency_threshold_sec", 30)) * 1000
        async with db.execute(
            "SELECT AVG(duration_ms) as avg_ms FROM logs WHERE timestamp >= ? AND event_type='success' AND duration_ms IS NOT NULL",
            (since_5m,)
        ) as cur:
            row = await cur.fetchone()
            avg_ms = row["avg_ms"] or 0
        if avg_ms > threshold_ms and not is_alert_throttled("latency"):
            await dispatch_alert(
                "latency",
                f"平均延迟过高",
                f"当前：{avg_ms/1000:.1f}s（阈值{threshold_ms/1000:.0f}s），过去5分钟"
            )

    # 3. 日Token配额预警
    if ac.get("daily_token_enabled", False):
        threshold_tokens = float(ac.get("daily_token_threshold_m", 10)) * 1_000_000
        async with db.execute(
            "SELECT SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) as tot FROM logs WHERE timestamp >= ? AND event_type='success'",
            (today_start,)
        ) as cur:
            row = await cur.fetchone()
            today_tokens = row["tot"] or 0
        if today_tokens > threshold_tokens and not is_alert_throttled("daily_token"):
            await dispatch_alert(
                "daily_token",
                f"日Token配额预警",
                f"今日已用：{today_tokens/1_000_000:.2f}M（阈值{threshold_tokens/1_000_000:.0f}M）"
            )

# ──────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_config()
    await get_db()
    cleanup_task = asyncio.create_task(auto_cleanup_task())
    alert_task = asyncio.create_task(alert_check_task())
    yield
    cleanup_task.cancel()
    alert_task.cancel()
    if _db_conn:
        await _db_conn.close()

app = FastAPI(title="LLM Logger", version="4.0.0", lifespan=lifespan)

# ──────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────
class LogEntry(BaseModel):
    id: Optional[str] = None
    request_id: Optional[str] = None
    timestamp: Optional[str] = None
    event_type: str
    model: Optional[str] = None
    upstream_model: Optional[str] = None
    duration_ms: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    request_body: Optional[dict] = None
    response_body: Optional[dict] = None
    chunk_data: Optional[str] = None
    chunk_count: Optional[int] = None
    stream_content: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[dict] = None

class LoginRequest(BaseModel):
    password: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class NoteRequest(BaseModel):
    log_id: str
    request_id: Optional[str] = None
    note: str

class AlertConfigRequest(BaseModel):
    telegram_bot_token: Optional[str] = ""
    telegram_chat_id: Optional[str] = ""
    webhook_url: Optional[str] = ""
    error_rate_enabled: Optional[bool] = True
    error_rate_threshold: Optional[float] = 20.0
    latency_enabled: Optional[bool] = True
    latency_threshold_sec: Optional[float] = 30.0
    large_request_enabled: Optional[bool] = True
    large_request_threshold: Optional[int] = 100000
    daily_token_enabled: Optional[bool] = False
    daily_token_threshold_m: Optional[float] = 10.0

# ──────────────────────────────────────────────
# AUTH endpoints (no auth required)
# ──────────────────────────────────────────────
@app.post("/auth/login")
async def auth_login(req: LoginRequest):
    cfg = load_config()
    if sha256(req.password) != cfg.get("password_hash", ""):
        raise HTTPException(status_code=401, detail="密码错误")
    token, expires_at = create_token()
    return {"token": token, "expires_at": expires_at}

@app.get("/auth/verify")
async def auth_verify(request: Request):
    token = request.headers.get("X-Auth-Token", "")
    if is_local_request(request) or (token and is_token_valid(token)):
        return {"valid": True}
    return {"valid": False}

@app.post("/auth/change-password")
async def auth_change_password(req: ChangePasswordRequest, _=Depends(require_auth)):
    cfg = load_config()
    if sha256(req.old_password) != cfg.get("password_hash", ""):
        raise HTTPException(status_code=400, detail="旧密码错误")
    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="新密码至少4位")
    cfg["password_hash"] = sha256(req.new_password)
    cfg["tokens"] = {}
    save_config(cfg)
    return {"ok": True}

# ──────────────────────────────────────────────
# POST /log  (no auth)
# ──────────────────────────────────────────────
@app.post("/log")
async def receive_log(entry: LogEntry):
    if not entry.id:
        entry.id = str(uuid.uuid4())
    if not entry.timestamp:
        entry.timestamp = datetime.utcnow().isoformat() + "Z"

    db = await get_db()

    # ── chunk 事件：累积到内存缓冲区，标记 hidden=1 存库 ──
    if entry.event_type == "chunk":
        rid = entry.request_id or ""
        if rid:
            if rid not in _chunk_buffer:
                _chunk_buffer[rid] = []
            if entry.chunk_data:
                _chunk_buffer[rid].append(entry.chunk_data)
        # 存库但标记 hidden
        await db.execute(
            """INSERT OR REPLACE INTO logs
               (id, request_id, timestamp, event_type, model, upstream_model,
                duration_ms, input_tokens, output_tokens,
                request_body, response_body, chunk_data, error, metadata, hidden)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                entry.id, entry.request_id, entry.timestamp, entry.event_type,
                entry.model, entry.upstream_model, entry.duration_ms,
                entry.input_tokens, entry.output_tokens,
                json.dumps(entry.request_body, ensure_ascii=False) if entry.request_body else None,
                json.dumps(entry.response_body, ensure_ascii=False) if entry.response_body else None,
                entry.chunk_data, entry.error,
                json.dumps(entry.metadata, ensure_ascii=False) if entry.metadata else None,
            ),
        )
        await db.commit()
        data = entry.model_dump()
        data["hidden"] = True
        await manager.broadcast(data)
        return {"ok": True, "id": entry.id}

    # ── success 事件：合并 chunk、计算成本 ──
    chunk_count = 0
    stream_content = None
    if entry.event_type == "success":
        rid = entry.request_id or ""
        if rid and rid in _chunk_buffer:
            # 有 chunk 事件积累的情况（逐 chunk 模式）
            chunks = _chunk_buffer.pop(rid)
            chunk_count = len(chunks)
            stream_content = "".join(chunks)
        elif entry.chunk_count and entry.chunk_count > 0:
            # callback 直接传来的 chunk_count（聚合模式，如 litellm success 事件）
            chunk_count = entry.chunk_count
            stream_content = entry.stream_content
        # 大请求告警
        ac = get_alert_config()
        if ac.get("large_request_enabled", True):
            threshold = int(ac.get("large_request_threshold", 100000))
            if (entry.input_tokens or 0) > threshold and not is_alert_throttled("large_request"):
                asyncio.create_task(dispatch_alert(
                    "large_request",
                    f"大请求告警",
                    f"单次输入token：{entry.input_tokens}（阈值{threshold}），模型：{entry.model or '未知'}"
                ))

    # 计算成本
    cost_usd = None
    if entry.event_type == "success":
        cost_usd = calc_cost(entry.model, entry.input_tokens, entry.output_tokens)

    await db.execute(
        """INSERT OR REPLACE INTO logs
           (id, request_id, timestamp, event_type, model, upstream_model,
            duration_ms, input_tokens, output_tokens,
            request_body, response_body, chunk_data, error, metadata,
            hidden, chunk_count, stream_content, cost_usd)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
        (
            entry.id, entry.request_id, entry.timestamp, entry.event_type,
            entry.model, entry.upstream_model, entry.duration_ms,
            entry.input_tokens, entry.output_tokens,
            json.dumps(entry.request_body, ensure_ascii=False) if entry.request_body else None,
            json.dumps(entry.response_body, ensure_ascii=False) if entry.response_body else None,
            entry.chunk_data, entry.error,
            json.dumps(entry.metadata, ensure_ascii=False) if entry.metadata else None,
            chunk_count, stream_content, cost_usd,
        ),
    )
    await db.commit()

    data = entry.model_dump()
    data["chunk_count"] = chunk_count
    data["stream_content"] = stream_content
    data["cost_usd"] = cost_usd
    data["hidden"] = False
    await manager.broadcast(data)
    return {"ok": True, "id": entry.id}

# ──────────────────────────────────────────────
# GET /logs  (paginated, multi-filter) — auth required
# ──────────────────────────────────────────────
@app.get("/logs")
async def query_logs(
    model: Optional[str] = Query(None),
    request_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    event_types: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    regex: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    annotated_only: Optional[bool] = Query(None),
    include_chunks: Optional[bool] = Query(False),
    min_cost: Optional[float] = Query(None),
    max_cost: Optional[float] = Query(None),
    cost_tier: Optional[str] = Query(None),  # cheap/medium/expensive
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    _=Depends(require_auth),
):
    db = await get_db()
    conditions = []
    params: list = []

    # 默认不返回 hidden chunk 事件
    if not include_chunks:
        conditions.append("l.hidden = 0")

    if model:
        conditions.append("l.model = ?")
        params.append(model)
    if request_id:
        conditions.append("l.request_id = ?")
        params.append(request_id)

    et_list = []
    if event_types:
        et_list = [e.strip() for e in event_types.split(",") if e.strip()]
    elif event_type:
        et_list = [event_type]
    if et_list:
        placeholders = ",".join("?" * len(et_list))
        conditions.append(f"l.event_type IN ({placeholders})")
        params.extend(et_list)

    if status == "success":
        conditions.append("l.event_type = 'success'")
    elif status == "failure":
        conditions.append("l.event_type = 'failure'")

    if start_time:
        conditions.append("l.timestamp >= ?")
        params.append(start_time)
    if end_time:
        conditions.append("l.timestamp <= ?")
        params.append(end_time)
    if keyword:
        conditions.append(
            "(l.model LIKE ? OR l.request_id LIKE ? OR l.request_body LIKE ? OR l.response_body LIKE ? OR l.chunk_data LIKE ? OR l.error LIKE ?)"
        )
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw, kw, kw])

    if annotated_only:
        conditions.append("EXISTS (SELECT 1 FROM notes n WHERE n.log_id = l.id OR n.request_id = l.request_id)")

    # 成本过滤
    if cost_tier == "cheap":
        conditions.append("l.cost_usd IS NOT NULL AND l.cost_usd < 0.01")
    elif cost_tier == "medium":
        conditions.append("l.cost_usd IS NOT NULL AND l.cost_usd >= 0.01 AND l.cost_usd < 0.1")
    elif cost_tier == "expensive":
        conditions.append("l.cost_usd IS NOT NULL AND l.cost_usd >= 0.1")
    if min_cost is not None:
        conditions.append("l.cost_usd >= ?")
        params.append(min_cost)
    if max_cost is not None:
        conditions.append("l.cost_usd <= ?")
        params.append(max_cost)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_sql = f"SELECT COUNT(*) as cnt FROM logs l {where_clause}"
    async with db.execute(count_sql, params) as cur:
        row = await cur.fetchone()
        total = row["cnt"] if row else 0

    offset = (page - 1) * page_size
    data_sql = f"SELECT l.* FROM logs l {where_clause} ORDER BY l.timestamp DESC LIMIT ? OFFSET ?"
    async with db.execute(data_sql, params + [page_size, offset]) as cur:
        rows = await cur.fetchall()

    log_ids = [r["id"] for r in rows]
    annotated_ids = set()
    if log_ids:
        placeholders = ",".join("?" * len(log_ids))
        async with db.execute(
            f"SELECT DISTINCT log_id FROM notes WHERE log_id IN ({placeholders})",
            log_ids
        ) as cur:
            ann_rows = await cur.fetchall()
            annotated_ids = {r["log_id"] for r in ann_rows}

    result = []
    for r in rows:
        d = row_to_dict(r)
        d["has_note"] = d["id"] in annotated_ids
        result.append(d)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "data": result,
    }

# ──────────────────────────────────────────────
# GET /logs/{request_id}  - full request chain
# ──────────────────────────────────────────────
@app.get("/logs/{request_id}")
async def get_request_chain(request_id: str, _=Depends(require_auth)):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM logs WHERE request_id = ? ORDER BY timestamp ASC",
        (request_id,),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        raise HTTPException(404, f"request_id {request_id!r} not found")
    return {"request_id": request_id, "events": [row_to_dict(r) for r in rows]}

# ──────────────────────────────────────────────
# Notes endpoints
# ──────────────────────────────────────────────
@app.post("/notes")
async def create_note(req: NoteRequest, _=Depends(require_auth)):
    db = await get_db()
    note_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    await db.execute(
        "INSERT INTO notes (id, log_id, request_id, note, created_at) VALUES (?,?,?,?,?)",
        (note_id, req.log_id, req.request_id, req.note, now)
    )
    await db.commit()
    return {"ok": True, "id": note_id}

@app.get("/notes/{log_id}")
async def get_note(log_id: str, _=Depends(require_auth)):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM notes WHERE log_id = ? ORDER BY created_at DESC",
        (log_id,)
    ) as cur:
        rows = await cur.fetchall()
    return {"notes": [dict(r) for r in rows]}

@app.delete("/notes/{note_id}")
async def delete_note(note_id: str, _=Depends(require_auth)):
    db = await get_db()
    await db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    await db.commit()
    return {"ok": True}

# ──────────────────────────────────────────────
# GET /stats
# ──────────────────────────────────────────────
@app.get("/stats")
async def get_stats(hours: int = Query(24, ge=1, le=720), _=Depends(require_auth)):
    db = await get_db()
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"

    async with db.execute(
        "SELECT COUNT(*) as cnt FROM logs WHERE timestamp >= ? AND event_type='success'", (since,)
    ) as cur:
        success_count = (await cur.fetchone())["cnt"]

    async with db.execute(
        "SELECT COUNT(*) as cnt FROM logs WHERE timestamp >= ? AND event_type='failure'", (since,)
    ) as cur:
        failure_count = (await cur.fetchone())["cnt"]

    async with db.execute(
        "SELECT AVG(duration_ms) as avg_ms FROM logs WHERE timestamp >= ? AND event_type='success' AND duration_ms IS NOT NULL",
        (since,),
    ) as cur:
        row = await cur.fetchone()
        avg_duration = round(row["avg_ms"], 2) if row["avg_ms"] else 0

    async with db.execute(
        "SELECT SUM(input_tokens) as inp, SUM(output_tokens) as outp FROM logs WHERE timestamp >= ? AND event_type='success'",
        (since,),
    ) as cur:
        row = await cur.fetchone()
        total_input = row["inp"] or 0
        total_output = row["outp"] or 0

    async with db.execute(
        "SELECT SUM(cost_usd) as total_cost, AVG(cost_usd) as avg_cost FROM logs WHERE timestamp >= ? AND event_type='success' AND cost_usd IS NOT NULL",
        (since,),
    ) as cur:
        row = await cur.fetchone()
        total_cost = round(row["total_cost"], 6) if row["total_cost"] else 0.0
        avg_cost = round(row["avg_cost"], 6) if row["avg_cost"] else 0.0

    rpm_since = (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z"
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM logs WHERE timestamp >= ? AND event_type='pre_call'", (rpm_since,)
    ) as cur:
        rpm = round((await cur.fetchone())["cnt"] / 60, 1)

    rpm_history = []
    for i in range(9, -1, -1):
        bucket_start = (datetime.utcnow() - timedelta(minutes=i+1)).isoformat() + "Z"
        bucket_end = (datetime.utcnow() - timedelta(minutes=i)).isoformat() + "Z"
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM logs WHERE timestamp >= ? AND timestamp < ? AND event_type='pre_call'",
            (bucket_start, bucket_end),
        ) as cur:
            rpm_history.append((await cur.fetchone())["cnt"])

    total = success_count + failure_count
    success_rate = round(success_count / total * 100, 1) if total > 0 else 0

    return {
        "period_hours": hours,
        "total_requests": total,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate": success_rate,
        "avg_duration_ms": avg_duration,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_cost_usd": total_cost,
        "avg_cost_usd": avg_cost,
        "rpm": rpm,
        "rpm_history": rpm_history,
    }

@app.get("/stats/models")
async def get_model_stats(hours: int = Query(24, ge=1, le=720), _=Depends(require_auth)):
    db = await get_db()
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
    async with db.execute(
        """SELECT model,
                  COUNT(*) as requests,
                  SUM(CASE WHEN event_type='success' THEN 1 ELSE 0 END) as successes,
                  SUM(CASE WHEN event_type='failure' THEN 1 ELSE 0 END) as failures,
                  AVG(CASE WHEN event_type='success' THEN duration_ms END) as avg_ms,
                  SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) as total_tokens,
                  SUM(COALESCE(cost_usd,0)) as cost_usd
           FROM logs
           WHERE timestamp >= ? AND model IS NOT NULL AND model != ''
           GROUP BY model
           ORDER BY requests DESC""",
        (since,),
    ) as cur:
        rows = await cur.fetchall()
    return {"models": [dict(r) for r in rows]}

@app.get("/stats/timeline")
async def get_timeline(minutes: int = Query(60, ge=5, le=1440), bucket: int = Query(5, ge=1, le=60), _=Depends(require_auth)):
    db = await get_db()
    since = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat() + "Z"
    async with db.execute(
        "SELECT timestamp, event_type, duration_ms FROM logs WHERE timestamp >= ? ORDER BY timestamp ASC",
        (since,),
    ) as cur:
        rows = await cur.fetchall()

    buckets: dict = {}
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            floored = ts.replace(minute=(ts.minute // bucket) * bucket, second=0, microsecond=0)
            key = floored.strftime("%Y-%m-%dT%H:%M:00Z")
        except Exception:
            continue
        if key not in buckets:
            buckets[key] = {"time": key, "requests": 0, "errors": 0, "avg_ms": []}
        b = buckets[key]
        b["requests"] += 1
        if r["event_type"] == "failure":
            b["errors"] += 1
        if r["duration_ms"] is not None and r["event_type"] == "success":
            b["avg_ms"].append(r["duration_ms"])

    timeline = []
    for b in sorted(buckets.values(), key=lambda x: x["time"]):
        avg = round(sum(b["avg_ms"]) / len(b["avg_ms"]), 1) if b["avg_ms"] else 0
        timeline.append({"time": b["time"], "requests": b["requests"], "errors": b["errors"], "avg_ms": avg})

    return {"bucket_minutes": bucket, "timeline": timeline}

@app.get("/stats/daily")
async def get_daily_stats(_=Depends(require_auth)):
    db = await get_db()
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + "Z"
    yesterday_start = (datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)).isoformat() + "Z"

    async with db.execute(
        "SELECT COUNT(*) as cnt FROM logs WHERE timestamp >= ? AND event_type IN ('success','failure')", (today_start,)
    ) as cur:
        today_total = (await cur.fetchone())["cnt"]

    async with db.execute(
        "SELECT COUNT(*) as cnt FROM logs WHERE timestamp >= ? AND timestamp < ? AND event_type IN ('success','failure')",
        (yesterday_start, today_start),
    ) as cur:
        yesterday_total = (await cur.fetchone())["cnt"]

    since_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat() + "Z"
    async with db.execute(
        "SELECT error FROM logs WHERE timestamp >= ? AND event_type='failure' AND error IS NOT NULL", (since_24h,)
    ) as cur:
        error_rows = await cur.fetchall()

    error_types: dict = {}
    for r in error_rows:
        err = str(r["error"])
        first_line = err.split("\n")[0][:80]
        error_types[first_line] = error_types.get(first_line, 0) + 1

    error_dist = sorted([{"error": k, "count": v} for k, v in error_types.items()], key=lambda x: -x["count"])[:10]

    return {"today_total": today_total, "yesterday_total": yesterday_total, "error_distribution": error_dist}

@app.get("/stats/latency-dist")
async def get_latency_dist(hours: int = Query(24, ge=1, le=720), _=Depends(require_auth)):
    db = await get_db()
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
    async with db.execute(
        "SELECT duration_ms FROM logs WHERE timestamp >= ? AND event_type='success' AND duration_ms IS NOT NULL",
        (since,)
    ) as cur:
        rows = await cur.fetchall()
    durations = [r["duration_ms"] for r in rows]

    buckets = [
        {"label": "<500ms", "min": 0, "max": 500, "count": 0},
        {"label": "500ms-1s", "min": 500, "max": 1000, "count": 0},
        {"label": "1s-2s", "min": 1000, "max": 2000, "count": 0},
        {"label": "2s-5s", "min": 2000, "max": 5000, "count": 0},
        {"label": "5s-10s", "min": 5000, "max": 10000, "count": 0},
        {"label": ">10s", "min": 10000, "max": float("inf"), "count": 0},
    ]
    for d in durations:
        for b in buckets:
            if b["min"] <= d < b["max"]:
                b["count"] += 1
                break
    for b in buckets:
        if b["max"] == float("inf"):
            b["max"] = -1
    return {"buckets": buckets, "total": len(durations)}

@app.get("/stats/token-dist")
async def get_token_dist(hours: int = Query(24, ge=1, le=720), _=Depends(require_auth)):
    db = await get_db()
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
    async with db.execute(
        """SELECT model,
                  SUM(COALESCE(input_tokens,0)) as input_tokens,
                  SUM(COALESCE(output_tokens,0)) as output_tokens,
                  SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) as total_tokens
           FROM logs WHERE timestamp >= ? AND event_type='success' AND model IS NOT NULL
           GROUP BY model ORDER BY total_tokens DESC""",
        (since,)
    ) as cur:
        rows = await cur.fetchall()
    return {"models": [dict(r) for r in rows]}

# ──────────────────────────────────────────────
# GET /health  (no auth)
# ──────────────────────────────────────────────
@app.get("/health")
async def health():
    db = await get_db()
    async with db.execute("SELECT COUNT(*) as cnt FROM logs") as cur:
        total = (await cur.fetchone())["cnt"]
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z", "total_logs": total, "version": "4.0.0"}

# ──────────────────────────────────────────────
# DELETE /logs
# ──────────────────────────────────────────────
@app.delete("/logs")
async def delete_logs(before_date: Optional[str] = Query(None), _=Depends(require_auth)):
    db = await get_db()
    if before_date:
        cutoff = before_date + "T00:00:00Z"
        async with db.execute("SELECT COUNT(*) as cnt FROM logs WHERE timestamp < ?", (cutoff,)) as cur:
            cnt = (await cur.fetchone())["cnt"]
        await db.execute("DELETE FROM logs WHERE timestamp < ?", (cutoff,))
    else:
        async with db.execute("SELECT COUNT(*) as cnt FROM logs") as cur:
            cnt = (await cur.fetchone())["cnt"]
        await db.execute("DELETE FROM logs")
    await db.commit()
    return {"deleted": cnt}

# ──────────────────────────────────────────────
# GET /models
# ──────────────────────────────────────────────
@app.get("/models")
async def list_models(_=Depends(require_auth)):
    db = await get_db()
    async with db.execute(
        "SELECT DISTINCT model FROM logs WHERE model IS NOT NULL AND model != '' ORDER BY model"
    ) as cur:
        rows = await cur.fetchall()
    return {"models": [r["model"] for r in rows]}

# ──────────────────────────────────────────────
# WebSocket /ws
# ──────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

# ──────────────────────────────────────────────
# 告警配置 API
# ──────────────────────────────────────────────
@app.get("/alerts/config")
async def get_alert_config_api(_=Depends(require_auth)):
    ac = get_alert_config()
    # 不返回 token 明文（仅返回是否已配置）
    safe = {k: v for k, v in ac.items() if k not in ("telegram_bot_token",)}
    safe["has_telegram_token"] = bool(ac.get("telegram_bot_token", ""))
    return safe

@app.post("/alerts/config")
async def save_alert_config_api(req: AlertConfigRequest, _=Depends(require_auth)):
    cfg = load_config()
    ac = cfg.get("alert_config", {})
    data = req.model_dump()
    # 保留旧 token 如果新值为空
    if not data.get("telegram_bot_token") and "telegram_bot_token" in ac:
        data["telegram_bot_token"] = ac["telegram_bot_token"]
    ac.update(data)
    cfg["alert_config"] = ac
    save_config(cfg)
    return {"ok": True}

@app.post("/alerts/test")
async def test_alert(request: Request):
    """发送测试消息（不需要认证，但需要配置）"""
    ac = get_alert_config()
    tg_token = ac.get("telegram_bot_token", "")
    tg_chat = ac.get("telegram_chat_id", "")
    results = {}
    if tg_token and tg_chat:
        try:
            await send_telegram(tg_token, tg_chat, "✅ LLM Logger 告警测试消息发送成功！")
            results["telegram"] = "ok"
        except Exception as e:
            results["telegram"] = f"error: {e}"
    else:
        results["telegram"] = "not configured"

    webhook_url = ac.get("webhook_url", "")
    if webhook_url:
        try:
            await send_webhook(webhook_url, {"type": "test", "message": "LLM Logger 告警测试"})
            results["webhook"] = "ok"
        except Exception as e:
            results["webhook"] = f"error: {e}"
    else:
        results["webhook"] = "not configured"
    return results

@app.get("/alerts/history")
async def get_alert_history(_=Depends(require_auth)):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM alerts ORDER BY created_at DESC LIMIT 10"
    ) as cur:
        rows = await cur.fetchall()
    return {"alerts": [dict(r) for r in rows]}

@app.post("/alerts/read")
async def mark_alerts_read(_=Depends(require_auth)):
    db = await get_db()
    await db.execute("UPDATE alerts SET is_read=1")
    await db.commit()
    return {"ok": True}

# Static files last (catch-all)
app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")
