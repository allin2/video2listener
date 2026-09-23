"""小红书 (Xiaohongshu) 视频提取模块。

参考 evey2obs-ds 实现：
- 解析 xhslink.com 短链与分享文案。
- 自动保留 xsec_token 关键安全鉴权参数，剥离无用追踪参数。
- 校验内容类型：图文笔记拦截并报错提示，仅放行纯视频笔记。
- 提取音频用于 Whisper 语音转写。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
import yt_dlp

from src.config import get_config
from src.sources.base import (
    ExtractionResult,
    VideoMeta,
    save_metadata,
)

logger = logging.getLogger(__name__)

_PRESERVE_PARAMS = {"xsec_token", "type"}
_INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*</script>", re.DOTALL
)
_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)


def resolve_xhs_url(url_or_text: str) -> tuple[str, str, str]:
    """解析小红书短链，保留鉴权 token，提取 note_id 与规范化 URL。

    Returns:
        (note_id, canonical_url, full_url_with_token)
    """
    raw_url = url_or_text.strip()
    if not raw_url.startswith("http"):
        raw_url = f"https://{raw_url}"

    # 短链重定向
    if "xhslink" in raw_url:
        try:
            with httpx.Client(follow_redirects=True, timeout=15.0) as client:
                resp = client.get(raw_url, headers={"User-Agent": _USER_AGENT})
                raw_url = str(resp.url)
        except Exception as e:
            logger.warning("小红书短链重定向失败: %s", e)

    parsed = urlparse(raw_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    clean_params = {k: v[0] for k, v in params.items() if k in _PRESERVE_PARAMS}

    # 提取 note_id
    m = re.search(r"/(?:discovery/item|explore)/([a-zA-Z0-9]+)", parsed.path)
    note_id = m.group(1) if m else parsed.path.rstrip("/").split("/")[-1]

    # 不带 token 的标准路径
    canonical_url = f"https://www.xiaohongshu.com/discovery/item/{note_id}"
    # 带 token 的请求 URL
    full_url = str(
        urlunparse(
            parsed._replace(
                query=urlencode(clean_params, doseq=False) if clean_params else ""
            )
        )
    )

    return note_id, canonical_url, full_url


def extract_xiaohongshu(
    url_or_text: str,
    output_dir: Optional[Path] = None,
) -> ExtractionResult:
    """提取小红书视频元数据与音频。"""
    note_id, canonical_url, full_url = resolve_xhs_url(url_or_text)

    cfg = get_config()
    root = cfg["_project_root"]
    if output_dir is None:
        output_dir = root / cfg["app"]["data_dir"] / f"xhs_{note_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    meta_path = output_dir / "metadata.json"
    audio_path = output_dir / f"{note_id}.m4a"

    # --- 1. 尝试探测是视频笔记还是图文笔记 ---
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                full_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
            html = resp.text
            m = _INITIAL_STATE_RE.search(html)
            if m:
                state = json.loads(m.group(1))
                note = state.get("note", {})
                note_type = note.get("type", "")
                if note_type == "normal" or not note.get("video"):
                    raise ValueError(
                        "检测到该小红书内容为【图文笔记】，不包含音频流，无法生成随身听播客。请提供视频笔记链接。"
                    )
    except ValueError:
        raise
    except Exception as e:
        logger.debug("小红书首屏探测忽略: %s", e)

    # --- 2. 使用 yt-dlp 抓取音频 ---
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / f"{note_id}.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(full_url, download=True)
            title = info.get("title", f"小红书视频_{note_id}")
            channel = info.get("uploader", "小红书创作者")
            duration = int(info.get("duration", 0)) if info.get("duration") else 0
            publish_date = info.get("upload_date", "")
    except Exception as e:
        msg = str(e)
        if "format" in msg.lower() or "no audio" in msg.lower():
            raise ValueError(
                "该小红书笔记没有可用的音频流（可能是图文笔记）。请提供视频笔记链接。"
            )
        raise RuntimeError(f"小红书视频下载失败: {e}")

    meta = VideoMeta(
        video_id=f"xhs_{note_id}",
        url=canonical_url,
        title=title,
        channel=channel,
        duration_seconds=duration,
        publish_date=publish_date,
        platform="xiaohongshu",
    )
    save_metadata(meta, meta_path)

    result = ExtractionResult(meta=meta, source_language="zh")
    if audio_path.exists():
        result.audio_path = audio_path
    else:
        wav = output_dir / f"{note_id}.wav"
        if wav.exists():
            result.audio_path = wav

    return result
