"""SQLite 存储层。管理任务记录和状态查询。"""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import get_config

logger = logging.getLogger(__name__)

VALID_MODES = {"podcast", "faithful", "condensed"}
VARIANT_MIGRATION = "episode_variant_v1"
_VARIANT_UPDATE_FIELDS = {
    "variant_dir",
    "script_zh_path",
    "summary_path",
    "tts_text_path",
    "tts_segments_dir",
    "audio_zh_path",
    "error_message",
    "audit_status",
    "audit_message",
    "translation_audit_path",
}


def _get_db_path() -> Path:
    cfg = get_config()
    return cfg["_project_root"] / cfg["app"]["db_path"]


def _get_conn() -> sqlite3.Connection:
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
            audit_status TEXT,
            audit_message TEXT,
            translation_audit_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glossary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term_en TEXT UNIQUE,
            term_zh TEXT,
            source_video_id TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migration (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episode_variant (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('podcast', 'faithful', 'condensed')),
            status TEXT NOT NULL DEFAULT 'new',
            variant_dir TEXT,
            script_zh_path TEXT,
            summary_path TEXT,
            tts_text_path TEXT,
            tts_segments_dir TEXT,
            audio_zh_path TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(video_id, mode),
            FOREIGN KEY(video_id) REFERENCES episode(video_id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_episode_variant_updated
        ON episode_variant(updated_at DESC)
    """)
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(episode_variant)").fetchall()
    }
    for column in ("audit_status", "audit_message", "translation_audit_path"):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE episode_variant ADD COLUMN {column} TEXT")

    migration = conn.execute(
        "SELECT 1 FROM schema_migration WHERE name = ?", (VARIANT_MIGRATION,)
    ).fetchone()
    if migration is None:
        _backfill_legacy_variants(conn)
        conn.execute(
            "INSERT INTO schema_migration (name, applied_at) VALUES (?, ?)",
            (VARIANT_MIGRATION, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", _get_db_path())


def _backfill_legacy_variants(conn: sqlite3.Connection) -> None:
    """一次性把旧 episode 中的模式产物登记为零拷贝变体。"""
    rows = conn.execute(
        """SELECT video_id, mode, status, script_zh_path, summary_path,
                  audio_zh_path, error_message, created_at, updated_at
           FROM episode"""
    ).fetchall()
    for row in rows:
        mode = row["mode"]
        if mode not in VALID_MODES:
            logger.warning("Skip legacy variant with unsupported mode: %s", mode)
            continue

        has_variant_data = any(
            row[key] for key in ("script_zh_path", "summary_path", "audio_zh_path")
        )
        if not has_variant_data and row["status"] not in ("done", "failed", "cancelled"):
            continue

        if row["audio_zh_path"] or row["status"] == "done":
            status = "done"
        elif row["script_zh_path"]:
            status = "translated"
        elif row["status"] in ("failed", "cancelled"):
            status = row["status"]
        else:
            status = "new"

        conn.execute(
            """INSERT OR IGNORE INTO episode_variant
               (video_id, mode, status, variant_dir, script_zh_path, summary_path,
                audio_zh_path, error_message, created_at, updated_at)
               VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
            (
                row["video_id"], mode, status, row["script_zh_path"],
                row["summary_path"], row["audio_zh_path"], row["error_message"],
                row["created_at"], row["updated_at"],
            ),
        )


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


def _validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"不支持的模式: {mode}")


def get_variant(video_id: str, mode: str) -> Optional[dict]:
    """查询指定视频和模式的输出变体。"""
    _validate_mode(mode)
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM episode_variant WHERE video_id = ? AND mode = ?",
        (video_id, mode),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_completed_variant(video_id: str, mode: str) -> Optional[dict]:
    """查询已完成且音频文件仍有效的指定模式变体。"""
    _validate_mode(mode)
    conn = _get_conn()
    row = conn.execute(
        """SELECT * FROM episode_variant
           WHERE video_id = ? AND mode = ? AND status = 'done'""",
        (video_id, mode),
    ).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    audio_path = result.get("audio_zh_path")
    if not audio_path:
        return None
    path = Path(audio_path)
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    return result


