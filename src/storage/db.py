"""SQLite 存储层。管理任务记录和状态查询。"""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import get_config

logger = logging.getLogger(__name__)


def _get_db_path() -> Path:
    cfg = get_config()
    return cfg["_project_root"] / cfg["app"]["db_path"]


def _get_conn() -> sqlite3.Connection:
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """初始化数据库表。幂等操作。"""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episode (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL,
            title_original TEXT,
            channel_name TEXT,
            duration_seconds INTEGER,
            publish_date TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            mode TEXT NOT NULL DEFAULT 'podcast',
            data_dir TEXT,
            metadata_path TEXT,
            captions_path TEXT,
            transcript_clean_path TEXT,
            script_zh_path TEXT,
            summary_path TEXT,
            audio_zh_path TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", _get_db_path())


def create_episode(
    video_id: str,
    url: str,
    title: str = "",
    channel: str = "",
    duration: int = 0,
    publish_date: str = "",
    mode: str = "podcast",
    data_dir: str = "",
) -> int:
    """创建新的 episode 记录。返回 row id。"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO episode
           (video_id, url, title_original, channel_name, duration_seconds,
            publish_date, status, mode, data_dir, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)""",
        (video_id, url, title, channel, duration, publish_date, mode, data_dir, now, now),
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def update_status(
    video_id: str,
    status: str,
    error_message: Optional[str] = None,
    **kwargs,
) -> None:
    """更新 episode 状态和可选字段。"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    sets = ["status = ?", "updated_at = ?"]
    params: list = [status, now]

    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message)
    elif status != "failed":
        # 一次失败后的成功重试不应继续展示旧错误。
        sets.append("error_message = NULL")

    for key, value in kwargs.items():
        if value is not None:
            sets.append(f"{key} = ?")
            params.append(value)
        else:
            sets.append(f"{key} = NULL")

    params.append(video_id)
    conn.execute(
        f"UPDATE episode SET {', '.join(sets)} WHERE video_id = ?",
        params,
    )
    conn.commit()
    conn.close()


def get_episode(video_id: str) -> Optional[dict]:
    """查询 episode 记录。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM episode WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def is_duplicate(video_id: str) -> Optional[dict]:
    """检查是否已存在完成的 episode。返回记录 dict 或 None。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM episode WHERE video_id = ? AND status = 'done'",
        (video_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_pending_episodes() -> list[dict]:
    """获取所有待处理的 episode（非 done/failed 状态）。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM episode WHERE status NOT IN ('done', 'failed')"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
