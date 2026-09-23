"""抖音 (Douyin) 视频提取模块。

参考 evey2obs-ds 实现：
- 解析 v.douyin.com 短链与分享文案。
- 提取纯数字视频 ID。
- 采用 yt-dlp 进行音频抓取，支持 Whisper 转写。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import httpx
import yt_dlp

from src.config import get_config
from src.sources.base import (
    ExtractionResult,
    VideoMeta,
    save_metadata,
)

logger = logging.getLogger(__name__)

_DOUYIN_ID_RE = re.compile(r"/video/(\d+)")
_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)


def resolve_douyin_url(url_or_text: str) -> tuple[str, str]:
    """解析抖音短链并提取纯数字 ID 与规范化 URL。"""
    # 如果已经包含 /video/{id}
    m = _DOUYIN_ID_RE.search(url_or_text)
    if m:
        dy_id = m.group(1)
        return dy_id, f"https://www.douyin.com/video/{dy_id}"

    # 提取短链并重定向
    url = url_or_text.strip()
    if "v.douyin.com" in url:
        if not url.startswith("http"):
            url = f"https://{url}"
        try:
            with httpx.Client(follow_redirects=True, timeout=15.0) as client:
                resp = client.get(url, headers={"User-Agent": _USER_AGENT})
                final_url = str(resp.url)
                m = _DOUYIN_ID_RE.search(final_url)
                if m:
                    dy_id = m.group(1)
                    return dy_id, f"https://www.douyin.com/video/{dy_id}"
                raise ValueError(f"无法从抖音短链解析视频 ID: {final_url}")
        except ValueError:
            raise
        except Exception as e:
            logger.warning("抖音短链解析失败: %s", e)

    raise ValueError(f"无法解析抖音链接: {url_or_text}")

def extract_douyin(
    url_or_text: str,
    output_dir: Optional[Path] = None,
) -> ExtractionResult:
    """提取抖音视频元数据与音频。"""
    dy_id, canonical_url = resolve_douyin_url(url_or_text)

    cfg = get_config()
    root = cfg["_project_root"]
    if output_dir is None:
        output_dir = root / cfg["app"]["data_dir"] / f"douyin_{dy_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    meta_path = output_dir / "metadata.json"
    audio_path = output_dir / f"{dy_id}.m4a"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / f"{dy_id}.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(canonical_url, download=True)
            title = info.get("title", f"抖音视频_{dy_id}")
            channel = info.get("uploader", "抖音创作者")
            duration = int(info.get("duration", 0)) if info.get("duration") else 0
            publish_date = info.get("upload_date", "")
    except Exception as e:
        raise RuntimeError(f"抖音视频下载失败: {e}")

    meta = VideoMeta(
        video_id=f"dy_{dy_id}",
        url=canonical_url,
        title=title,
        channel=channel,
        duration_seconds=duration,
        publish_date=publish_date,
        platform="douyin",
    )
    save_metadata(meta, meta_path)

    result = ExtractionResult(meta=meta, source_language="zh")
    if audio_path.exists():
        result.audio_path = audio_path
    else:
        wav = output_dir / f"{dy_id}.wav"
        if wav.exists():
            result.audio_path = wav

    return result