def list_variants(video_id: str) -> list[dict]:
    """列出一个视频的全部模式变体。"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM episode_variant
           WHERE video_id = ? ORDER BY mode ASC""",
        (video_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_all_variants() -> list[dict]:
    """一次查询返回所有模式变体，供历史聚合避免逐视频查询。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM episode_variant ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def upsert_variant(
    video_id: str,
    mode: str,
    status: str = "new",
    **kwargs,
) -> int:
    """创建或更新一个模式变体，未提供的已有字段保持不变。"""
    _validate_mode(mode)
    unknown = set(kwargs) - _VARIANT_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"不支持的变体字段: {sorted(unknown)}")

    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    columns = ["video_id", "mode", "status", "created_at", "updated_at", *kwargs.keys()]
    values = [video_id, mode, status, now, now, *kwargs.values()]
    updates = ["status = excluded.status", "updated_at = excluded.updated_at"]
    updates.extend(f"{field} = excluded.{field}" for field in kwargs)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"""INSERT INTO episode_variant ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(video_id, mode) DO UPDATE SET {', '.join(updates)}""",
        values,
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM episode_variant WHERE video_id = ? AND mode = ?",
        (video_id, mode),
    ).fetchone()
    row_id = row["id"] if row else cursor.lastrowid
    conn.close()
    return row_id


def update_variant_status(
    video_id: str,
    mode: str,
    status: str,
    error_message: Optional[str] = None,
    **kwargs,
) -> None:
    """更新指定模式变体的状态和产物路径。"""
    _validate_mode(mode)
    unknown = set(kwargs) - _VARIANT_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"不支持的变体字段: {sorted(unknown)}")

    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    sets = ["status = ?", "updated_at = ?"]
    params: list = [status, now]
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message)
    elif status != "failed":
        sets.append("error_message = NULL")
    for key, value in kwargs.items():
        sets.append(f"{key} = ?")
        params.append(value)
    params.extend([video_id, mode])
    cursor = conn.execute(
        f"UPDATE episode_variant SET {', '.join(sets)} WHERE video_id = ? AND mode = ?",
        params,
    )
    if cursor.rowcount == 0:
        conn.close()
        upsert_variant(
            video_id, mode, status=status, error_message=error_message, **kwargs
        )
        return
    conn.commit()
    conn.close()


def update_variant_artifacts(video_id: str, mode: str, **kwargs) -> None:
    """只更新变体产物字段，不改变管道状态。

    供摘要等后台任务使用，避免读取旧状态后再写回造成状态倒退。
    """
    _validate_mode(mode)
    if not kwargs:
        return
    unknown = set(kwargs) - _VARIANT_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"不支持的变体字段: {sorted(unknown)}")

    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    sets = ["updated_at = ?"]
    params: list = [now]
    for key, value in kwargs.items():
        sets.append(f"{key} = ?")
        params.append(value)
    params.extend([video_id, mode])
    cursor = conn.execute(
        f"UPDATE episode_variant SET {', '.join(sets)} WHERE video_id = ? AND mode = ?",
        params,
    )
    if cursor.rowcount == 0:
        conn.close()
        raise KeyError(f"变体不存在: {video_id}:{mode}")
    conn.commit()
    conn.close()


def delete_variant(video_id: str, mode: str) -> bool:
    """删除指定模式变体记录。"""
    _validate_mode(mode)
    conn = _get_conn()
    cursor = conn.execute(
        "DELETE FROM episode_variant WHERE video_id = ? AND mode = ?",
        (video_id, mode),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


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


def get_all_episodes() -> list[dict]:
    """获取所有 episode，按更新时间倒序。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM episode ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_episode(video_id: str) -> bool:
    """删除 episode 记录。返回 True 表示删除了至少一行。"""
    conn = _get_conn()
    cursor = conn.execute(
        "DELETE FROM episode WHERE video_id = ?",
        (video_id,),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def upsert_glossary_term(term_en: str, term_zh: str, source_video_id: str) -> None:
    """插入或更新术语表条目。term_en 为唯一键，重复时替换。"""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO glossary (term_en, term_zh, source_video_id, created_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (term_en, term_zh, source_video_id),
    )
    conn.commit()
    conn.close()


def get_glossary(limit: int = 30) -> list[dict]:
    """获取术语表，按创建时间倒序，最多返回 limit 条。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM glossary ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
