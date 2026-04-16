"""SQLite 日志模块 — 按月分库，两张表：request_log（轻量）+ request_detail（完整 body/响应）"""

import sqlite3
import threading
import json
import time
import os
from datetime import datetime, timezone, timedelta

_local = threading.local()
_db_dir = None       # DB 文件所在目录
_db_prefix = None    # DB 文件名前缀（不含 .db）
_current_month = None  # 当前月份 "YYYY-MM"
_write_lock = threading.Lock()  # 序列化所有写操作，防止并发协程冲突

_BJT = timezone(timedelta(hours=8))


def _current_db_path():
    """返回当前月份的 DB 文件路径"""
    month = datetime.now(_BJT).strftime("%Y-%m")
    return os.path.join(_db_dir, f"{_db_prefix}-{month}.db"), month


def _init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            finished_at REAL,
            client_ip TEXT,
            api_key_name TEXT,
            model TEXT,
            status TEXT DEFAULT 'pending',
            is_stream INTEGER DEFAULT 1,
            msg_count INTEGER DEFAULT 0,
            tool_count INTEGER DEFAULT 0,
            error_message TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            connect_time_ms INTEGER,
            first_token_time_ms INTEGER,
            total_time_ms INTEGER,
            retry_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS request_detail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT UNIQUE NOT NULL,
            request_headers TEXT,
            request_body TEXT,
            response_body TEXT,
            FOREIGN KEY (request_id) REFERENCES request_log(request_id)
        );

        CREATE INDEX IF NOT EXISTS idx_log_created ON request_log(created_at);
        CREATE INDEX IF NOT EXISTS idx_log_status ON request_log(status);
        CREATE INDEX IF NOT EXISTS idx_log_apikey ON request_log(api_key_name);
    """)
    _ensure_schema_columns(conn)
    conn.commit()


def _ensure_schema_columns(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(request_log)").fetchall()}
    if "retry_count" not in cols:
        conn.execute("ALTER TABLE request_log ADD COLUMN retry_count INTEGER DEFAULT 0")


def init(path):
    """初始化 DB 模块。path 是原始 db 路径如 /opt/cc-proxy/cc-proxy.db，
    实际文件名会按月后缀，如 cc-proxy-2026-04.db"""
    global _db_dir, _db_prefix, _current_month
    _db_dir = os.path.dirname(path)
    # 从 "cc-proxy.db" 提取 "cc-proxy"
    base = os.path.basename(path)
    if base.endswith(".db"):
        _db_prefix = base[:-3]
    else:
        _db_prefix = base
    # 初始化当前月的 DB
    conn = _get_conn()
    print(f"[DB] Using {_current_db_path()[0]}")


def _get_conn():
    global _current_month
    db_path, month = _current_db_path()
    # 检查是否需要切换到新月份的 DB
    need_new = (not hasattr(_local, "conn") or _local.conn is None
                or _local.month != month)
    if need_new:
        # 关闭旧连接
        if hasattr(_local, "conn") and _local.conn is not None:
            try:
                _local.conn.close()
            except Exception:
                pass
        _local.conn = sqlite3.connect(db_path, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local.month = month
        _current_month = month
        _init_schema(_local.conn)
    return _local.conn


def insert_pending(request_id, client_ip, api_key_name, model, is_stream,
                   msg_count, tool_count, request_headers, request_body):
    """请求发起时立即记录（状态 pending）"""
    with _write_lock:
        conn = _get_conn()
        now = time.time()
        conn.execute(
            """INSERT INTO request_log
               (request_id, created_at, client_ip, api_key_name, model, status,
                is_stream, msg_count, tool_count)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (request_id, now, client_ip, api_key_name, model, "pending",
             1 if is_stream else 0, msg_count, tool_count),
        )
        conn.execute(
            """INSERT INTO request_detail (request_id, request_headers, request_body)
               VALUES (?,?,?)""",
            (request_id,
             json.dumps(request_headers, ensure_ascii=False) if request_headers else None,
             json.dumps(request_body, ensure_ascii=False) if request_body else None),
        )
        conn.commit()


