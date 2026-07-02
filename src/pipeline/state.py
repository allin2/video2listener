"""管道状态管理。定义任务状态枚举和中间文件检查。"""

from enum import Enum
from pathlib import Path
from typing import Optional


class TaskStatus(str, Enum):
    NEW = "new"
    METADATA_FETCHED = "metadata_fetched"
    TEXT_READY = "text_ready"
    TRANSLATED = "translated"
    TTS_DONE = "tts_done"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 状态流转顺序
STATUS_ORDER = [
    TaskStatus.NEW,
    TaskStatus.METADATA_FETCHED,
    TaskStatus.TEXT_READY,
    TaskStatus.TRANSLATED,
    TaskStatus.TTS_DONE,
    TaskStatus.DONE,
]


def next_status(current: TaskStatus) -> Optional[TaskStatus]:
    """获取下一个正常状态。"""
    try:
        idx = STATUS_ORDER.index(current)
        return STATUS_ORDER[idx + 1] if idx + 1 < len(STATUS_ORDER) else None
    except ValueError:
        return None


def check_stage_file(data_dir: Path, status: TaskStatus) -> bool:
    """检查对应阶段的中间文件是否存在且非空。"""
    if status == TaskStatus.TTS_DONE:
        # tts_segments 目录有文件才说明 TTS 已完成
        segs_dir = data_dir / "tts_segments"
        return segs_dir.is_dir() and any(segs_dir.iterdir())
    if status == TaskStatus.DONE:
        return any(path.stat().st_size > 0 for path in data_dir.glob("*.mp3"))

    checks = {
        TaskStatus.METADATA_FETCHED: data_dir / "metadata.json",
        TaskStatus.TEXT_READY: data_dir / "transcript_clean.txt",
        TaskStatus.TRANSLATED: data_dir / "script_zh.txt",
    }
    path = checks.get(status)
    if path is None:
        return False
    if path.is_dir():
        return any(path.iterdir())
    return path.exists() and path.stat().st_size > 0
