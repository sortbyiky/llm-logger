"""
LLM Logger - Enterprise Grade Backend V3
SQLite (aiosqlite) + WAL mode, full API, WebSocket real-time push, auto-cleanup
新增：认证系统、日志标注 (notes)、全文搜索增强
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager

DB_PATH = Path("/app/logs/llm_logger.db")
CONFIG_PATH = Path("/app/logs/config.json")
DB_PATH.parent.mkdir(exist_ok=True)

DEFAULT_PASSWORD = "183193"
TOKEN_VALIDITY_DAYS = 7

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
    # Prune expired tokens
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
    metadata      TEXT
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
"""

_db_conn: Optional[aiosqlite.Connection] = None

async def get_db() -> aiosqlite.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = await aiosqlite.connect(str(DB_PATH))
        await _db_conn.execute("PRAGMA journal_mode=WAL")
        await _db_conn.execute("PRAGMA synchronous=NORMAL")
        await _db_conn.executescript(CREATE_TABLE_SQL)
        await _db_conn.commit()
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
# Lifespan
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_config()
    await get_db()
    task = asyncio.create_task(auto_cleanup_task())
    yield
    task.cancel()
    if _db_conn:
        await _db_conn.close()

app = FastAPI(title="LLM Logger", version="3.0.0", lifespan=lifespan)

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
    cfg["tokens"] = {}  # Revoke all tokens
    save_config(cfg)
    return {"ok": True}

# ──────────────────────────────────────────────
# POST /log  (no auth so LiteLLM can post freely from internal network)
# ──────────────────────────────────────────────
@app.post("/log")
async def receive_log(entry: LogEntry):
    if not entry.id:
        entry.id = str(uuid.uuid4())
    if not entry.timestamp:
        entry.timestamp = datetime.utcnow().isoformat() + "Z"

    db = await get_db()
    await db.execute(
        """INSERT OR REPLACE INTO logs
           (id, request_id, timestamp, event_type, model, upstream_model,
            duration_ms, input_tokens, output_tokens,
            request_body, response_body, chunk_data, error, metadata)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            entry.id,
            entry.request_id,
            entry.timestamp,
            entry.event_type,
            entry.model,
            entry.upstream_model,
            entry.duration_ms,
            entry.input_tokens,
            entry.output_tokens,
            json.dumps(entry.request_body, ensure_ascii=False) if entry.request_body is not None else None,
            json.dumps(entry.response_body, ensure_ascii=False) if entry.response_body is not None else None,
            entry.chunk_data,
            entry.error,
            json.dumps(entry.metadata, ensure_ascii=False) if entry.metadata is not None else None,
        ),
    )
    await db.commit()

    data = entry.model_dump()
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
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    _=Depends(require_auth),
):
    db = await get_db()
    conditions = []
    params: list = []

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

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_sql = f"SELECT COUNT(*) as cnt FROM logs l {where_clause}"
    async with db.execute(count_sql, params) as cur:
        row = await cur.fetchone()
        total = row["cnt"] if row else 0

    offset = (page - 1) * page_size
    data_sql = f"SELECT l.* FROM logs l {where_clause} ORDER BY l.timestamp DESC LIMIT ? OFFSET ?"
    async with db.execute(data_sql, params + [page_size, offset]) as cur:
        rows = await cur.fetchall()

    # Fetch note IDs for these log entries
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
                  SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) as total_tokens
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

# ──────────────────────────────────────────────
# GET /stats/latency-dist  - latency distribution for histogram
# ──────────────────────────────────────────────
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

    # Bucket into ranges: 0-500, 500-1000, 1000-2000, 2000-5000, 5000+
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

# ──────────────────────────────────────────────
# GET /stats/token-dist  - token usage by model
# ──────────────────────────────────────────────
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
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z", "total_logs": total, "version": "3.0.0"}

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
# WebSocket /ws  (no auth - WS auth handled in frontend via token param)
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

# Static files last (catch-all)
app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")