def finish_success(request_id, input_tokens, output_tokens, cache_creation,
                   cache_read, connect_ms, first_token_ms, total_ms, response_body,
                   retry_count=0):
    """请求成功完成 — response_body 完整存储，不截断"""
    with _write_lock:
        conn = _get_conn()
        now = time.time()
        conn.execute(
            """UPDATE request_log SET
               status='success', finished_at=?, input_tokens=?, output_tokens=?,
               cache_creation_tokens=?, cache_read_tokens=?,
               connect_time_ms=?, first_token_time_ms=?, total_time_ms=?, retry_count=?
               WHERE request_id=?""",
            (now, input_tokens, output_tokens, cache_creation, cache_read,
             connect_ms, first_token_ms, total_ms, retry_count, request_id),
        )
        conn.execute(
            "UPDATE request_detail SET response_body=? WHERE request_id=?",
            (response_body, request_id),
        )
        conn.commit()


def finish_error(request_id, error_message, connect_ms, total_ms, response_body=None, retry_count=0):
    """请求失败 — error_message 不截断"""
    with _write_lock:
        conn = _get_conn()
        now = time.time()
        conn.execute(
            """UPDATE request_log SET
               status='error', finished_at=?, error_message=?,
               connect_time_ms=?, total_time_ms=?, retry_count=?
               WHERE request_id=?""",
            (now, error_message, connect_ms, total_ms, retry_count, request_id),
        )
        if response_body:
            conn.execute(
                "UPDATE request_detail SET response_body=? WHERE request_id=?",
                (response_body, request_id),
            )
        conn.commit()


def cleanup_stale_pending(timeout_seconds=600):
    """启动时清理因进程崩溃遗留的 pending 记录"""
    with _write_lock:
        conn = _get_conn()
        cutoff = time.time() - timeout_seconds
        count = conn.execute(
            """UPDATE request_log SET status='error', error_message='process crashed (stale pending)',
               finished_at=? WHERE status='pending' AND created_at < ?""",
            (time.time(), cutoff),
        ).rowcount
        conn.commit()
        if count:
            print(f"[DB] Cleaned up {count} stale pending records")


def checkpoint():
    """执行 WAL checkpoint，控制 WAL 文件大小"""
    conn = _get_conn()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# ─── 查询 ───

