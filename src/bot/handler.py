"""飞书消息处理逻辑。解析用户消息，提取模式和视频 ID。"""

import re
from typing import Optional

# 模式前缀映射
MODE_PREFIXES = {
    "浓缩": "condensed",
    "忠实": "faithful",
    "播客": "podcast",
    "condensed": "condensed",
    "faithful": "faithful",
    "podcast": "podcast",
}


def parse_message(text: str) -> tuple[Optional[str], str]:
    """解析用户消息，提取模式前缀和剩余文本。

    Returns:
        (mode: str|None, remaining: str)
    """
    text = text.strip()
    for prefix, mode in MODE_PREFIXES.items():
        if text.startswith(prefix):
            remaining = text[len(prefix):].strip()
            return mode, remaining
    return None, text


def extract_youtube_id(text: str) -> Optional[str]:
    """从文本中提取 YouTube video_id。

    支持格式：
    - 纯 11 位 ID
    - youtube.com/watch?v=xxx
    - youtu.be/xxx
    - youtube.com/embed/xxx
    - youtube.com/shorts/xxx

    Returns:
        video_id 或 None
    """
    text = text.strip()
    # 纯 ID
    if re.match(r"^[A-Za-z0-9_-]{11}$", text):
        return text
    # URL
    patterns = [
        r"(?:v=|/v/|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        match = re.search(pat, text)
        if match:
            return match.group(1)
    return None


def extract_video_id(text: str) -> Optional[str]:
    """从文本中提取视频 ID，支持 YouTube、B站、抖音、小红书等。"""
    from src.inputs import parse_video_input
    try:
        _, vid, _ = parse_video_input(text)
        return vid
    except Exception:
        return None
