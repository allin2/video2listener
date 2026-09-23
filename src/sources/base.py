"""视频源抽取基础模型与工具函数。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class VideoMeta:
    video_id: str
    url: str
    title: str
    channel: str
    duration_seconds: int
    publish_date: str  # ISO-format date
    platform: str = "youtube"


@dataclass
class SubtitleEntry:
    start: str  # "hh:mm:ss"
    end: str
    text: str


@dataclass
class ExtractionResult:
    meta: VideoMeta
    subtitles: list[SubtitleEntry] = field(default_factory=list)
    audio_path: Optional[Path] = None
    has_captions: bool = False
    source_language: str = "en"  # "en" / "zh" / "ja" etc.


def seconds_to_timestamp(seconds: float) -> str:
    """秒数转 hh:mm:ss 格式字符串。"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def save_metadata(meta: VideoMeta, dest_path: Path) -> None:
    """保存元数据到 JSON 文件。"""
    dest_path.write_text(
        json.dumps(
            {
                "video_id": meta.video_id,
                "url": meta.url,
                "title": meta.title,
                "channel": meta.channel,
                "duration_seconds": meta.duration_seconds,
                "publish_date": meta.publish_date,
                "platform": meta.platform,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