def recent_logs(limit=20):
    conn = _get_conn()
    return conn.execute(
        """SELECT request_id, created_at, client_ip, api_key_name, model, status,
                  is_stream, error_message, input_tokens, output_tokens,
                  cache_creation_tokens, cache_read_tokens,
                  connect_time_ms, first_token_time_ms, total_time_ms, retry_count
           FROM request_log ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def recent_success_logs(limit=3):
    conn = _get_conn()
    return conn.execute(
        """SELECT request_id, created_at, client_ip, api_key_name, model, status,
                  is_stream, error_message, input_tokens, output_tokens,
                  cache_creation_tokens, cache_read_tokens,
                  connect_time_ms, first_token_time_ms, total_time_ms, retry_count
           FROM request_log
           WHERE status IN ('success', 'error')
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def stats_summary(since_ts):
    """统计汇总"""
    conn = _get_conn()
    row = conn.execute(
        """SELECT
             COUNT(*) as total,
             SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success_count,
             SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as error_count,
             SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending_count,
             SUM(retry_count) as total_retries,
             SUM(CASE WHEN retry_count > 0 THEN 1 ELSE 0 END) as retried_requests,
             AVG(CASE WHEN status='success' THEN connect_time_ms END) as avg_connect_ms,
             AVG(CASE WHEN status='success' AND is_stream=1 THEN first_token_time_ms END) as avg_first_token_ms,
             AVG(CASE WHEN status='success' THEN total_time_ms END) as avg_total_ms,
             SUM(CASE WHEN status='success' AND cache_read_tokens > 0 THEN 1 ELSE 0 END) as success_with_cache_hit,
             SUM(CASE WHEN status='success' AND cache_creation_tokens > 0 THEN 1 ELSE 0 END) as success_with_cache_write,
             SUM(input_tokens) as total_input_tokens,
             SUM(output_tokens) as total_output_tokens,
             SUM(cache_creation_tokens) as total_cache_creation,
             SUM(cache_read_tokens) as total_cache_read
           FROM request_log WHERE created_at >= ?""",
        (since_ts,),
    ).fetchone()

    recent_errors = conn.execute(
        """SELECT created_at, api_key_name, model, error_message
           FROM request_log WHERE status='error' AND created_at >= ?
           ORDER BY created_at DESC LIMIT 5""",
        (since_ts,),
    ).fetchall()

    recent_calls = conn.execute(
        """SELECT request_id, created_at, model, status, error_message,
                  is_stream,
                  input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                  connect_time_ms, first_token_time_ms, total_time_ms, retry_count
           FROM request_log WHERE created_at >= ?
           ORDER BY created_at DESC LIMIT 3""",
        (since_ts,),
    ).fetchall()

    return row, recent_errors, recent_calls


def cache_stats_by_model(since_ts, limit=3):
    conn = _get_conn()
    return conn.execute(
        """SELECT
             COALESCE(model, '?') as model,
             COUNT(*) as total_requests,
             SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success_count,
             SUM(CASE WHEN status='success' AND cache_read_tokens > 0 THEN 1 ELSE 0 END) as hit_requests,
             SUM(CASE WHEN status='success' AND cache_creation_tokens > 0 THEN 1 ELSE 0 END) as write_requests,
             SUM(input_tokens + cache_creation_tokens + cache_read_tokens) as total_prompt_tokens,
             SUM(cache_creation_tokens) as total_cache_creation,
             SUM(cache_read_tokens) as total_cache_read
           FROM request_log
           WHERE created_at >= ?
           GROUP BY COALESCE(model, '?')
           ORDER BY total_prompt_tokens DESC, total_requests DESC
           LIMIT ?""",
        (since_ts, limit),
    ).fetchall()


def cache_stats_by_apikey(since_ts, limit=3):
    conn = _get_conn()
    return conn.execute(
        """SELECT
             COALESCE(api_key_name, '?') as api_key_name,
             COUNT(*) as total_requests,
             SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success_count,
             SUM(CASE WHEN status='success' AND cache_read_tokens > 0 THEN 1 ELSE 0 END) as hit_requests,
             SUM(CASE WHEN status='success' AND cache_creation_tokens > 0 THEN 1 ELSE 0 END) as write_requests,
             SUM(input_tokens + cache_creation_tokens + cache_read_tokens) as total_prompt_tokens,
             SUM(cache_creation_tokens) as total_cache_creation,
             SUM(cache_read_tokens) as total_cache_read
           FROM request_log
           WHERE created_at >= ?
           GROUP BY COALESCE(api_key_name, '?')
           ORDER BY total_prompt_tokens DESC, total_requests DESC
           LIMIT ?""",
        (since_ts, limit),
    ).fetchall()


def recent_cache_misses(since_ts, limit=3):
    conn = _get_conn()
    return conn.execute(
        """SELECT
             request_id, created_at, api_key_name, model, is_stream,
             msg_count, tool_count, input_tokens, output_tokens,
             cache_creation_tokens, cache_read_tokens, connect_time_ms,
             first_token_time_ms, total_time_ms, retry_count
           FROM request_log
           WHERE created_at >= ?
             AND status='success'
             AND cache_read_tokens=0
           ORDER BY created_at DESC
           LIMIT ?""",
        (since_ts, limit),
    ).fetchall()
