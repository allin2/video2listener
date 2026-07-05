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
    """兼容旧调用：在同一目录检查共享或变体阶段文件。"""
    if status in (TaskStatus.METADATA_FETCHED, TaskStatus.TEXT_READY):
        return check_shared_stage_file(data_dir, status)
    return check_variant_stage_file(data_dir, status)


def check_shared_stage_file(data_dir: Path, status: TaskStatus) -> bool:
    """检查与模式无关的下载、转写和清洗阶段文件。"""
    checks = {
        TaskStatus.METADATA_FETCHED: data_dir / "metadata.json",
        TaskStatus.TEXT_READY: data_dir / "transcript_clean.txt",
    }
    path = checks.get(status)
    return bool(path and path.is_file() and path.stat().st_size > 0)


def check_variant_stage_file(variant_dir: Path, status: TaskStatus) -> bool:
    """只在目标模式目录检查翻译、TTS 和最终输出。"""
    if status == TaskStatus.TTS_DONE:
        # 单个分段只能说明 TTS 已开始；必须已有合并后的 MP3 才算完成。
        segs_dir = variant_dir / "tts_segments"
        has_segment = segs_dir.is_dir() and any(
            path.is_file() and path.stat().st_size > 0 for path in segs_dir.iterdir()
        )
        outputs = [path for path in variant_dir.glob("*.mp3") if path.stat().st_size > 0]
        has_output = bool(outputs)
        script_path = variant_dir / "script_zh.txt"
        if has_output and script_path.is_file():
            # 新译文生成后，旧 MP3/分段不能再被误认为当前任务的完成结果。
            has_output = max(path.stat().st_mtime for path in outputs) >= script_path.stat().st_mtime
        return has_segment and has_output
    if status == TaskStatus.DONE:
        return any(path.stat().st_size > 0 for path in variant_dir.glob("*.mp3"))

    checks = {
        TaskStatus.TRANSLATED: variant_dir / "script_zh.txt",
    }
    path = checks.get(status)
    if path is None:
        return False
    if path.is_dir():
        return any(path.iterdir())
    return path.exists() and path.stat().st_size > 0
